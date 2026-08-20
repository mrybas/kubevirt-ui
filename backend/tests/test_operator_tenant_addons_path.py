"""Who writes a tenant's addon releases.

One of them. There were two renderers of this object and they did not agree:
the repair path omits `install.disableWait`, whose absence wedges a CNI release
in `uninstalling` for ever, and it fires exactly when a release is missing —
the state a fresh tenant is in.
"""

import inspect
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_the_create_path_writes_while_the_operator_does_not():
    from app.api.v1 import tenants_crud

    write = AsyncMock()
    with patch.object(tenants_crud, "_create_addon_resources", write), \
         patch.object(tenants_crud, "tenant_addons_path_enabled", return_value=False):
        did = await tenants_crud._create_tenant_addons(
            AsyncMock(), "t1", [], object(),
        )

    assert did is True
    assert write.await_count == 1


@pytest.mark.asyncio
async def test_the_create_path_stands_aside_once_the_operator_has_it():
    from app.api.v1 import tenants_crud

    write = AsyncMock()
    with patch.object(tenants_crud, "_create_addon_resources", write), \
         patch.object(tenants_crud, "tenant_addons_path_enabled", return_value=True):
        did = await tenants_crud._create_tenant_addons(
            AsyncMock(), "t1", [], object(),
        )

    assert did is False
    assert write.await_count == 0, "two renderers of one HelmRelease"


def test_the_repair_path_is_gated_too():
    """The create path is the easy half. The repair is the one that renders the
    release without `disableWait`, and it is the one that fires on a fresh
    tenant."""
    from app.core import tenant_reconciler

    source = inspect.getsource(tenant_reconciler._reconcile_tenant)
    assert "tenant_addons_path_enabled()" in source
    assert source.index("tenant_addons_path_enabled()") < source.index(
        "_create_required_addon("
    ), "the guard has to come before the write it guards"


def test_both_call_sites_are_covered():
    """A flag that covers one of two writers is worse than no flag: it reads as
    handed over while the other one keeps writing."""
    import app.api.v1.tenants_crud as crud
    import app.core.tenant_reconciler as reconciler

    assert inspect.getsource(crud).count("await _create_addon_resources(") == 1
    assert inspect.getsource(reconciler).count("await _create_required_addon(") == 1
