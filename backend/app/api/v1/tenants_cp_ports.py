"""Control-plane port allocator for VPC-isolated tenants.

VPC tenants reach their apiserver/konnectivity through ONE shared MetalLB
VIP (the "CP demux VIP") demultiplexed by PORT — each tenant gets a
per-tenant `<t>-cp-lb` LoadBalancer Service sharing that VIP
(`metallb.universe.tf/allow-shared-ip`). MetalLB's only constraint is that
ports never overlap across the Services sharing one IP, so port allocation
must be GLOBAL (not per-tenant-independent).

This module owns that global allocation: a single cluster-wide ConfigMap
maps tenant → (api_port, konn_port). Allocation is idempotent — a tenant's
ports must NEVER change once issued, because they get baked into the
apiserver `--secure-port`, the advertised cluster-info endpoint, the KCP
konnectivity server port, and the cp-lb Service. Re-running create / a
storage-reconcile / a repair returns the same ports.

Pattern mirrors the egress-gateway transit-IP allocator (ConfigMap +
optimistic-lock on resourceVersion).

Port space: a single wide flat pool (default 20000-60000), two ports per
tenant → ~20k tenants per VIP, i.e. port exhaustion is not a practical
limit (real ceiling is Kamaji control-plane density on the host cluster).
Kamaji hardcodes its konnectivity admin=8133 / health=8134 ports, so those
are excluded defensively even though the default pool doesn't reach them.
"""

from __future__ import annotations

import logging
import os

from kubernetes_asyncio.client import ApiException, V1ConfigMap, V1ObjectMeta

from app.core.constants import SYSTEM_NAMESPACE

logger = logging.getLogger(__name__)

CP_PORTS_CM_NAME = "kubevirt-ui-cp-ports"
MANAGED_LABEL = "kubevirt-ui.io/managed"

# Flat port pool [BASE, END) — two ports per tenant. Override via env.
_PORT_BASE = int(os.getenv("TENANTS_CP_PORT_BASE", "20000"))
_PORT_END = int(os.getenv("TENANTS_CP_PORT_END", "60000"))


def _reserved_ports() -> set[int]:
    """Ports the allocator must never hand out.

    Kamaji's konnectivity-server hardcodes admin-port=8133 and
    health-port=8134 (not derived from the configured agent port); a tenant
    konn port colliding with those crashloops the server. Defensive even
    though the default pool starts at 20000. Override via
    TENANTS_CP_RESERVED_PORTS (comma-separated).
    """
    raw = os.getenv("TENANTS_CP_RESERVED_PORTS", "8133,8134")
    out: set[int] = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if tok.isdigit():
            out.add(int(tok))
    return out


def _parse_entry(value: str) -> tuple[int, int] | None:
    """Parse a CM value 'api=NNNN,konn=MMMM' → (api, konn)."""
    api = konn = None
    for part in value.split(","):
        k, _, v = part.partition("=")
        if k.strip() == "api" and v.strip().isdigit():
            api = int(v.strip())
        elif k.strip() == "konn" and v.strip().isdigit():
            konn = int(v.strip())
    if api is None or konn is None:
        return None
    return api, konn


def _format_entry(api: int, konn: int) -> str:
    return f"api={api},konn={konn}"


async def _read_cm(k8s) -> tuple[dict[str, str], str]:
    """Return (data, resourceVersion). Missing CM → ({}, "")."""
    try:
        cm = await k8s.core_api.read_namespaced_config_map(
            name=CP_PORTS_CM_NAME, namespace=SYSTEM_NAMESPACE,
        )
        return cm.data or {}, cm.metadata.resource_version
    except ApiException as e:
        if e.status == 404:
            return {}, ""
        raise


async def _write_cm(k8s, data: dict[str, str], resource_version: str) -> None:
    """Create or replace the allocator CM with optimistic locking."""
    body = V1ConfigMap(
        metadata=V1ObjectMeta(
            name=CP_PORTS_CM_NAME,
            namespace=SYSTEM_NAMESPACE,
            labels={MANAGED_LABEL: "true", "kubevirt-ui.io/role": "cp-port-allocator"},
            **({"resource_version": resource_version} if resource_version else {}),
        ),
        data=data,
    )
    if resource_version:
        await k8s.core_api.replace_namespaced_config_map(
            name=CP_PORTS_CM_NAME, namespace=SYSTEM_NAMESPACE, body=body,
        )
    else:
        await k8s.core_api.create_namespaced_config_map(
            namespace=SYSTEM_NAMESPACE, body=body,
        )


def _used_ports(data: dict[str, str]) -> set[int]:
    used: set[int] = set()
    for v in data.values():
        parsed = _parse_entry(v)
        if parsed:
            used.update(parsed)
    return used


def _pick_two_free(used: set[int]) -> tuple[int, int]:
    """Pick the two lowest free ports in [BASE, END) avoiding `used` + reserved."""
    blocked = used | _reserved_ports()
    picked: list[int] = []
    for p in range(_PORT_BASE, _PORT_END):
        if p not in blocked:
            picked.append(p)
            blocked.add(p)
            if len(picked) == 2:
                return picked[0], picked[1]
    raise RuntimeError(
        f"CP port pool [{_PORT_BASE},{_PORT_END}) exhausted "
        f"({len(used)} ports in use) — widen TENANTS_CP_PORT_END"
    )


async def allocate_cp_ports(k8s, tenant_name: str, max_retries: int = 5) -> tuple[int, int]:
    """Return this tenant's (api_port, konn_port), allocating on first call.

    Idempotent: an existing tenant always gets the same ports back.
    Optimistic-lock retry on concurrent writes.
    """
    for _ in range(max_retries):
        data, rv = await _read_cm(k8s)
        existing = data.get(tenant_name)
        if existing:
            parsed = _parse_entry(existing)
            if parsed:
                return parsed
            # corrupt entry — fall through to re-allocate over it

        api, konn = _pick_two_free(_used_ports(data))
        new_data = dict(data)
        new_data[tenant_name] = _format_entry(api, konn)
        try:
            await _write_cm(k8s, new_data, rv)
            logger.info(
                f"Allocated CP ports for tenant {tenant_name!r}: api={api} konn={konn}"
            )
            return api, konn
        except ApiException as e:
            if e.status == 409:
                continue  # someone else wrote — re-read and retry
            raise
    raise RuntimeError(
        f"Could not allocate CP ports for {tenant_name!r} after {max_retries} "
        "conflict retries"
    )


async def release_cp_ports(k8s, tenant_name: str, max_retries: int = 5) -> None:
    """Free a tenant's CP ports (delete its CM entry). Idempotent."""
    for _ in range(max_retries):
        data, rv = await _read_cm(k8s)
        if tenant_name not in data:
            return
        new_data = dict(data)
        del new_data[tenant_name]
        try:
            await _write_cm(k8s, new_data, rv)
            logger.info(f"Released CP ports for tenant {tenant_name!r}")
            return
        except ApiException as e:
            if e.status == 409:
                continue
            if e.status == 404:
                return
            raise
    logger.warning(
        f"Could not release CP ports for {tenant_name!r} after {max_retries} "
        "retries — manual ConfigMap cleanup may be needed"
    )
