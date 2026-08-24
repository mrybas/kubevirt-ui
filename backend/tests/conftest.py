"""Pytest fixtures."""

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth import (
    User,
    require_auth,
    require_env_admin,
    require_env_member,
    require_env_viewer,
    require_folder_admin,
    require_folder_member,
    require_folder_viewer,
)
from app.main import app


@pytest.fixture(autouse=True)
def authenticated_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the suite as if authentication is on.

    `is_admin` short-circuits to True when AUTH_TYPE=none — auth off means auth
    off — and AUTH_TYPE defaults to "none" when the env var is unset. Without
    this the authorization tests pass under docker-compose (which sets
    AUTH_TYPE=oidc) and fail under a bare `pytest`, which is a miserable thing
    to debug. Tests that exercise the auth-disabled path monkeypatch it back.
    """
    monkeypatch.setattr("app.core.auth.AUTH_TYPE", "oidc")


@pytest.fixture
def mock_k8s_client() -> MagicMock:
    """Create a mock Kubernetes client."""
    mock = MagicMock()
    mock.check_connectivity = AsyncMock(return_value=True)
    mock.list_virtual_machines = AsyncMock(return_value=[])
    mock.list_virtual_machine_instances = AsyncMock(return_value=[])
    # Default namespace listing — provides one enabled namespace so
    # require_auth + get_user_namespaces returns ["default"] for admin users.
    mock.list_namespaces = AsyncMock(
        return_value=[
            {"name": "default", "status": "Active", "labels": {"kubevirt-ui.io/enabled": "true"}},
        ]
    )
    mock.list_nodes = AsyncMock(return_value=[])
    mock.get_kubevirt_status = AsyncMock(
        return_value={"installed": True, "phase": "Deployed"}
    )
    mock.get_cdi_status = AsyncMock(return_value={"installed": True, "phase": "Deployed"})
    # Endpoints that build a real kubernetes_asyncio API object off the raw
    # client (`client.CustomObjectsApi(k8s._api_client)`) end up calling
    # `call_api` — a plain MagicMock there is not awaitable, so the generated
    # method blows up before the endpoint's own logic is exercised.
    mock._api_client.call_api = AsyncMock(return_value={})
    return mock


@pytest.fixture
def mock_vm_cache(mock_k8s_client: MagicMock) -> MagicMock:
    """VM cache mock — delegates to ``mock_k8s_client.list_virtual_machines``.

    Lets endpoint tests configure VMs via the same `list_virtual_machines` mock
    they used pre-cache.
    """
    cache = MagicMock()

    async def _list(ns: str) -> list:
        return await mock_k8s_client.list_virtual_machines(namespace=ns)

    cache.list_vms = AsyncMock(side_effect=_list)
    cache.close = AsyncMock()
    return cache


@pytest.fixture
def fake_user() -> User:
    """Default authenticated user for endpoint tests."""
    return User(
        id="testuser",
        email="testuser@local",
        username="testuser",
        groups=["kubevirt-ui-admins"],
    )


@pytest.fixture
def client(
    mock_k8s_client: MagicMock, mock_vm_cache: MagicMock, fake_user: User,
) -> Iterator[TestClient]:
    """Create a test client with mocked K8s client, VM cache, and auth bypass.

    Overrides every Phase 1 + Phase 2 auth dep to short-circuit to
    `fake_user`.  The Phase 2 deps (`require_env_*`, `require_folder_*`)
    are `@cache`-decorated factories — calling them in conftest returns
    the *same* closure that the routes registered, so the override key
    matches by identity.
    """
    app.state.k8s_client = mock_k8s_client
    app.state.vm_cache = mock_vm_cache

    async def _return_fake_user() -> User:
        return fake_user

    # Phase 1 + Phase 2 deps that gate route access.  Each maps to the
    # same fake_user — tests that need a non-admin user override
    # `fake_user` itself, not the deps.
    overrides = {
        require_auth: _return_fake_user,
        require_folder_admin(): _return_fake_user,
        require_folder_member(): _return_fake_user,
        require_folder_viewer(): _return_fake_user,
        require_env_admin(): _return_fake_user,
        require_env_member(): _return_fake_user,
        require_env_viewer(): _return_fake_user,
    }
    for key, fn in overrides.items():
        app.dependency_overrides[key] = fn
    try:
        yield TestClient(app)
    finally:
        for key in overrides:
            app.dependency_overrides.pop(key, None)


@pytest.fixture(autouse=True)
def _no_template_resources(monkeypatch):
    """A cluster with no ManagedVMTemplates, unless a test says otherwise.

    Every path that names a template now looks in both stores — that is the fix
    for a template written as a resource, shown in the list, offered by the
    wizard, and answered 404 by create-from-template. The lookup is real code on
    a mock client, so without this the tests that only ever mocked the ConfigMap
    await a MagicMock.

    Empty rather than absent on purpose: "no such resources" and "no such CRD"
    take the same path in the reader, and the tests below are about the legacy
    store either way.
    """
    import app.api.v1.templates as templates

    async def none(*_args, **_kwargs):
        return {"items": []}

    api = MagicMock()
    api.list_cluster_custom_object = AsyncMock(side_effect=none)
    monkeypatch.setattr(
        templates.client, "CustomObjectsApi", lambda *_a, **_k: api, raising=True,
    )
    return api
