"""A cloud-init pool cannot be created any more, and says why.

The operator builds Talos workers only — `reconcileWorkers` answers
`CloudInitNotMigrated` and waits — so a cloud-init tenant created today is a
tenant whose machines never join, with the reason arriving as a condition on an
object nobody is watching. The wizard offers one kind now, and this is the other
half: a choice taken off a screen is not a choice taken out of an API.

The tenants that already run that way are untouched. Reading, scaling and
deleting them all still know how, and the build functions still have the branch
— see `TestCloudInitStillBuilds`.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1.tenants_crud import create_tenant
from app.models.tenant import TenantCreateRequest


def _request():
    request = MagicMock()
    request.app.state.k8s_client = MagicMock()
    return request


def _req(**kw):
    base = {"name": "t1", "display_name": "T1", "folder": "f", "environment": "e"}
    base.update(kw)
    return TenantCreateRequest(**base)


def test_the_default_is_talos() -> None:
    assert _req().worker_os == "talos"


@pytest.mark.asyncio
async def test_asking_for_cloud_init_is_refused_with_the_reason() -> None:
    with pytest.raises(HTTPException) as e:
        await create_tenant(_request(), _req(worker_os="cloud-init"), user=MagicMock())

    assert e.value.status_code == 422
    detail = e.value.detail
    assert "cloud-init" in detail, detail
    # Not just "invalid": the reader has to learn that the pool would sit
    # unbuilt, and that their existing tenants are fine.
    assert "unbuilt" in detail and "Existing" in detail, detail


@pytest.mark.asyncio
async def test_the_refusal_happens_before_anything_is_written(monkeypatch) -> None:
    """Refusing after the folder ceiling is charged, or after a namespace
    exists, would be a tenant half-made for a request that was never valid."""
    import app.api.v1.tenants_crud as mod

    for name in ("_create_tenant_described", "_ensure_folders_configmap"):
        monkeypatch.setattr(mod, name, AsyncMock(
            side_effect=AssertionError(f"{name} ran for a refused request")))

    with pytest.raises(HTTPException):
        await create_tenant(_request(), _req(worker_os="cloud-init"), user=MagicMock())
