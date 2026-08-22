"""Enabling the CSI driver on a tenant with no storage wires the storage.

It used to refuse, and the refusal was right about the driver and wrong about
the way out.

Enabling it on a tenant created without `enable_storage` looked like it
worked. The HelmRelease went to

    Running 'install' action with timeout of 15m0s

and stayed there — waiting for host-side credentials that only creation puts
in place — while `GET /tenants/tci/storage/status` said

    phase: disabled, host_credentials_ready: false,
    "Tenant storage is not enabled — the host-side CSI credentials…"

and `POST …/storage/reconcile` answered "ensure storage was enabled at create".
The card that would have shown any of this is hidden precisely when the phase
is `disabled`.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1.tenants_crud import _tenant_storage_enabled, enable_addon
from app.models.tenant import TenantAddon


def _k8s(kvc_spec):
    k8s = MagicMock()
    k8s.custom_api.get_namespaced_custom_object = AsyncMock(
        return_value={"spec": kvc_spec},
    )
    return k8s


@pytest.mark.asyncio
class TestStorageDetection:
    async def test_the_marker_is_the_infra_cluster_secret_ref(self) -> None:
        assert await _tenant_storage_enabled(
            _k8s({"infraClusterSecretRef": {"name": "tci-infra"}}), "tci",
        )

    async def test_without_it_storage_is_off(self) -> None:
        assert not await _tenant_storage_enabled(_k8s({}), "tci")

    async def test_a_missing_cluster_is_not_a_crash(self) -> None:
        k8s = MagicMock()
        k8s.custom_api.get_namespaced_custom_object = AsyncMock(
            side_effect=RuntimeError("gone"),
        )
        assert not await _tenant_storage_enabled(k8s, "tci")


@pytest.mark.asyncio
async def test_enabling_the_driver_wires_the_storage_it_needs(monkeypatch) -> None:
    """The refusal it replaces named the right hazard and the wrong remedy.

    "Recreate the tenant with storage enabled" was the only path there was:
    `storage/reconcile` copies a credential that does not exist yet, and nothing
    creates one after the tenant is built. So the host side is created here, and
    the guarantee the refusal protected is kept by ordering — the driver is
    enabled after the thing it talks to exists.
    """
    import app.api.v1.tenants_crud as mod

    k8s = _k8s({})
    request = MagicMock()
    request.app.state.k8s_client = k8s

    monkeypatch.setattr(mod, "require_tenant_access", AsyncMock())
    catalog = MagicMock()
    catalog.get_component.return_value = MagicMock(
        parameters=[], namespace=None, category="storage", required=False)
    monkeypatch.setattr(mod, "_get_addon_catalog", AsyncMock(return_value=catalog))
    monkeypatch.setattr(mod, "tenant_addons_path_enabled", lambda: False)

    order: list[str] = []
    monkeypatch.setattr(mod, "_wire_tenant_storage",
                        AsyncMock(side_effect=lambda *_a: order.append("wired")))

    async def created(**_kw):
        order.append("enabled")
        return {}

    k8s.custom_api.create_namespaced_custom_object = AsyncMock(side_effect=created)
    k8s.custom_api.get_namespaced_custom_object = AsyncMock(
        return_value={"spec": {"values": {"namespaces": []}}})
    k8s.custom_api.patch_namespaced_custom_object = AsyncMock()

    await enable_addon(
        request, "tci", TenantAddon(addon_id="kubevirt-csi-driver"), user=MagicMock(),
    )

    assert order == ["wired", "enabled"], (
        "the driver was enabled before it had anything to talk to"
    )


@pytest.mark.asyncio
async def test_a_tenant_that_already_has_storage_is_not_rewired(monkeypatch) -> None:
    import app.api.v1.tenants_crud as mod

    k8s = _k8s({"infraClusterSecretRef": {"name": "capk-infra-credentials"}})
    request = MagicMock()
    request.app.state.k8s_client = k8s

    monkeypatch.setattr(mod, "require_tenant_access", AsyncMock())
    catalog = MagicMock()
    catalog.get_component.return_value = MagicMock(
        parameters=[], namespace=None, category="storage", required=False)
    monkeypatch.setattr(mod, "_get_addon_catalog", AsyncMock(return_value=catalog))
    monkeypatch.setattr(mod, "tenant_addons_path_enabled", lambda: False)
    wired = AsyncMock()
    monkeypatch.setattr(mod, "_wire_tenant_storage", wired)

    # Answering per object: the storage check reads the KubevirtCluster and the
    # namespace list reads a HelmRelease, and one mock for both made the check
    # read the wrong thing — which is how this test first "passed" the wrong way.
    async def get_object(**kw):
        if kw.get("plural") == "kubevirtclusters":
            return {"spec": {"infraClusterSecretRef": {
                "name": "capk-infra-credentials"}}}
        return {"spec": {"values": {"namespaces": []}}}

    k8s.custom_api.get_namespaced_custom_object = AsyncMock(side_effect=get_object)
    k8s.custom_api.create_namespaced_custom_object = AsyncMock(return_value={})
    k8s.custom_api.patch_namespaced_custom_object = AsyncMock()

    await enable_addon(
        request, "tci", TenantAddon(addon_id="kubevirt-csi-driver"), user=MagicMock(),
    )

    wired.assert_not_awaited()
