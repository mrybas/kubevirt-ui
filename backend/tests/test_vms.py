"""Virtual Machine endpoint tests."""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient


def test_list_vms_empty(client: TestClient) -> None:
    """Test listing VMs when none exist."""
    response = client.get("/api/v1/namespaces/default/vms")
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0


def test_list_vms(client: TestClient, mock_k8s_client: MagicMock) -> None:
    """Test listing VMs."""
    mock_k8s_client.list_virtual_machines.return_value = [
        {
            "metadata": {
                "name": "testvm",
                "namespace": "default",
                "creationTimestamp": "2026-01-18T10:00:00Z",
                "labels": {},
                "annotations": {},
            },
            "spec": {
                "running": True,
                "template": {
                    "spec": {
                        "domain": {
                            "cpu": {"cores": 2},
                            "resources": {"requests": {"memory": "2Gi"}},
                        },
                        "volumes": [],
                    }
                },
            },
            "status": {
                "printableStatus": "Running",
                "ready": True,
                "conditions": [],
            },
        }
    ]
    mock_k8s_client.list_virtual_machine_instances.return_value = [
        {
            "metadata": {"name": "testvm", "namespace": "default"},
            "status": {
                "phase": "Running",
                "nodeName": "node-1",
                "interfaces": [{"ipAddress": "10.244.0.10"}],
            },
        }
    ]

    response = client.get("/api/v1/namespaces/default/vms")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "testvm"
    assert data["items"][0]["status"] == "Running"
    assert data["items"][0]["ip_address"] == "10.244.0.10"
    assert data["items"][0]["node"] == "node-1"


def test_create_vm_validation(client: TestClient) -> None:
    """Test VM creation with invalid name."""
    response = client.post(
        "/api/v1/namespaces/default/vms",
        json={"name": "Invalid_Name"},  # Invalid: uppercase and underscore
    )
    assert response.status_code == 422  # Validation error
