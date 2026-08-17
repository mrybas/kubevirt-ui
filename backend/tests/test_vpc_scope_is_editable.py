"""A VPC's folder/environment was write-once, and the wizard never showed it.

Scope is chosen in the create wizard, which did not display it on the Review
step, and there was no way to change it afterwards — so a VPC created under the
wrong folder had to be deleted, with everything in it, and rebuilt. Meanwhile
the tenant-create wizard tells people to "scope a VPC to this folder", advice
whose action did not exist.

These are labels: kube-ovn does not read them, so nothing about the dataplane
moves. They decide who sees the VPC and which tenants may be placed in it — and
the tenant wizard filters on the *subnet's* copy of the pair, which is why both
have to move together.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1.vpcs import set_vpc_scope
from app.core.auth import User
from app.models.vpc import VpcScopeRequest

VPC = "t1-vpc"


def _user() -> User:
    return User(id="u1", email="a@b.c", username="admin", groups=["admins"])


def _k8s(folders: dict[str, dict] | None = None) -> MagicMock:
    vpc = {
        "metadata": {
            "name": VPC,
            "labels": {"kubevirt-ui.io/folder": "old-folder",
                       "kubevirt-ui.io/environment": "dev"},
        },
        "spec": {"namespaces": []},
        "status": {},
    }
    subnet = {
        "metadata": {
            "name": f"{VPC}-default",
            "labels": {"kubevirt-ui.io/folder": "old-folder",
                       "kubevirt-ui.io/environment": "dev"},
        },
        "spec": {"vpc": VPC, "cidrBlock": "10.200.8.0/22"},
        "status": {},
    }
    patches: list[tuple[str, str, dict]] = []

    async def get_obj(**kw):
        return vpc

    async def list_obj(**kw):
        return {"items": [subnet]} if kw["plural"] == "subnets" else {"items": [vpc]}

    async def patch_obj(**kw):
        patches.append((kw["plural"], kw["name"], kw["body"]))
        target = vpc if kw["plural"] == "vpcs" else subnet
        for k, v in kw["body"]["metadata"]["labels"].items():
            if v is None:
                target["metadata"]["labels"].pop(k, None)
            else:
                target["metadata"]["labels"][k] = v
        return target

    cm = MagicMock()
    cm.data = {"folders.yaml": ""}

    k8s = MagicMock()
    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_obj)
    k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
    k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=patch_obj)
    k8s.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)
    k8s._patches = patches
    k8s._vpc = vpc
    k8s._subnet = subnet
    return k8s


def _request(k8s: MagicMock) -> MagicMock:
    r = MagicMock()
    r.app.state.k8s_client = k8s
    return r


@pytest.fixture
def known_folders(monkeypatch):
    async def load(_k8s):
        return {"platform": {"_name": "platform"}, "old-folder": {"_name": "old-folder"}}

    monkeypatch.setattr("app.api.v1.vpcs.load_folders", load)


class TestMovingAVpc:
    @pytest.mark.asyncio
    async def test_the_vpc_and_its_subnet_move_together(self, known_folders) -> None:
        """Rescoping only the VPC hides it from the filter the scope drives."""
        k8s = _k8s()

        await set_vpc_scope(
            _request(k8s), VPC, VpcScopeRequest(folder="platform", environment="prod"), _user(),
        )

        assert k8s._vpc["metadata"]["labels"]["kubevirt-ui.io/folder"] == "platform"
        assert k8s._subnet["metadata"]["labels"]["kubevirt-ui.io/folder"] == "platform"
        assert k8s._subnet["metadata"]["labels"]["kubevirt-ui.io/environment"] == "prod"

    @pytest.mark.asyncio
    async def test_clearing_the_environment_widens_it_to_the_folder(self, known_folders) -> None:
        k8s = _k8s()

        await set_vpc_scope(
            _request(k8s), VPC, VpcScopeRequest(folder="platform"), _user(),
        )

        assert "kubevirt-ui.io/environment" not in k8s._vpc["metadata"]["labels"]
        assert "kubevirt-ui.io/environment" not in k8s._subnet["metadata"]["labels"]

    @pytest.mark.asyncio
    async def test_a_merge_patch_is_used_so_nulls_actually_delete(self, known_folders) -> None:
        """A strategic-merge patch would leave the old label in place."""
        k8s = _k8s()

        await set_vpc_scope(_request(k8s), VPC, VpcScopeRequest(folder="platform"), _user())

        for call in k8s.custom_api.patch_cluster_custom_object.await_args_list:
            assert call.kwargs["_content_type"] == "application/merge-patch+json"


class TestRefusals:
    @pytest.mark.asyncio
    async def test_an_unknown_folder_is_refused(self, known_folders) -> None:
        """Scoping into a folder that does not exist hides the VPC from everyone."""
        k8s = _k8s()

        with pytest.raises(HTTPException) as e:
            await set_vpc_scope(
                _request(k8s), VPC, VpcScopeRequest(folder="typo"), _user(),
            )

        assert e.value.status_code == 422
        assert "typo" in e.value.detail
        assert k8s._patches == [], "nothing may move while the target is wrong"

    def test_an_environment_without_a_folder_is_rejected_by_the_model(self) -> None:
        with pytest.raises(ValueError):
            VpcScopeRequest(environment="prod")
