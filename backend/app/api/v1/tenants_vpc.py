"""VPC binding for tenants (T7 — explicit label-based selection).

The tenant create flow does NOT provision per-tenant VPCs. Instead, when the
caller supplies an explicit `vpc_name`, that VPC's `spec.namespaces` is
patched to include the new tenant namespace. The caller (tenant CRUD layer)
is responsible for validating that the chosen VPC is scoped to the same
folder/env as the tenant via the VPC's `kubevirt-ui.io/folder` +
`kubevirt-ui.io/environment` labels.

T4 had an auto-derive lookup (find VPC whose `spec.namespaces` contains the
env namespace) — gone, because kube-ovn allows a namespace in at most one
VPC, so the lookup couldn't represent "multiple eligible VPCs for one env".
Label-based selection lets admins create N VPCs scoped to (folder, env) and
expose them to the wizard as a dropdown.
"""

import logging

from fastapi import HTTPException
from kubernetes_asyncio.client import ApiException

from app.core.constants import KUBEOVN_API_GROUP, KUBEOVN_API_VERSION

from app.api.v1.tenants_common import TENANT_NS_PREFIX

logger = logging.getLogger(__name__)


async def attach_tenant_to_vpc(k8s, vpc_name: str, tenant_name: str) -> None:
    """Append `tenant-<tenant_name>` to the named VPC's `spec.namespaces`.

    The caller must have already validated that the VPC is appropriate for
    the tenant (folder/env labels match). This function only handles the
    patch — it doesn't re-check labels.

    Raises HTTPException(400) if the VPC doesn't exist. Idempotent: if the
    tenant ns is already in the list, the patch is skipped.

    B3 (T9): uses JSON-patch `add /spec/namespaces/-` (append) instead of a
    merge-patch on the whole array. Merge-patch on a list REPLACES the
    entire array — two concurrent attaches would race and the second writer
    would clobber the first's append. JSON-patch's `add /-` is server-side
    append and avoids the read-modify-write window entirely.
    """
    tenant_ns = f"{TENANT_NS_PREFIX}{tenant_name}"

    try:
        vpc = await k8s.custom_api.get_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="vpcs",
            name=vpc_name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=400,
                detail=f"VPC '{vpc_name}' does not exist",
            ) from e
        raise

    current = (vpc.get("spec") or {}).get("namespaces") or []
    if tenant_ns in current:
        logger.info(
            f"Tenant ns {tenant_ns!r} already in VPC {vpc_name!r}.namespaces — no patch"
        )
        return

    # JSON-patch: append to spec.namespaces (or initialise the array if absent).
    # Without the conditional init step, `add /spec/namespaces/-` would 422 when
    # the VPC was created without an explicit namespaces field.
    patch_ops: list[dict] = []
    if (vpc.get("spec") or {}).get("namespaces") is None:
        patch_ops.append({"op": "add", "path": "/spec/namespaces", "value": []})
    patch_ops.append({"op": "add", "path": "/spec/namespaces/-", "value": tenant_ns})

    await k8s.custom_api.patch_cluster_custom_object(
        group=KUBEOVN_API_GROUP,
        version=KUBEOVN_API_VERSION,
        plural="vpcs",
        name=vpc_name,
        body=patch_ops,
        _content_type="application/json-patch+json",
    )
    logger.info(
        f"Attached tenant ns {tenant_ns!r} to VPC {vpc_name!r} "
        f"(was {len(current)} ns)"
    )


async def detach_tenant_from_folder_vpc(k8s, tenant_name: str) -> None:
    """Remove `tenant-<name>` from any VPC's `spec.namespaces`.

    Inverse of `attach_tenant_to_vpc` — run on tenant delete so the VPC
    doesn't keep stale namespace bindings after the tenant ns is cascade-
    deleted. Iterates every managed VPC because we don't track which one
    the tenant attached to in tenant metadata (and historically multiple
    VPCs could theoretically reference the ns).

    Idempotent + best-effort: per-VPC failures are logged but never raised.

    B3 (T9): uses JSON-patch with a `test` op so the removal is safe against
    concurrent attaches reordering `spec.namespaces`. If the element at the
    computed index isn't the tenant ns by the time the patch lands, the
    apiserver rejects the test op (422) and we retry from a fresh GET. With
    a small retry cap to avoid unbounded loops in pathological cases.
    """
    tenant_ns = f"{TENANT_NS_PREFIX}{tenant_name}"
    try:
        result = await k8s.custom_api.list_cluster_custom_object(
            group=KUBEOVN_API_GROUP,
            version=KUBEOVN_API_VERSION,
            plural="vpcs",
        )
    except ApiException as e:
        if e.status == 404:
            return  # CRD not installed → nothing to detach
        logger.warning(f"Failed to list VPCs while detaching tenant {tenant_name}: {e}")
        return

    for vpc in result.get("items", []):
        spec_ns = (vpc.get("spec") or {}).get("namespaces") or []
        if tenant_ns not in spec_ns:
            continue
        vpc_name = vpc["metadata"]["name"]
        await _detach_with_retry(k8s, vpc_name, tenant_ns, initial_spec_ns=spec_ns)


async def _detach_with_retry(
    k8s, vpc_name: str, tenant_ns: str,
    initial_spec_ns: list[str],
    max_attempts: int = 5,
) -> None:
    """JSON-patch remove with test-op guard; refresh on 422 (test failed).

    Each iteration: locate `tenant_ns` in the array, emit
      [{"op":"test","path":"/spec/namespaces/<idx>","value":tenant_ns},
       {"op":"remove","path":"/spec/namespaces/<idx>"}]
    A 422 from the apiserver means the index shifted under us — re-GET and
    retry. After `max_attempts` we give up with a warning.
    """
    spec_ns = initial_spec_ns
    for attempt in range(max_attempts):
        try:
            idx = spec_ns.index(tenant_ns)
        except ValueError:
            return  # already gone — concurrent detach won the race
        patch_ops = [
            {"op": "test", "path": f"/spec/namespaces/{idx}", "value": tenant_ns},
            {"op": "remove", "path": f"/spec/namespaces/{idx}"},
        ]
        try:
            await k8s.custom_api.patch_cluster_custom_object(
                group=KUBEOVN_API_GROUP,
                version=KUBEOVN_API_VERSION,
                plural="vpcs",
                name=vpc_name,
                body=patch_ops,
                _content_type="application/json-patch+json",
            )
            logger.info(
                f"Detached tenant ns {tenant_ns!r} from VPC {vpc_name!r}"
            )
            return
        except ApiException as e:
            # 422 → test op failed (index shifted); 409 → resourceVersion stale.
            # Both are retryable by re-reading.
            if e.status not in (409, 422):
                logger.warning(
                    f"Failed to detach tenant ns {tenant_ns!r} from VPC {vpc_name!r}: {e}"
                )
                return
            try:
                vpc = await k8s.custom_api.get_cluster_custom_object(
                    group=KUBEOVN_API_GROUP,
                    version=KUBEOVN_API_VERSION,
                    plural="vpcs",
                    name=vpc_name,
                )
                spec_ns = (vpc.get("spec") or {}).get("namespaces") or []
            except ApiException as ge:
                if ge.status == 404:
                    return  # VPC vanished — nothing to detach
                logger.warning(
                    f"Failed to re-read VPC {vpc_name!r} during detach retry: {ge}"
                )
                return
    logger.warning(
        f"Gave up detaching {tenant_ns!r} from VPC {vpc_name!r} "
        f"after {max_attempts} retries — manual cleanup may be required"
    )
