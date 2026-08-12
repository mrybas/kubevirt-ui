"""Unit tests for the storage-wiring status and its background completion."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client import ApiException

from app.api.v1 import tenants_storage
from app.api.v1.tenants_storage import (
    CAPK_KUBECONFIG_SECRET_NAME,
    CSI_KUBECONFIG_SECRET_NAME,
    TENANT_DRIVER_NAMESPACE,
    get_storage_status,
)


def _not_found() -> ApiException:
    return ApiException(status=404, reason="Not Found")


def _host_api(present: set[str]) -> MagicMock:
    """CoreV1Api over the host cluster where `present` secrets exist."""
    api = MagicMock()

    async def _read(name: str, namespace: str):
        if name in present:
            return MagicMock()
        raise _not_found()

    api.read_namespaced_secret = AsyncMock(side_effect=_read)
    return api


def _tenant_api(replicated: bool) -> MagicMock:
    api = MagicMock()
    api.read_namespaced_secret = AsyncMock(
        return_value=MagicMock() if replicated else None,
        side_effect=None if replicated else _not_found(),
    )
    api.api_client.close = AsyncMock()
    return api


@pytest.mark.asyncio
class TestGetStorageStatus:
    async def test_disabled_when_host_secret_missing(self) -> None:
        with patch.object(
            tenants_storage.client, "CoreV1Api", return_value=_host_api(set()),
        ):
            status = await get_storage_status(MagicMock(), "demo")

        assert status.phase == "disabled"
        assert status.host_credentials_ready is False

    async def test_pending_when_tenant_unreachable(self) -> None:
        host = _host_api({CSI_KUBECONFIG_SECRET_NAME, CAPK_KUBECONFIG_SECRET_NAME})
        with patch.object(tenants_storage.client, "CoreV1Api", return_value=host), \
             patch.object(
                 tenants_storage, "_build_tenant_core_api", AsyncMock(return_value=None),
             ):
            status = await get_storage_status(MagicMock(), "demo")

        assert status.phase == "pending"
        assert status.host_credentials_ready is True
        assert status.capk_credentials_ready is True
        assert status.tenant_reachable is False

    async def test_pending_when_tenant_up_but_secret_absent(self) -> None:
        host = _host_api({CSI_KUBECONFIG_SECRET_NAME, CAPK_KUBECONFIG_SECRET_NAME})
        tenant = _tenant_api(replicated=False)
        with patch.object(tenants_storage.client, "CoreV1Api", return_value=host), \
             patch.object(
                 tenants_storage, "_build_tenant_core_api",
                 AsyncMock(return_value=tenant),
             ):
            status = await get_storage_status(MagicMock(), "demo")

        assert status.phase == "pending"
        assert status.tenant_reachable is True
        assert status.credentials_replicated is False
        tenant.api_client.close.assert_awaited_once()

    async def test_ready_when_replicated(self) -> None:
        host = _host_api({CSI_KUBECONFIG_SECRET_NAME, CAPK_KUBECONFIG_SECRET_NAME})
        tenant = _tenant_api(replicated=True)
        with patch.object(tenants_storage.client, "CoreV1Api", return_value=host), \
             patch.object(
                 tenants_storage, "_build_tenant_core_api",
                 AsyncMock(return_value=tenant),
             ):
            status = await get_storage_status(MagicMock(), "demo")

        assert status.phase == "ready"
        assert status.credentials_replicated is True
        tenant.read_namespaced_secret.assert_awaited_once_with(
            name=CSI_KUBECONFIG_SECRET_NAME, namespace=TENANT_DRIVER_NAMESPACE,
        )

    async def test_capk_secret_reported_separately(self) -> None:
        # Tenant created before the CAPK/CSI identity split: CSI secret is
        # there, the CAPK one isn't — surfacing that is the point.
        host = _host_api({CSI_KUBECONFIG_SECRET_NAME})
        with patch.object(tenants_storage.client, "CoreV1Api", return_value=host), \
             patch.object(
                 tenants_storage, "_build_tenant_core_api", AsyncMock(return_value=None),
             ):
            status = await get_storage_status(MagicMock(), "demo")

        assert status.host_credentials_ready is True
        assert status.capk_credentials_ready is False


@pytest.mark.asyncio
class TestBackgroundRetry:
    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tenants_storage.asyncio, "sleep", AsyncMock())

    def _k8s(self, ns_exists: bool = True) -> MagicMock:
        k8s = MagicMock()
        k8s.core_api.read_namespace = AsyncMock(
            return_value=MagicMock() if ns_exists else None,
            side_effect=None if ns_exists else _not_found(),
        )
        return k8s

    async def test_stops_on_first_success(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        replicate = AsyncMock(side_effect=[False, False, True])
        monkeypatch.setattr(
            tenants_storage, "replicate_csi_credentials_to_tenant", replicate,
        )

        await tenants_storage._retry_credential_replication(self._k8s(), "demo")

        assert replicate.await_count == 3

    async def test_gives_up_after_the_configured_attempts(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        replicate = AsyncMock(return_value=False)
        monkeypatch.setattr(
            tenants_storage, "replicate_csi_credentials_to_tenant", replicate,
        )

        await tenants_storage._retry_credential_replication(self._k8s(), "demo")

        assert replicate.await_count == len(
            tenants_storage.REPLICATION_RETRY_DELAYS_SEC
        )

    async def test_abandons_when_the_tenant_is_deleted(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        replicate = AsyncMock(return_value=False)
        monkeypatch.setattr(
            tenants_storage, "replicate_csi_credentials_to_tenant", replicate,
        )

        await tenants_storage._retry_credential_replication(
            self._k8s(ns_exists=False), "demo",
        )

        replicate.assert_not_awaited()

    async def test_survives_a_raising_replication(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A transient error must not kill the loop — it retries and lands.
        replicate = AsyncMock(side_effect=[RuntimeError("boom"), True])
        monkeypatch.setattr(
            tenants_storage, "replicate_csi_credentials_to_tenant", replicate,
        )

        await tenants_storage._retry_credential_replication(self._k8s(), "demo")

        assert replicate.await_count == 2
