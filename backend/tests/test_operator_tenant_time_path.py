"""Who writes the tenant's time source.

One of them. The per-tenant Service is harmless twice, but the chrony
Deployment is not: two renderers of one Deployment means it rolls from
whichever side wrote last, and chrony is what a joining worker asks for the
time. Rolling it during a join is a node that never appears.
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_the_backend_serves_the_time_while_the_operator_does_not():
    from app.api.v1 import tenants_crud

    server, service = AsyncMock(), AsyncMock()
    with patch.object(tenants_crud, "ensure_ntp_server", server), \
         patch.object(tenants_crud, "ensure_tenant_ntp_service", service), \
         patch.object(tenants_crud, "tenant_time_path_enabled", return_value=False):
        did = await tenants_crud._ensure_tenant_time(
            AsyncMock(), "t1", "tenant-t1", "10.199.0.100",
        )

    assert did is True
    assert server.await_count == 1
    assert service.await_count == 1


@pytest.mark.asyncio
async def test_the_backend_stands_aside_once_the_operator_has_it():
    from app.api.v1 import tenants_crud

    server, service = AsyncMock(), AsyncMock()
    with patch.object(tenants_crud, "ensure_ntp_server", server), \
         patch.object(tenants_crud, "ensure_tenant_ntp_service", service), \
         patch.object(tenants_crud, "tenant_time_path_enabled", return_value=True):
        did = await tenants_crud._ensure_tenant_time(
            AsyncMock(), "t1", "tenant-t1", "10.199.0.100",
        )

    assert did is False
    assert server.await_count == 0, "two renderers of the chrony Deployment"
    assert service.await_count == 0


@pytest.mark.asyncio
async def test_a_tenant_without_an_address_still_gets_the_shared_server():
    """The Service needs an address to sit on; the Deployment does not, and
    every other tenant depends on it existing."""
    from app.api.v1 import tenants_crud

    server, service = AsyncMock(), AsyncMock()
    with patch.object(tenants_crud, "ensure_ntp_server", server), \
         patch.object(tenants_crud, "ensure_tenant_ntp_service", service), \
         patch.object(tenants_crud, "tenant_time_path_enabled", return_value=False):
        await tenants_crud._ensure_tenant_time(AsyncMock(), "t1", "tenant-t1", None)

    assert server.await_count == 1
    assert service.await_count == 0


def test_the_create_path_still_calls_it():
    """A guard nobody calls is a guard nobody has."""
    import inspect

    from app.api.v1 import tenants_crud

    source = inspect.getsource(tenants_crud)
    assert "_ensure_tenant_time(k8s, req.name, ns, talos_vip)" in source
    # And the writes it guards happen nowhere else on the create path.
    for call in ("await ensure_ntp_server(", "await ensure_tenant_ntp_service("):
        assert source.count(call) == 1, (
            f"{call} is called from more than one place, so the flag only "
            f"covers one of them"
        )
