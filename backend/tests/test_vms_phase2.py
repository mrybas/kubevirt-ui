"""Phase 2 VM endpoint tests — synthetic naming (generateName) + display_name.

Covers:
- POST /vms: uses `generateName`, stamps display-name annotation and slug label.
- GET /vms: exposes display_name; supports `search` substring filter.
- GET /vms/{name}: returns display_name and falls back to metadata.name.
- PATCH /vms/{name}/display-name: updates annotation + label, leaves name unchanged.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


def _vm(
    name: str,
    namespace: str = "default",
    display_name: str | None = None,
    extra_labels: dict[str, str] | None = None,
    extra_annotations: dict[str, str] | None = None,
) -> dict[str, Any]:
    annotations = dict(extra_annotations or {})
    if display_name is not None:
        annotations["kubevirt-ui.io/display-name"] = display_name
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "creationTimestamp": "2026-01-18T10:00:00Z",
            "labels": dict(extra_labels or {}),
            "annotations": annotations,
        },
        "spec": {
            "running": False,
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
        "status": {"printableStatus": "Stopped", "ready": False, "conditions": []},
    }


# --- POST /vms (create with generateName) ---------------------------------


def test_create_vm_uses_generate_name(client: TestClient, mock_k8s_client: MagicMock) -> None:
    """create_vm should send no metadata.name; only generateName + display-name annotation."""
    captured_body: dict[str, Any] = {}

    async def _create(*, namespace: str, body: dict) -> dict:  # noqa: ARG001
        captured_body.update(body)
        # K8s assigns metadata.name from generateName + random suffix
        returned = dict(body)
        returned["metadata"] = dict(body["metadata"])
        returned["metadata"]["name"] = body["metadata"]["generateName"] + "abc12"
        return returned

    mock_k8s_client.create_virtual_machine = AsyncMock(side_effect=_create)

    resp = client.post(
        "/api/v1/namespaces/default/vms",
        json={
            "display_name": "My Production DB!",
            "cpu_cores": 2,
            "memory": "2Gi",
            "disk_size": "10Gi",
            "image": "quay.io/kubevirt/cirros-container-disk-demo",
        },
    )
    assert resp.status_code == 201, resp.text
    md = captured_body["metadata"]
    # No metadata.name — server-side assigned
    assert "name" not in md
    assert md["generateName"].startswith("my-production-db")
    # generateName must end with "-" so the K8s suffix is appended cleanly
    assert md["generateName"].endswith("-")
    # display-name annotation preserved verbatim
    assert md["annotations"]["kubevirt-ui.io/display-name"] == "My Production DB!"
    # slug label derived from display name (sanitized)
    assert md["labels"]["kubevirt-ui.io/slug"].startswith("my-production-db")
    # Response carries display_name back to the client
    body = resp.json()
    assert body["display_name"] == "My Production DB!"
    assert body["name"].startswith("my-production-db")


def test_create_vm_rejects_blank_display_name(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/namespaces/default/vms",
        json={
            "display_name": "",
            "cpu_cores": 1,
            "memory": "1Gi",
            "disk_size": "1Gi",
            "image": "x",
        },
    )
    assert resp.status_code == 422


# --- GET /vms (list with display_name + search) ----------------------------


def test_list_vms_exposes_display_name(client: TestClient, mock_k8s_client: MagicMock) -> None:
    mock_k8s_client.list_virtual_machines.return_value = [
        _vm("alpha-abc12", display_name="Alpha Server"),
    ]
    resp = client.get("/api/v1/namespaces/default/vms")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "alpha-abc12"
    assert items[0]["display_name"] == "Alpha Server"


def test_list_vms_display_name_falls_back_to_metadata_name(
    client: TestClient, mock_k8s_client: MagicMock
) -> None:
    """When the display-name annotation is absent, fall back to metadata.name."""
    mock_k8s_client.list_virtual_machines.return_value = [_vm("legacy-vm")]
    resp = client.get("/api/v1/namespaces/default/vms")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["display_name"] == "legacy-vm"


def test_list_vms_search_substring_case_insensitive(
    client: TestClient, mock_k8s_client: MagicMock
) -> None:
    mock_k8s_client.list_virtual_machines.return_value = [
        _vm("alpha-1", display_name="Alpha Server"),
        _vm("beta-1", display_name="Beta Worker"),
        _vm("alpha-2", display_name="Alphabet Soup"),
    ]
    resp = client.get("/api/v1/namespaces/default/vms?search=alpha")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert {i["display_name"] for i in data["items"]} == {"Alpha Server", "Alphabet Soup"}

    # Case-insensitive substring filter
    resp2 = client.get("/api/v1/namespaces/default/vms?search=WORKER")
    assert resp2.status_code == 200
    items2 = resp2.json()["items"]
    assert [i["display_name"] for i in items2] == ["Beta Worker"]


def test_list_vms_search_paginates_filtered_set(
    client: TestClient, mock_k8s_client: MagicMock
) -> None:
    """Pagination totals reflect the filtered set, not the raw list size."""
    vms = [_vm(f"alpha-{i}", display_name=f"Alpha {i}") for i in range(5)]
    vms.extend(_vm(f"beta-{i}", display_name=f"Beta {i}") for i in range(5))
    mock_k8s_client.list_virtual_machines.return_value = vms

    resp = client.get("/api/v1/namespaces/default/vms?search=alpha&page=1&per_page=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert data["pages"] == 3
    assert len(data["items"]) == 2


# --- GET /vms/{name} ------------------------------------------------------


def test_get_vm_exposes_display_name(client: TestClient, mock_k8s_client: MagicMock) -> None:
    mock_k8s_client.get_virtual_machine = AsyncMock(
        return_value=_vm("alpha-abc12", display_name="Alpha")
    )
    from kubernetes_asyncio.client.exceptions import ApiException

    mock_k8s_client.get_virtual_machine_instance = AsyncMock(
        side_effect=ApiException(status=404, reason="VMI not found")
    )
    resp = client.get("/api/v1/namespaces/default/vms/alpha-abc12")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "alpha-abc12"
    assert body["display_name"] == "Alpha"


# --- PATCH /vms/{name}/display-name ---------------------------------------


def test_patch_display_name_updates_annotation_and_label(
    client: TestClient, mock_k8s_client: MagicMock
) -> None:
    captured: dict[str, Any] = {}

    async def _patch(
        *,
        group: str,  # noqa: ARG001
        version: str,  # noqa: ARG001
        namespace: str,  # noqa: ARG001
        plural: str,  # noqa: ARG001
        name: str,
        body: dict,
        _content_type: str,
    ) -> dict:
        captured["body"] = body
        captured["content_type"] = _content_type
        captured["name"] = name
        # Return the VM with new display name applied
        return _vm(
            name, display_name=body["metadata"]["annotations"]["kubevirt-ui.io/display-name"]
        )

    from kubernetes_asyncio.client.exceptions import ApiException

    mock_k8s_client.get_virtual_machine_instance = AsyncMock(
        side_effect=ApiException(status=404, reason="VMI not found")
    )

    with patch("app.api.v1.vms.client.CustomObjectsApi") as api_cls:
        api = MagicMock()
        api.patch_namespaced_custom_object = AsyncMock(side_effect=_patch)
        api_cls.return_value = api

        resp = client.patch(
            "/api/v1/namespaces/default/vms/alpha-abc12/display-name",
            json={"display_name": "Production WebApp"},
        )

    assert resp.status_code == 200, resp.text
    # The K8s resource identity is unchanged
    assert captured["name"] == "alpha-abc12"
    # Merge patch with annotations + labels only
    body = captured["body"]
    assert body["metadata"]["annotations"]["kubevirt-ui.io/display-name"] == "Production WebApp"
    slug = body["metadata"]["labels"]["kubevirt-ui.io/slug"]
    assert slug.startswith("production-webapp")
    # Merge-patch content type — not strategic-merge / JSON-patch
    assert captured["content_type"] == "application/merge-patch+json"
    # Response echoes new display_name
    assert resp.json()["display_name"] == "Production WebApp"


def test_patch_display_name_returns_404_for_missing_vm(
    client: TestClient,
) -> None:
    from kubernetes_asyncio.client.exceptions import ApiException

    with patch("app.api.v1.vms.client.CustomObjectsApi") as api_cls:
        api = MagicMock()
        api.patch_namespaced_custom_object = AsyncMock(
            side_effect=ApiException(status=404, reason="Not Found")
        )
        api_cls.return_value = api

        resp = client.patch(
            "/api/v1/namespaces/default/vms/nope/display-name",
            json={"display_name": "Anything"},
        )

    assert resp.status_code == 404


def test_patch_display_name_rejects_blank(client: TestClient) -> None:
    resp = client.patch(
        "/api/v1/namespaces/default/vms/alpha-abc12/display-name",
        json={"display_name": ""},
    )
    assert resp.status_code == 422


# --- POST /vms/from-template (synthetic naming) ---------------------------


def _template_configmap() -> Any:
    """Build a minimal ConfigMap-like mock returning one template."""
    import json as _json

    template = {
        "compute": {"cpu_cores": 1, "vcpu": 1, "cpu_sockets": 1, "cpu_threads": 1},
        "disk": {"size": "10Gi"},
        "network": {"type": "default"},
        "console": {"vnc_enabled": True, "serial_console_enabled": False},
        "golden_image_name": "ubuntu-2204",
        "golden_image_namespace": "golden-images",
        "cloud_init": {},
    }
    cm = MagicMock()
    cm.data = {"ubuntu-2204": _json.dumps(template)}
    return cm


def test_create_vm_from_template_uses_generate_name(
    client: TestClient, mock_k8s_client: MagicMock
) -> None:
    """from-template should use generateName + stamp display-name; one POST, no runStrategy patch."""
    mock_k8s_client.core_api.read_namespaced_config_map = AsyncMock(
        return_value=_template_configmap()
    )
    mock_k8s_client.core_api.read_namespace = AsyncMock(
        side_effect=Exception("ns labels not needed for this test")
    )

    captured_create: dict[str, Any] = {}

    async def _create(*, group: str, version: str, namespace: str, plural: str, body: dict) -> dict:  # noqa: ARG001
        captured_create.update(body)
        body = dict(body)
        body["metadata"] = dict(body["metadata"])
        body["metadata"]["name"] = body["metadata"]["generateName"] + "xyz12"
        return body

    patch_mock = AsyncMock()

    with (
        patch("app.api.v1.vms.client.CustomObjectsApi") as api_cls,
        patch("app.api.v1.profile.get_user_ssh_keys", new=AsyncMock(return_value=[])),
        patch(
            "app.api.v1.vms.get_cluster_settings",
            new=AsyncMock(return_value=MagicMock(cpu_overcommit=1)),
        ),
    ):
        api = MagicMock()
        api.create_namespaced_custom_object = AsyncMock(side_effect=_create)
        api.patch_namespaced_custom_object = patch_mock
        api_cls.return_value = api

        resp = client.post(
            "/api/v1/namespaces/default/vms/from-template",
            json={
                "display_name": "My App",
                "template_name": "ubuntu-2204",
                "start": True,
            },
        )

    assert resp.status_code == 201, resp.text

    # Create call: generateName set, no metadata.name, display-name annotation present
    md = captured_create["metadata"]
    assert "name" not in md
    assert md["generateName"] == "my-app-"
    assert md["annotations"]["kubevirt-ui.io/display-name"] == "My App"
    assert md["labels"]["kubevirt-ui.io/slug"] == "my-app"

    # dataVolumeTemplates: literal DV name, slug-prefixed with unique suffix; no
    # kubevirt-ui.io/vm label — DV ownership is tracked via ownerReferences.
    dvts = captured_create["spec"]["dataVolumeTemplates"]
    assert len(dvts) == 1
    dv_meta = dvts[0]["metadata"]
    assert dv_meta["name"].startswith("my-app-root-")
    assert len(dv_meta["name"]) == len("my-app-root-") + 6  # 6-char hex suffix
    assert "kubevirt-ui.io/vm" not in dv_meta["labels"]

    # VM volumes reference the DV by its literal name
    volumes = captured_create["spec"]["template"]["spec"]["volumes"]
    root_vol = next(v for v in volumes if v["name"] == "rootdisk")
    assert root_vol["dataVolume"]["name"] == dv_meta["name"]

    # runStrategy is set to the requested value directly — no Halted→Always trick
    assert captured_create["spec"]["runStrategy"] == "Always"

    # No kubevirt.io/domain on the template — KubeVirt stamps launcher pods with
    # vm.kubevirt.io/name automatically; list_vms uses that label instead.
    tpl_meta = captured_create["spec"]["template"].get("metadata", {})
    assert "kubevirt.io/domain" not in tpl_meta.get("labels", {})

    # The only post-create patch is the vm-name label stamp (the label is
    # unique per VM and backup/schedule targeting selects on it; it can only
    # be applied once the server has assigned the generateName suffix). The
    # old Halted→Always runStrategy patch is gone — assert that specifically,
    # since that's what this test guards.
    patch_mock.assert_awaited_once()
    assert patch_mock.await_args.kwargs["body"] == {
        "metadata": {"labels": {"kubevirt-ui.io/vm-name": "my-app-xyz12"}},
    }


def test_create_vm_from_template_rejects_blank_display_name(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/namespaces/default/vms/from-template",
        json={"display_name": "", "template_name": "ubuntu-2204"},
    )
    assert resp.status_code == 422


def test_create_vm_from_template_returns_404_for_missing_template(
    client: TestClient, mock_k8s_client: MagicMock
) -> None:
    """If template ConfigMap has no entry for the requested template_name, return 404."""
    cm = MagicMock()
    cm.data = {"other-template": "{}"}
    mock_k8s_client.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)

    resp = client.post(
        "/api/v1/namespaces/default/vms/from-template",
        json={"display_name": "My App", "template_name": "ubuntu-2204"},
    )
    assert resp.status_code == 404


def test_create_vm_from_template_halted_when_start_false(
    client: TestClient, mock_k8s_client: MagicMock
) -> None:
    """When start=False, runStrategy is set to Halted directly at create-time."""
    mock_k8s_client.core_api.read_namespaced_config_map = AsyncMock(
        return_value=_template_configmap()
    )
    mock_k8s_client.core_api.read_namespace = AsyncMock(side_effect=Exception("skip"))

    captured_create: dict[str, Any] = {}

    async def _create(*, group: str, version: str, namespace: str, plural: str, body: dict) -> dict:  # noqa: ARG001
        captured_create.update(body)
        body = dict(body)
        body["metadata"] = dict(body["metadata"])
        body["metadata"]["name"] = body["metadata"]["generateName"] + "abc12"
        return body

    with (
        patch("app.api.v1.vms.client.CustomObjectsApi") as api_cls,
        patch("app.api.v1.profile.get_user_ssh_keys", new=AsyncMock(return_value=[])),
        patch(
            "app.api.v1.vms.get_cluster_settings",
            new=AsyncMock(return_value=MagicMock(cpu_overcommit=1)),
        ),
    ):
        api = MagicMock()
        api.create_namespaced_custom_object = AsyncMock(side_effect=_create)
        api_cls.return_value = api

        resp = client.post(
            "/api/v1/namespaces/default/vms/from-template",
            json={
                "display_name": "My App",
                "template_name": "ubuntu-2204",
                "start": False,
            },
        )

    assert resp.status_code == 201
    assert captured_create["spec"]["runStrategy"] == "Halted"


# --- POST /vms/{name}/snapshots (VM snapshot) -----------------------------


def test_create_vm_snapshot_uses_generate_name(client: TestClient) -> None:
    """Snapshot create uses generateName + display-name annotation."""
    captured: dict[str, Any] = {}

    async def _create(*, group: str, version: str, namespace: str, plural: str, body: dict) -> dict:  # noqa: ARG001
        captured.update(body)
        body = dict(body)
        body["metadata"] = dict(body["metadata"])
        body["metadata"]["name"] = body["metadata"]["generateName"] + "k7n2x"
        body["metadata"]["creationTimestamp"] = "2026-05-16T10:00:00Z"
        return body

    with patch("app.api.v1.vm_snapshots.client.CustomObjectsApi") as api_cls:
        api = MagicMock()
        api.create_namespaced_custom_object = AsyncMock(side_effect=_create)
        api_cls.return_value = api

        resp = client.post(
            "/api/v1/namespaces/default/vms/myvm/snapshots",
            json={"display_name": "Before Upgrade"},
        )

    assert resp.status_code == 201, resp.text
    md = captured["metadata"]
    assert "name" not in md
    assert md["generateName"] == "before-upgrade-"
    assert md["annotations"]["kubevirt-ui.io/display-name"] == "Before Upgrade"
    assert md["labels"]["kubevirt-ui.io/slug"] == "before-upgrade"
    body = resp.json()
    assert body["display_name"] == "Before Upgrade"
    assert body["name"].startswith("before-upgrade-")


def test_create_vm_snapshot_rejects_blank(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/namespaces/default/vms/myvm/snapshots",
        json={"display_name": ""},
    )
    assert resp.status_code == 422


def test_list_vm_snapshots_populates_display_name(client: TestClient) -> None:
    """list_vm_snapshots reads display-name annotation, falls back to metadata.name."""
    with patch("app.api.v1.vm_snapshots.client.CustomObjectsApi") as api_cls:
        api = MagicMock()
        api.list_namespaced_custom_object = AsyncMock(
            return_value={
                "items": [
                    {
                        "metadata": {
                            "name": "snap-xyz12",
                            "annotations": {"kubevirt-ui.io/display-name": "Pre-Patch"},
                            "creationTimestamp": "2026-05-16T09:00:00Z",
                        },
                        "spec": {"source": {"kind": "VirtualMachine", "name": "myvm"}},
                        "status": {"phase": "Succeeded", "readyToUse": True},
                    },
                    {
                        "metadata": {
                            "name": "legacy-snap",
                            "creationTimestamp": "2026-05-16T08:00:00Z",
                        },
                        "spec": {"source": {"kind": "VirtualMachine", "name": "myvm"}},
                        "status": {"phase": "Succeeded", "readyToUse": True},
                    },
                ]
            }
        )
        api_cls.return_value = api

        resp = client.get("/api/v1/namespaces/default/vms/myvm/snapshots")

    assert resp.status_code == 200
    items = resp.json()
    by_name = {s["name"]: s for s in items}
    assert by_name["snap-xyz12"]["display_name"] == "Pre-Patch"
    assert by_name["legacy-snap"]["display_name"] == "legacy-snap"  # fallback


# --- POST /vms/{name}/clone -----------------------------------------------


def _source_vm_for_clone() -> dict[str, Any]:
    return {
        "metadata": {
            "name": "source-vm-abc12",
            "namespace": "default",
            "labels": {
                "kubevirt.io/vm": "source-vm",
                "kubevirt-ui.io/managed": "true",
            },
        },
        "spec": {
            "runStrategy": "Always",
            "dataVolumeTemplates": [
                {"metadata": {"name": "source-vm-abc12-root-aaa111"}, "spec": {}},
            ],
            "template": {
                "spec": {
                    "domain": {"devices": {"disks": []}},
                    "volumes": [
                        {
                            "name": "rootdisk",
                            "dataVolume": {"name": "source-vm-abc12-root-aaa111"},
                        }
                    ],
                }
            },
        },
    }


def test_clone_vm_uses_generate_name_and_renames_dvs(client: TestClient) -> None:
    """Clone uses generateName for the new VM and gives DVs fresh unique names."""
    captured_create: dict[str, Any] = {}

    async def _get(*, group: str, version: str, namespace: str, plural: str, name: str) -> dict:  # noqa: ARG001
        return _source_vm_for_clone()

    async def _create(*, group: str, version: str, namespace: str, plural: str, body: dict) -> dict:  # noqa: ARG001
        captured_create.update(body)
        body = dict(body)
        body["metadata"] = dict(body["metadata"])
        body["metadata"]["name"] = body["metadata"]["generateName"] + "q9z3l"
        return body

    with patch("app.api.v1.vm_actions.client.CustomObjectsApi") as api_cls:
        api = MagicMock()
        api.get_namespaced_custom_object = AsyncMock(side_effect=_get)
        api.create_namespaced_custom_object = AsyncMock(side_effect=_create)
        api_cls.return_value = api

        resp = client.post(
            "/api/v1/namespaces/default/vms/source-vm-abc12/clone",
            json={"display_name": "Clone of Source", "start": False},
        )

    assert resp.status_code == 201, resp.text
    md = captured_create["metadata"]
    assert "name" not in md
    assert md["generateName"] == "clone-of-source-"
    assert md["annotations"]["kubevirt-ui.io/display-name"] == "Clone of Source"
    assert md["annotations"]["kubevirt-ui.io/cloned-from"] == "default/source-vm-abc12"

    # DV got a fresh unique name keyed off the clone's slug (not the source's name)
    dvts = captured_create["spec"]["dataVolumeTemplates"]
    new_dv_name = dvts[0]["metadata"]["name"]
    assert new_dv_name.startswith("clone-of-source-disk0-")
    assert "source-vm-abc12" not in new_dv_name

    # Volume reference rewritten to match the new DV name
    vol = captured_create["spec"]["template"]["spec"]["volumes"][0]
    assert vol["dataVolume"]["name"] == new_dv_name

    body = resp.json()
    assert body["clone_display_name"] == "Clone of Source"
    assert body["clone"].endswith("clone-of-source-q9z3l")


def test_clone_vm_rejects_blank_display_name(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/namespaces/default/vms/source-vm-abc12/clone",
        json={"display_name": ""},
    )
    assert resp.status_code == 422


# --- POST /schedules (CronJob) --------------------------------------------


def test_create_schedule_uses_generate_name(client: TestClient) -> None:
    captured: dict[str, Any] = {}

    async def _create(*, namespace: str, body: dict) -> Any:  # noqa: ARG001
        captured.update(body)
        result = MagicMock()
        result.metadata.name = body["metadata"]["generateName"] + "m4t7c"
        result.metadata.namespace = namespace
        return result

    with patch("app.api.v1.schedules.client.BatchV1Api") as api_cls:
        api = MagicMock()
        api.create_namespaced_cron_job = AsyncMock(side_effect=_create)
        api_cls.return_value = api

        resp = client.post(
            "/api/v1/namespaces/default/schedules",
            json={
                "display_name": "Daily Auto-Stop",
                "action": "stop",
                "schedule": "0 18 * * *",
                "vm_name": "myvm",
                "vm_namespace": "default",
            },
        )

    assert resp.status_code == 201, resp.text
    md = captured["metadata"]
    assert "name" not in md
    assert md["generateName"] == "daily-auto-stop-"
    assert md["annotations"]["kubevirt-ui.io/display-name"] == "Daily Auto-Stop"
    body = resp.json()
    assert body["display_name"] == "Daily Auto-Stop"
    assert body["name"] == "daily-auto-stop-m4t7c"


def test_create_schedule_rejects_blank_display_name(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/namespaces/default/schedules",
        json={
            "display_name": "",
            "action": "stop",
            "schedule": "0 18 * * *",
            "vm_name": "myvm",
            "vm_namespace": "default",
        },
    )
    assert resp.status_code == 422
