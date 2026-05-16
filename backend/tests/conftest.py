"""Pytest fixtures."""

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth import User, require_auth
from app.main import app


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
    """Create a test client with mocked K8s client, VM cache, and auth bypass."""
    app.state.k8s_client = mock_k8s_client
    app.state.vm_cache = mock_vm_cache

    async def _override_require_auth() -> User:
        return fake_user

    app.dependency_overrides[require_auth] = _override_require_auth
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_auth, None)
