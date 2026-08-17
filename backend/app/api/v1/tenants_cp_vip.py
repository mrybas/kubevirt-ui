"""One VIP per tenant, allocated by MetalLB — not by us.

The shared-VIP model gave every VPC tenant the same address and told them
apart by port, which is why `tenants_cp_ports.py` exists at all. Talos ends
that model: it dials trustd on a **fixed** :50001 of whatever host is in
`cluster.controlPlane.endpoint`, and MetalLB only lets Services share an
address while their ports do not overlap. The third port would be handed to
exactly one tenant, and the second tenant's Service would silently never get
an address — the same shape as Kamaji's hardcoded 8133/8134 collision.

So each tenant gets its own address and every tenant uses the same ports:
api 6443, konnectivity 8132, and — for Talos — trustd 50001. That deletes the
port allocator, the SNI hop in the join path, and the DNS dependency in the
join path, all at once.

**Who owns the address space matters more than which addresses we pick.**
An obvious implementation is a ConfigMap allocator of our own over the pool's
range, in the shape of the transit-IP allocator. That would be a second
allocator over one key space: MetalLB hands addresses to any Service that
asks, ours would hand out the same ones, and the collision would be silent —
the fifth visit from "two records, one key" in this codebase. Instead the
Service is created *first, without an address*, MetalLB assigns one, and we
read the fact back. MetalLB stays the only owner; we are a reader.

"The address is needed up front" still holds: it is needed before the
*tenant cluster* is built (advertiseAddress, cluster-info, certSANs), not
before the Service. Creating the Service first costs nothing — its selector
simply matches no pods for a few seconds longer.

Note on waiting: MetalLB assigns the address immediately (IPAM), but in L2
mode it does not *announce* it until the Service has ready endpoints — which
it cannot have before the control-plane pods exist. So this waits for
`status.loadBalancer.ingress[0].ip` and nothing else. Waiting for the address
to answer traffic here would deadlock tenant creation on a chicken-and-egg.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from typing import Any

from fastapi import HTTPException
from kubernetes_asyncio.client import ApiException

logger = logging.getLogger(__name__)

# Every tenant uses the same ports now — that is the point of an address each.
TENANT_API_PORT = 6443
TENANT_KONN_PORT = 8132
TENANT_TRUSTD_PORT = 50001

METALLB_GROUP = "metallb.io"
METALLB_VERSION = "v1beta1"


def per_tenant_vip_enabled() -> bool:
    """Off switch for the shared-VIP model, kept for the migration window.

    Existing tenants keep the address and ports baked into their apiserver
    flags and cluster-info; nothing re-derives them. Only new tenants take
    this path.
    """
    return (os.getenv("TENANTS_CP_PER_TENANT_VIP", "true") or "").lower() not in (
        "false", "0", "no",
    )


def cp_metallb_pool() -> str:
    return os.getenv("TENANTS_CP_METALLB_POOL", "traefik")


def cp_metallb_namespace() -> str:
    return os.getenv("TENANTS_CP_METALLB_NAMESPACE", "o0-metallb")


def _parse_ranges(entries: list[str], sep: str) -> list[tuple[int, int]]:
    """`a-b` / `a..b` / bare address → integer ranges, unparsable skipped."""
    out: list[tuple[int, int]] = []
    for raw in entries or []:
        text = str(raw).strip()
        if not text:
            continue
        try:
            if sep in text:
                lo, hi = text.split(sep, 1)
                out.append((int(ipaddress.IPv4Address(lo.strip())),
                            int(ipaddress.IPv4Address(hi.strip()))))
            elif "/" in text:
                net = ipaddress.IPv4Network(text, strict=False)
                out.append((int(net.network_address), int(net.broadcast_address)))
            else:
                one = int(ipaddress.IPv4Address(text))
                out.append((one, one))
        except (ValueError, ipaddress.AddressValueError):
            logger.debug("Skipping unparsable range %r", text)
    return out


def _covered(inner: tuple[int, int], outer: list[tuple[int, int]]) -> bool:
    """Is `inner` fully inside the union of `outer`? Ranges may be adjacent."""
    lo, hi = inner
    for start, end in sorted(outer):
        if start > lo:
            break
        if end >= hi:
            return True
        if end >= lo:  # partial cover — continue from where it ends
            lo = end + 1
    return lo > hi


async def assert_pool_excluded_from_ovn(k8s, pool: str, subnet: str) -> None:
    """The MetalLB pool must sit inside the subnet's excludeIps.

    kube-ovn hands out addresses from the same range for router legs and
    EIPs. If a pool range is not excluded, both allocators can produce the
    same address and the loser is discovered as a duplicate-IP outage. This
    is the exact hazard the `.0.100` demux VIP was configured around, and it
    is checked here rather than believed.

    Silent when either object cannot be read: a diagnostic must not be the
    thing that stops a tenant from being created.
    """
    try:
        pool_obj = await k8s.custom_api.get_namespaced_custom_object(
            group=METALLB_GROUP, version=METALLB_VERSION,
            namespace=cp_metallb_namespace(), plural="ipaddresspools", name=pool,
        )
        subnet_obj = await k8s.custom_api.get_cluster_custom_object(
            group="kubeovn.io", version="v1", plural="subnets", name=subnet,
        )
    except ApiException as e:
        logger.debug("Pool/subnet check skipped (%s)", e)
        return

    pool_ranges = _parse_ranges(
        (pool_obj.get("spec", {}) or {}).get("addresses") or [], "-",
    )
    excluded = _parse_ranges(
        (subnet_obj.get("spec", {}) or {}).get("excludeIps") or [], "..",
    )

    uncovered = [r for r in pool_ranges if not _covered(r, excluded)]
    if not uncovered:
        return

    shown = ", ".join(
        f"{ipaddress.IPv4Address(lo)}-{ipaddress.IPv4Address(hi)}"
        for lo, hi in uncovered
    )
    raise HTTPException(
        status_code=422,
        detail=(
            f"MetalLB pool {pool!r} has ranges outside the excludeIps of subnet "
            f"{subnet!r}: {shown}. kube-ovn allocates router legs and EIPs from "
            f"that subnet, so it can hand out an address MetalLB has already "
            f"given to a tenant VIP. Add the range to the subnet's excludeIps "
            f"before creating tenants on this pool."
        ),
    )


def build_cp_lb_service(
    tenant: str, namespace: str, *, ports: list[tuple[str, int]], pool: str,
) -> dict[str, Any]:
    """The tenant's own LoadBalancer, with no address of its own yet.

    Deliberately no `loadBalancerIPs` and no `allow-shared-ip`: MetalLB picks
    the address from `pool`, and nothing is shared, so no port may collide
    with another tenant's.

    `service.cilium.io/type: ClusterIP` opts the address out of Cilium's LB
    BPF DNAT (same fix as the ingress Services) so in-VPC pod traffic is not
    rewritten before kube-ovn routing.
    """
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": f"{tenant}-cp-lb",
            "namespace": namespace,
            "labels": {
                "kubevirt-ui.io/tenant": tenant,
                "kubevirt-ui.io/managed": "true",
            },
            "annotations": {
                "service.cilium.io/type": "ClusterIP",
                "metallb.universe.tf/address-pool": pool,
            },
        },
        "spec": {
            "type": "LoadBalancer",
            "selector": {"kamaji.clastix.io/name": tenant},
            "ports": [
                {"name": name, "port": port, "targetPort": port, "protocol": "TCP"}
                for name, port in ports
            ],
        },
    }


def tenant_cp_ports(worker_os: str) -> list[tuple[str, int]]:
    """api + konnectivity for everyone; trustd only where Talos needs it."""
    ports = [("api", TENANT_API_PORT), ("konn", TENANT_KONN_PORT)]
    if worker_os == "talos":
        # Talos dials this port and no other; it is not configurable, which is
        # the whole reason each tenant needs its own address.
        ports.append(("trustd", TENANT_TRUSTD_PORT))
    return ports


async def acquire_tenant_vip(
    k8s, tenant: str, namespace: str, *, worker_os: str,
    timeout: float = 60.0, poll: float = 1.0,
) -> str:
    """Create the tenant's cp-lb Service and return the address MetalLB gave it.

    Idempotent: an existing Service is reused, so a retried create never asks
    for a second address.
    """
    pool = cp_metallb_pool()
    body = build_cp_lb_service(
        tenant, namespace, ports=tenant_cp_ports(worker_os), pool=pool,
    )

    try:
        await k8s.core_api.create_namespaced_service(namespace=namespace, body=body)
    except ApiException as e:
        if e.status != 409:
            raise
        logger.info("Tenant %r: cp-lb Service already exists, reusing it", tenant)

    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            svc = await k8s.core_api.read_namespaced_service(
                name=f"{tenant}-cp-lb", namespace=namespace,
            )
        except ApiException as e:
            raise HTTPException(
                status_code=500, detail=f"Could not read {tenant}-cp-lb: {e}",
            ) from e

        ingress = ((getattr(svc.status, "load_balancer", None) or None)
                   and svc.status.load_balancer.ingress) or []
        for entry in ingress:
            ip = getattr(entry, "ip", None)
            if ip:
                logger.info("Tenant %r: MetalLB assigned VIP %s from pool %r",
                            tenant, ip, pool)
                return ip

        if asyncio.get_running_loop().time() >= deadline:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"MetalLB did not assign an address to {tenant}-cp-lb from "
                    f"pool {pool!r} within {int(timeout)}s. The usual cause is "
                    f"an exhausted pool: every address in it is already held by "
                    f"another tenant. Append a range to the IPAddressPool "
                    f"(append — never resize a range in use) and retry."
                ),
            )
        await asyncio.sleep(poll)
