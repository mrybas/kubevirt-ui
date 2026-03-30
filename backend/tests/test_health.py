"""Health endpoint tests."""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    """Test liveness probe."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_ready(client: TestClient, mock_k8s_client: MagicMock) -> None:
    """Test readiness probe when K8s is connected."""
    response = client.get("/health/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["kubernetes"] == "connected"


def test_health_ready_disconnected(
    client: TestClient, mock_k8s_client: MagicMock
) -> None:
    """Test readiness probe when K8s is disconnected."""
    mock_k8s_client.check_connectivity.return_value = False

    response = client.get("/health/ready")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "not ready"
