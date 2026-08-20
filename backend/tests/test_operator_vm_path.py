"""VMs routed through the operator instead of rendered by the handler.

The claims worth guarding are the same three as for images, and one more that
only applies to machines.

The flag switches the writer and nothing else: same request in, same response
shape out, same naming rule. Deletion and power actions follow *ownership*, not
the flag, because a machine created while the flag was on stays described by its
resource afterwards — patching the VirtualMachine directly would be reverted on
the next reconcile, so the button would appear to work and then undo itself.

And the password never travels inside the resource. A ManagedVM lands in etcd
and, for anything managed as code, in a state file.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from kubernetes_asyncio.client.exceptions import ApiException

from app.api.v1.vms import VMFromTemplateRequest, VMNetworkRequest


def _template() -> dict[str, Any]:
    return {
        "display_name": "OpDev Ubuntu",
        "os_type": "linux",
        "golden_image_name": "ubuntu-2404",
        "golden_image_namespace": "opdev-dev",
        "compute": {"cpu_cores": 2, "cpu_sockets": 1, "cpu_threads": 1, "memory": "4Gi"},
        "disk": {"size": "20Gi", "storage_class": None},
        "console": {"vnc_enabled": True, "serial_console_enabled": False},
    }


async def _create_vm(
    *,
    flag_on: bool,
    req: VMFromTemplateRequest,
    monkeypatch: pytest.MonkeyPatch,
    profile_keys: list[str] | None = None,
    create_side_effect: Any = None,
) -> tuple[dict[str, Any], Any, list[dict[str, Any]]]:
    from app.api.v1 import vms

    monkeypatch.setenv("OPERATOR_VM_ENABLED", "true" if flag_on else "")

    captured: dict[str, Any] = {}
    secrets: list[dict[str, Any]] = []

    async def _create_custom(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        if create_side_effect is not None:
            raise create_side_effect
        body = dict(kwargs["body"])
        body["metadata"] = dict(body["metadata"])
        body["metadata"]["name"] = body["metadata"].get("generateName", "") + "x7k2p"
        body["metadata"]["creationTimestamp"] = "2026-08-20T00:00:00Z"
        return body

    async def _create_secret(namespace: str, body: Any) -> Any:
        secrets.append({"namespace": namespace, "body": body})
        created = MagicMock()
        created.metadata.name = "web-01-initpw-abcde"
        return created

    api = MagicMock()
    api.create_namespaced_custom_object = AsyncMock(side_effect=_create_custom)

    k8s = MagicMock()
    ns = MagicMock()
    ns.metadata.labels = {"kubevirt-ui.io/project": "opdev"}
    k8s.core_api.read_namespace = AsyncMock(return_value=ns)
    k8s.core_api.create_namespaced_secret = AsyncMock(side_effect=_create_secret)
    k8s.core_api.patch_namespaced_secret = AsyncMock()
    k8s.get_cluster_settings = AsyncMock()

    request = MagicMock()
    request.app.state.k8s_client = k8s

    user = MagicMock()
    user.username = "kv-devadmin"
    user.email = "kv-devadmin@local"

    async def _keys(*_a: Any, **_k: Any) -> list[str]:
        return profile_keys or []

    with (
        patch.object(vms.client, "CustomObjectsApi", return_value=api),
        patch.object(vms, "_load_template", AsyncMock(return_value=_template()), create=True),
        patch("app.api.v1.profile.get_user_ssh_keys", _keys),
    ):
        # The template lookup is a ConfigMap read inside the handler; stub the
        # read itself so the test does not depend on how it is spelled.
        k8s.core_api.read_namespaced_config_map = AsyncMock(
            return_value=MagicMock(data={"opdev-ubuntu": __import__("json").dumps(_template())}),
        )
        response = await vms.create_vm_from_template(
            request=request, namespace="opdev-dev", vm_request=req, user=user,
        )
    return captured, response, secrets


def _request(**overrides: Any) -> VMFromTemplateRequest:
    base: dict[str, Any] = {
        "display_name": "Web 01",
        "template_name": "opdev-ubuntu",
        "start": True,
    }
    base.update(overrides)
    return VMFromTemplateRequest(**base)


@pytest.mark.asyncio
class TestWhichObjectGetsWritten:
    async def test_flag_on_writes_a_managed_vm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured, _, _ = await _create_vm(flag_on=True, req=_request(), monkeypatch=monkeypatch)
        assert captured["plural"] == "managedvms"
        assert captured["body"]["kind"] == "ManagedVM"

    async def test_the_naming_rule_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured, response, _ = await _create_vm(
            flag_on=True, req=_request(), monkeypatch=monkeypatch,
        )
        assert captured["body"]["metadata"]["generateName"] == "web-01-"
        assert response.name == "web-01-x7k2p"

    async def test_the_request_survives_the_translation(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        req = _request(
            cpu_cores=4,
            memory="8Gi",
            disk_size="40Gi",
            storage_class="ceph-block",
            networks=[VMNetworkRequest(subnet="team-net", static_ip="10.0.0.5")],
            network_binding="masquerade",
            start=False,
        )
        captured, _, _ = await _create_vm(flag_on=True, req=req, monkeypatch=monkeypatch)
        spec = captured["body"]["spec"]
        assert spec["templateRef"] == {"name": "opdev-ubuntu"}
        assert spec["compute"] == {"cores": 4, "memory": "8Gi"}
        assert spec["rootDisk"] == {"size": "40Gi", "storageClass": "ceph-block"}
        assert spec["networks"] == [{"subnet": "team-net", "staticIP": "10.0.0.5"}]
        assert spec["networkBinding"] == "masquerade"
        assert spec["running"] is False

    async def test_the_profile_keys_are_made_explicit(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Whose keys is a question about a person, so it is answered here.

        The handler injected them silently and installed none when the profile
        read failed — a VM with no way in, and nothing anywhere saying so. In
        the resource they are a visible list.
        """
        captured, _, _ = await _create_vm(
            flag_on=True,
            req=_request(ssh_key="ssh-ed25519 PERVM"),
            monkeypatch=monkeypatch,
            profile_keys=["ssh-rsa PROFILE"],
        )
        assert captured["body"]["spec"]["ssh"]["authorizedKeys"] == [
            "ssh-rsa PROFILE",
            "ssh-ed25519 PERVM",
        ]

    async def test_the_owner_is_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured, _, _ = await _create_vm(flag_on=True, req=_request(), monkeypatch=monkeypatch)
        annotations = captured["body"]["metadata"]["annotations"]
        assert annotations["kubevirt-ui.io/owner"] == "kv-devadmin@local"

    async def test_the_password_never_enters_the_resource(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured, _, secrets = await _create_vm(
            flag_on=True, req=_request(password="s3cret"), monkeypatch=monkeypatch,
        )
        body = __import__("json").dumps(captured["body"])
        assert "s3cret" not in body, "the password was written into the resource"
        assert captured["body"]["spec"]["initialPasswordSecretRef"] == {
            "name": "web-01-initpw-abcde", "key": "password",
        }
        assert secrets and secrets[0]["body"].string_data == {"password": "s3cret"}

    async def test_an_admission_refusal_reaches_the_user(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The webhook's message is the useful part; do not flatten it."""
        refusal = ApiException(status=403, reason="Forbidden")
        refusal.body = (
            '{"message":"admission webhook denied the request: subnet x belongs to '
            'folder poc-transit and this namespace is in folder opdev"}'
        )
        with pytest.raises(HTTPException) as caught:
            await _create_vm(
                flag_on=True, req=_request(), monkeypatch=monkeypatch,
                create_side_effect=refusal,
            )
        assert "poc-transit" in caught.value.detail

    async def test_a_missing_crd_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(HTTPException) as caught:
            await _create_vm(
                flag_on=True, req=_request(), monkeypatch=monkeypatch,
                create_side_effect=ApiException(status=404, reason="Not Found"),
            )
        assert caught.value.status_code == 503
        assert "ManagedVM CRD" in caught.value.detail


def _owned_vm(owner: str | None) -> dict[str, Any]:
    labels: dict[str, str] = {"kubevirt-ui.io/managed": "true"}
    if owner:
        labels["platform.kubevirt-ui.io/owner-kind"] = "ManagedVM"
        labels["platform.kubevirt-ui.io/owner-name"] = owner
    return {"metadata": {"name": "web-01-x7k2p", "labels": labels}}


@pytest.mark.asyncio
class TestPowerActionsFollowOwnership:
    async def _run(self, owner: str | None, action: str) -> tuple[list[str], list[dict[str, Any]]]:
        from app.api.v1 import vm_actions

        patches: list[dict[str, Any]] = []
        touched: list[str] = []

        async def _get(**kwargs: Any) -> dict[str, Any]:
            return _owned_vm(owner)

        async def _patch(**kwargs: Any) -> dict[str, Any]:
            touched.append(kwargs["plural"])
            patches.append(kwargs["body"])
            return {}

        api = MagicMock()
        api.get_namespaced_custom_object = AsyncMock(side_effect=_get)
        api.patch_namespaced_custom_object = AsyncMock(side_effect=_patch)

        k8s = MagicMock()

        async def _patch_vm(**kwargs: Any) -> dict[str, Any]:
            touched.append("virtualmachines")
            patches.append(kwargs["body"])
            return {}

        k8s.patch_virtual_machine = AsyncMock(side_effect=_patch_vm)

        request = MagicMock()
        request.app.state.k8s_client = k8s

        with patch.object(vm_actions.client, "CustomObjectsApi", return_value=api):
            if action == "start":
                await vm_actions.start_vm(
                    request=request, namespace="opdev-dev", name="web-01-x7k2p",
                    user=MagicMock(),
                )
            else:
                await vm_actions.stop_vm(
                    request=request, namespace="opdev-dev", name="web-01-x7k2p",
                    stop_request=vm_actions.StopVMRequest(), user=MagicMock(),
                )
        return touched, patches

    async def test_starting_an_owned_machine_sets_the_declared_state(self) -> None:
        touched, patches = await self._run("web-01-x7k2p", "start")
        assert touched == ["managedvms"]
        assert patches == [{"spec": {"running": True}}]

    async def test_stopping_an_owned_machine_sets_the_declared_state(self) -> None:
        touched, patches = await self._run("web-01-x7k2p", "stop")
        assert touched == ["managedvms"]
        assert patches == [{"spec": {"running": False}}]

    async def test_an_unowned_machine_keeps_the_old_path(self) -> None:
        touched, _ = await self._run(None, "start")
        assert touched == ["virtualmachines"]
