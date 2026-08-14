"""The CSI driver cannot be enabled on a tenant that has no storage.

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
async def test_enabling_the_driver_without_storage_is_refused(monkeypatch) -> None:
    import app.api.v1.tenants_crud as mod

    k8s = _k8s({})
    request = MagicMock()
    request.app.state.k8s_client = k8s

    monkeypatch.setattr(mod, "require_tenant_access", AsyncMock())
    catalog = MagicMock()
    catalog.get_component.return_value = MagicMock(parameters=[], namespace=None, category="storage")
    monkeypatch.setattr(mod, "_get_addon_catalog", AsyncMock(return_value=catalog))

    with pytest.raises(HTTPException) as e:
        await enable_addon(
            request, "tci", TenantAddon(addon_id="kubevirt-csi-driver"), user=MagicMock(),
        )
    assert e.value.status_code == 409
    assert "without storage" in e.value.detail
    assert "wait for them indefinitely" in e.value.detail
