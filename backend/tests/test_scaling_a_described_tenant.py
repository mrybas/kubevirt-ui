"""Scaling a described tenant changes the description.

Neither growing the pool nor resizing it worked, and both failed the same
quiet way. The endpoint patches the MachineDeployment's replicas and rotates the
worker template; for a tenant the operator holds, the next pass writes
`replicas` back from `spec.workers.count` and replaces the template it does not
recognise. Nothing errors — the number moves and then returns.

The resize had a second failure underneath, in the operator: it wrote the
template under a fixed name, and CAPI rolls a pool when the template
*reference* changes. Editing it in place gives the new shape to the next worker
created and none of the ones running. That half is fixed by naming the template
after the shape.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from kubernetes_asyncio.client.rest import ApiException

from app.api.v1.tenants_crud import scale_tenant
from app.models.tenant import TenantScaleRequest


def _stand(monkeypatch, *, described: dict | None):
    import app.api.v1.tenants_crud as mod

    monkeypatch.setattr(mod, "require_tenant_access", AsyncMock())
    monkeypatch.setattr(mod, "_plan_tenant_quota", AsyncMock(return_value={}))
    monkeypatch.setattr(mod, "_write_tenant_quota", AsyncMock())
    monkeypatch.setattr(mod, "get_tenant", AsyncMock(return_value="tenant"))
    monkeypatch.setattr(mod, "tenant_addons_path_enabled", lambda: True)
    rotate = AsyncMock(return_value=None)
    monkeypatch.setattr(mod, "_rotate_worker_template", rotate)

    k8s = MagicMock()
    if described is None:
        k8s.custom_api.get_cluster_custom_object = AsyncMock(
            side_effect=ApiException(status=404))
    else:
        described.setdefault("metadata", {}).setdefault("resourceVersion", "7")
        k8s.custom_api.get_cluster_custom_object = AsyncMock(return_value=described)
    k8s.custom_api.patch_cluster_custom_object = AsyncMock()
    k8s.custom_api.patch_namespaced_custom_object = AsyncMock()

    request = MagicMock()
    request.app.state.k8s_client = k8s
    return request, k8s, rotate


def _described(count=2, vcpu=2, memory="2Gi"):
    return {"spec": {"workers": {
        "count": count, "vcpu": vcpu, "memory": memory,
        "disk": "20Gi", "os": "talos"}}}


@pytest.mark.asyncio
async def test_growing_the_pool_edits_the_description(monkeypatch) -> None:
    request, k8s, rotate = _stand(monkeypatch, described=_described())

    await scale_tenant(request, "t1", TenantScaleRequest(worker_count=5),
                       user=MagicMock())

    k8s.custom_api.patch_namespaced_custom_object.assert_not_awaited()
    rotate.assert_not_awaited()
    patch = k8s.custom_api.patch_cluster_custom_object.await_args.kwargs
    assert patch["plural"] == "managedtenants"
    assert patch["body"]["spec"]["workers"]["count"] == 5
    # Everything else about the pool survives a resize of one field.
    assert patch["body"]["spec"]["workers"]["disk"] == "20Gi"
    assert patch["body"]["metadata"]["resourceVersion"] == "7"


@pytest.mark.asyncio
async def test_resizing_carries_cpu_and_memory(monkeypatch) -> None:
    request, k8s, _ = _stand(monkeypatch, described=_described())

    await scale_tenant(
        request, "t1",
        TenantScaleRequest(worker_count=2, worker_vcpu=4, worker_memory="8Gi"),
        user=MagicMock())

    workers = k8s.custom_api.patch_cluster_custom_object.await_args.kwargs[
        "body"]["spec"]["workers"]
    assert (workers["vcpu"], workers["memory"]) == (4, "8Gi")


@pytest.mark.asyncio
async def test_a_tenant_the_operator_does_not_hold_is_scaled_as_before(monkeypatch) -> None:
    """Half a migration is the normal state, and the old path still works."""
    request, k8s, rotate = _stand(monkeypatch, described=None)

    await scale_tenant(request, "legacy", TenantScaleRequest(worker_count=3),
                       user=MagicMock())

    k8s.custom_api.patch_namespaced_custom_object.assert_awaited()
    k8s.custom_api.patch_cluster_custom_object.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_resizes_that_read_the_same_shape_do_not_lose_one(monkeypatch) -> None:
    request, k8s, _ = _stand(monkeypatch, described=_described())
    k8s.custom_api.patch_cluster_custom_object = AsyncMock(
        side_effect=ApiException(status=409))

    with pytest.raises(HTTPException) as e:
        await scale_tenant(request, "t1", TenantScaleRequest(worker_count=4),
                           user=MagicMock())

    assert e.value.status_code == 409
    assert "try again" in e.value.detail


@pytest.mark.asyncio
async def test_the_quota_follows_the_shape_either_way(monkeypatch) -> None:
    """A scale-up whose quota lags is refused pod by pod until the next pass."""
    import app.api.v1.tenants_crud as mod

    request, _k8s, _ = _stand(monkeypatch, described=_described())
    written = AsyncMock()
    monkeypatch.setattr(mod, "_write_tenant_quota", written)

    await scale_tenant(request, "t1", TenantScaleRequest(worker_count=6),
                       user=MagicMock())

    written.assert_awaited()
    _ = SimpleNamespace


# --- storage ---------------------------------------------------------------
#
# The checkbox and the driver behind it have to travel together. The older path
# adds the CSI addon when `enable_storage` is set; the described path did not,
# so a tenant created with storage ticked got the host side — service account,
# credential, quota — and no driver, and every PVC in it stayed Pending with
# nothing saying why.

def test_ticking_storage_asks_for_the_driver() -> None:
    from app.api.v1.tenants_crud import _described_addons
    from app.models.tenant import TenantCreateRequest

    req = TenantCreateRequest(
        name="t1", display_name="T1", folder="f", environment="e",
        enable_storage=True,
    )
    addons = _described_addons(req, "ceph-block")

    csi = [a for a in addons if a["id"] == "kubevirt-csi-driver"]
    assert csi, f"storage was asked for and the driver was not: {addons}"
    # The class on the host, which is what the driver provisions from. Sent
    # empty it falls back to the host default and lands tenant volumes
    # somewhere else.
    assert csi[0]["parameters"]["INFRA_STORAGE_CLASS_NAME"] == "ceph-block"


def test_without_storage_the_driver_is_not_asked_for() -> None:
    from app.api.v1.tenants_crud import _described_addons
    from app.models.tenant import TenantCreateRequest

    req = TenantCreateRequest(
        name="t1", display_name="T1", folder="f", environment="e",
    )
    assert _described_addons(req, None) == []


def test_asking_twice_adds_it_once() -> None:
    """The wizard lets the box be ticked and the addon chosen."""
    from app.api.v1.tenants_crud import _described_addons
    from app.models.tenant import TenantAddon, TenantCreateRequest

    req = TenantCreateRequest(
        name="t1", display_name="T1", folder="f", environment="e",
        enable_storage=True,
        addons=[TenantAddon(addon_id="kubevirt-csi-driver",
                            parameters={"INFRA_STORAGE_CLASS_NAME": "other"})],
    )
    addons = _described_addons(req, "ceph-block")
    csi = [a for a in addons if a["id"] == "kubevirt-csi-driver"]
    assert len(csi) == 1
    # And what the caller said wins over what was discovered.
    assert csi[0]["parameters"]["INFRA_STORAGE_CLASS_NAME"] == "other"
