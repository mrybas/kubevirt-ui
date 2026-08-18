"""Time for isolated tenants, served from the transit plane.

Measured in T8: **Talos does not start the kubelet until the clock is
synchronised.** While `t8v` had no way out, the node presented as a join
failure — nothing in the logs named the clock, and the symptom is
indistinguishable from a network fault:

    waiting for time sync

The obvious answer, public NTP through egress, is the wrong one. It makes the
join a soft dependency of the internet plane, and quietly deletes the property
the whole B3 design is for: an egress outage must not stop a node from
joining. It would hold for existing workers and fail only for new ones —
the kind of regression nobody notices until a bad day.

So the time comes from the same address the node already dials for its API,
konnectivity and trustd: the tenant's own VIP on the transit plane, which is
reachable before any gateway exists. Behind it, chrony on the host cluster,
whose clock is the nodes' clock — they are synchronised, and a container
shares the host's clock, so there is nothing further upstream to depend on.

Public servers stay in the list *after* it. When egress is alive they are
better sources; when it is not, the first entry already answered.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from kubernetes_asyncio.client import ApiException

logger = logging.getLogger(__name__)

NTP_PORT = 123
NTP_NAMESPACE = os.getenv("TENANTS_NTP_NAMESPACE", "kubevirt-ui-system")
NTP_APP = "kubevirt-ui-ntp"
CHRONY_IMAGE = os.getenv("TENANTS_CHRONY_IMAGE", "cturra/ntp:latest")

# Ordered on purpose: the transit address first because it is the one that
# works with no egress at all. These are only reached when it does not answer.
PUBLIC_FALLBACK_NTP = ("time.cloudflare.com", "pool.ntp.org")


def public_fallbacks() -> list[str]:
    raw = os.getenv("TENANTS_NTP_FALLBACK")
    if raw is None:
        return list(PUBLIC_FALLBACK_NTP)
    return [s.strip() for s in raw.split(",") if s.strip()]


def worker_time_servers(vip: str | None) -> list[str]:
    """`machine.time.servers` for a worker.

    The tenant's VIP first, public servers after. With no VIP — a tenant on
    the default overlay, where egress is not in question — only the public
    list, which is what Talos would have used anyway.
    """
    fallbacks = public_fallbacks()
    return [vip, *fallbacks] if vip else fallbacks


def build_chrony_deployment() -> dict[str, Any]:
    """One chrony for the cluster, serving the nodes' own clock.

    `LOG_LEVEL=0` and a single replica on purpose: this is infrastructure that
    must answer during a join, not a service anybody watches. It needs no
    upstream — the container's clock *is* the node's clock, and the nodes are
    synchronised by the platform.
    """
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": NTP_APP,
            "namespace": NTP_NAMESPACE,
            "labels": {"app": NTP_APP, "kubevirt-ui.io/managed": "true"},
        },
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": NTP_APP}},
            "template": {
                "metadata": {"labels": {"app": NTP_APP}},
                "spec": {
                    # Spread them: a tenant that cannot get the time cannot get
                    # a node, so both replicas on one node would put every
                    # future join behind a single drain.
                    "topologySpreadConstraints": [{
                        "maxSkew": 1,
                        "topologyKey": "kubernetes.io/hostname",
                        "whenUnsatisfiable": "ScheduleAnyway",
                        "labelSelector": {"matchLabels": {"app": NTP_APP}},
                    }],
                    "containers": [{
                        "name": "chrony",
                        "image": CHRONY_IMAGE,
                        "env": [
                            {"name": "LOG_LEVEL", "value": "0"},
                            {"name": "ENABLE_NTS", "value": "false"},
                        ],
                        "ports": [{
                            "containerPort": NTP_PORT,
                            "protocol": "UDP",
                            "name": "ntp",
                        }],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {
                                # chrony binds :123 and wants to step the
                                # clock; it may not, and does not need to —
                                # it serves the clock it is given.
                                "drop": ["ALL"],
                                "add": ["NET_BIND_SERVICE"],
                            },
                        },
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "32Mi"},
                            "limits": {"memory": "64Mi"},
                        },
                    }],
                },
            },
        },
    }


def build_tenant_ntp_service(tenant: str, namespace: str, *, vip: str) -> dict[str, Any]:
    """UDP/123 on the tenant's existing VIP.

    A separate Service because the control-plane one selects Kamaji pods and
    this must select chrony. Same address, which MetalLB only permits with
    `allow-shared-ip` **and** no port collision — 123/udp does not meet
    6443/8132/50001, so the pair is legal.

    `externalTrafficPolicy: Cluster` deliberately: chrony does not run on
    every node, and Local would black-hole the request from any node that has
    no replica.
    """
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": f"{tenant}-ntp",
            "namespace": namespace,
            "labels": {
                "kubevirt-ui.io/tenant": tenant,
                "kubevirt-ui.io/managed": "true",
            },
            "annotations": {
                "service.cilium.io/type": "ClusterIP",
                "metallb.universe.tf/loadBalancerIPs": vip,
                "metallb.universe.tf/allow-shared-ip": f"{tenant}-cp",
            },
        },
        "spec": {
            "type": "LoadBalancer",
            "externalTrafficPolicy": "Cluster",
            "selector": {"app": NTP_APP},
            "ports": [{
                "name": "ntp",
                "port": NTP_PORT,
                "targetPort": NTP_PORT,
                "protocol": "UDP",
            }],
        },
    }


async def ensure_ntp_server(k8s) -> None:
    """The cluster-wide chrony. Idempotent; failures are not fatal here.

    A tenant whose NTP Service exists but whose chrony is missing fails at
    join with the same "waiting for time sync" — so this is logged loudly
    rather than swallowed, but it does not abort a tenant create: the public
    fallbacks still work wherever egress does.
    """
    body = build_chrony_deployment()
    try:
        await k8s.apps_api.create_namespaced_deployment(
            namespace=NTP_NAMESPACE, body=body,
        )
        logger.info("Created the shared chrony deployment in %s", NTP_NAMESPACE)
    except ApiException as e:
        if e.status == 409:
            return
        logger.error(
            "Could not create the shared NTP server (%s). Talos workers in "
            "isolated VPCs will park on 'waiting for time sync' unless egress "
            "is up.", e,
        )


async def ensure_tenant_ntp_service(k8s, tenant: str, namespace: str, vip: str) -> None:
    """Publish UDP/123 on this tenant's VIP."""
    body = build_tenant_ntp_service(tenant, namespace, vip=vip)
    try:
        await k8s.core_api.create_namespaced_service(namespace=namespace, body=body)
        logger.info("Published NTP for %s on %s:123/udp", tenant, vip)
    except ApiException as e:
        if e.status != 409:
            logger.error("Could not publish NTP for %s on %s: %s", tenant, vip, e)
