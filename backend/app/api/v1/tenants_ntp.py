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


CHRONY_CONF = """\
# Serve the node's clock, with no upstream of our own.
#
# `local stratum 10` is not optional here and its absence is silent: chronyd
# refuses to answer at all until it considers itself synchronised, so without
# it the pod runs, reports Ready, and every query times out. Measured — the
# deployment was 1/1 and `ntpd -q` against both the Service and the pod IP
# returned nothing.
#
# The clock it serves is the node's, which the platform keeps correct; stratum
# 10 says plainly that this is a local reference and not a traceable chain.
local stratum 10

# Clients are tenant workers arriving through their own transit VIP; the
# Service and the transit ACL are what actually scope this.
allow all

bindaddress 0.0.0.0
bindcmdaddress /run/chrony/chronyd.sock
driftfile /var/lib/chrony/chrony.drift
"""


def build_chrony_config_map() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": NTP_APP,
            "namespace": NTP_NAMESPACE,
            "labels": {"app": NTP_APP, "kubevirt-ui.io/managed": "true"},
        },
        "data": {"chrony.conf": CHRONY_CONF},
    }


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
                                # Measured, not guessed. With `drop: ALL` and
                                # NET_BIND_SERVICE alone the pod crash-looped:
                                #
                                #   chown: /var/lib/chrony: Operation not permitted
                                #   Could not open /var/run/chrony/chronyd.pid
                                #
                                # chronyd's entrypoint takes ownership of its
                                # state and run directories and then drops
                                # privileges itself. It still may not step the
                                # clock — SYS_TIME is deliberately absent; this
                                # serves the clock it is given.
                                "drop": ["ALL"],
                                "add": [
                                    "NET_BIND_SERVICE",
                                    "CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID",
                                ],
                            },
                        },
                        # The pid file has to be cleared before start, and the
                        # reason is a trap worth naming: chronyd *is* pid 1 in
                        # this container, so it writes "1" into its pid file.
                        # `/run/chrony` is an emptyDir and survives a container
                        # restart, so the next chronyd reads a stale pid of 1,
                        # checks whether that process is alive — it always is,
                        # it is chronyd itself — and dies with
                        #
                        #   Fatal error : Another chronyd may already be
                        #   running (pid=1)
                        #
                        # forever. One crash makes the pod permanently
                        # unstartable, which is how a rollout can sit in
                        # CrashLoopBackOff while an older ReplicaSet keeps
                        # serving the image's own configuration.
                        "command": [
                            "sh", "-c",
                            "rm -f /run/chrony/chronyd.pid; "
                            # -x is what "serves a clock, never sets one"
                            # actually means to chronyd. Without it it tries to
                            # discipline the system clock at startup and dies:
                            #
                            #   CAP_SYS_TIME not present
                            #   Fatal error : adjtimex(0x8001) failed
                            #
                            # The alternative would be handing it CAP_SYS_TIME,
                            # i.e. letting a pod set the node's time — which is
                            # the opposite of the intent.
                            "exec chronyd -d -x -f /etc/chrony/chrony.conf",
                        ],
                        "volumeMounts": [
                            {"name": "run", "mountPath": "/run/chrony"},
                            {"name": "state", "mountPath": "/var/lib/chrony"},
                            {"name": "conf", "mountPath": "/etc/chrony"},
                        ],
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "32Mi"},
                            "limits": {"memory": "64Mi"},
                        },
                    }],
                    # The image writes a pid file and a drift file; the root
                    # filesystem gives it neither directory.
                    "volumes": [
                        {"name": "run", "emptyDir": {}},
                        {"name": "state", "emptyDir": {}},
                        {"name": "conf", "configMap": {"name": NTP_APP}},
                    ],
                },
            },
        },
    }


def cp_sharing_key(tenant: str) -> str:
    """The MetalLB sharing key both of this tenant's Services must carry.

    Both: MetalLB refuses the second Service outright when only it declares
    the key —

        can't change sharing key for "tenant-tntp/tntp-ntp",
        address also in use by tenant-tntp/tntp-cp-lb

    — and the NTP address stays `<pending>` forever, which presents as a
    worker that cannot get the time.
    """
    return f"{tenant}-cp"


def build_tenant_ntp_service(tenant: str, namespace: str, *, vip: str) -> dict[str, Any]:
    """UDP/123 on the tenant's existing VIP.

    A separate Service because the control-plane one selects Kamaji pods and
    this must select chrony. Same address, which MetalLB only permits with
    `allow-shared-ip` **and** no port collision — 123/udp does not meet
    6443/8132/50001, so the pair is legal.

    It lives in **chrony's** namespace, not the tenant's. A Service selects
    only pods beside it, so the tenant-namespace version had no endpoints at
    all: it existed, it had an address, and every query timed out. That looked
    exactly like a server refusing to answer, and is why the first diagnosis
    of this went to chronyd's configuration instead of here.

    `externalTrafficPolicy: Cluster` deliberately: chrony does not run on
    every node, and Local would black-hole the request from any node that has
    no replica.
    """
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": f"{tenant}-ntp",
            "namespace": NTP_NAMESPACE,
            "labels": {
                "kubevirt-ui.io/tenant": tenant,
                "kubevirt-ui.io/managed": "true",
            },
            "annotations": {
                "service.cilium.io/type": "ClusterIP",
                "metallb.universe.tf/loadBalancerIPs": vip,
                "metallb.universe.tf/allow-shared-ip": cp_sharing_key(tenant),
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
    cm = build_chrony_config_map()
    try:
        await k8s.core_api.create_namespaced_config_map(
            namespace=NTP_NAMESPACE, body=cm,
        )
    except ApiException as e:
        if e.status == 409:
            await k8s.core_api.replace_namespaced_config_map(
                name=NTP_APP, namespace=NTP_NAMESPACE, body=cm,
            )
        else:
            logger.error("Could not write the chrony config: %s", e)

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
    """Publish UDP/123 on this tenant's VIP, beside the pods that serve it."""
    body = build_tenant_ntp_service(tenant, namespace, vip=vip)
    try:
        await k8s.core_api.create_namespaced_service(
            namespace=NTP_NAMESPACE, body=body,
        )
        logger.info("Published NTP for %s on %s:123/udp", tenant, vip)
    except ApiException as e:
        if e.status != 409:
            logger.error("Could not publish NTP for %s on %s: %s", tenant, vip, e)
