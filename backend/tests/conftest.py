"""Pytest fixtures."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def mock_k8s_client() -> MagicMock:
    """Create a mock Kubernetes client."""
    mock = MagicMock()
    mock.check_connectivity = AsyncMock(return_value=True)
    mock.list_virtual_machines = AsyncMock(return_value=[])
    mock.list_virtual_machine_instances = AsyncMock(return_value=[])
    mock.list_namespaces = AsyncMock(return_value=[])
    mock.list_nodes = AsyncMock(return_value=[])
    mock.get_kubevirt_status = AsyncMock(
        return_value={"installed": True, "phase": "Deployed"}
    )
    mock.get_cdi_status = AsyncMock(return_value={"installed": True, "phase": "Deployed"})
    return mock


@pytest.fixture
def client(mock_k8s_client: MagicMock) -> TestClient:
    """Create a test client with mocked K8s client."""
    app.state.k8s_client = mock_k8s_client
    return TestClient(app)
