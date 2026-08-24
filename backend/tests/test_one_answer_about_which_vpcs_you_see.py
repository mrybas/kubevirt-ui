"""Both readers of "may this user see this VPC" give the same answer.

Run 4 on the stand: three VPCs in `poc-transit/dev`, every one of them with
`spec.namespaces: []` because nothing was attached yet. The tenant wizard
offered all three and built a tenant in one. The VM wizard said "No VPC or
external networks available for this project". One backend, one user, one set
of objects.

Underneath: `GET /vpcs` asked whether the namespaces overlapped **or** the
folder access block admitted the user; `GET /subnets` asked only the first,
and a VPC nothing is attached to yet passes neither half of it. The second
half had been added to `list_vpcs` once, with a comment describing this exact
failure, and the other reader was never told.

So the rule has one owner now, and this file measures the property that
matters — not that the function is called, but that the two paths agree.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.vpc_access import VpcFacts, facts_from_item, visible_vpcs

FOLDERS = {
    "poc-transit": {
        "access": {
            "admins": ["kv-poc-transit-admins"],
            "members": [],
            "viewers": [],
            "env_access": {"dev": {"admins": ["kv-poc-transit-dev-admins"]}},
        },
    },
}

# The three VPCs of the run, as kube-ovn holds them.
ITEMS = [
    {
        "metadata": {"name": f"uat-net-{suffix}", "labels": {
            "kubevirt-ui.io/folder": "poc-transit",
            "kubevirt-ui.io/environment": "dev",
        }},
        "spec": {"namespaces": []},
    }
    for suffix in ("vm", "t1", "t2")
]


def _user(groups: list[str]):
    return SimpleNamespace(
        email="kv-devadmin@ipa.test", username="kv-devadmin",
        groups=groups, is_admin=False,
    )


async def _visible(user, items=ITEMS, user_namespaces=("poc-transit-dev",)):
    k8s = MagicMock()
    with (
        patch("app.core.vpc_access.get_user_namespaces",
              AsyncMock(return_value=list(user_namespaces))),
        patch("app.core.vpc_access.load_folders", AsyncMock(return_value=FOLDERS)),
    ):
        return await visible_vpcs(
            k8s, user, [facts_from_item(i) for i in items], "ovn-cluster",
        )


@pytest.mark.asyncio
class TestAVpcNothingIsAttachedToYet:
    async def test_the_env_admin_sees_all_three(self) -> None:
        """The state every VPC is in for the minute after it is created."""
        seen = await _visible(_user(["kv-poc-transit-dev-admins"]))
        assert seen == {"uat-net-vm", "uat-net-t1", "uat-net-t2"}

    async def test_a_stranger_sees_none_of_them(self) -> None:
        seen = await _visible(_user(["some-other-team"]), user_namespaces=("other-dev",))
        assert seen == set()

    async def test_namespace_overlap_still_admits_on_its_own(self) -> None:
        """The original half has to keep working: a VPC in use, no folder."""
        item = {
            "metadata": {"name": "legacy-net", "labels": {}},
            "spec": {"namespaces": ["poc-transit-dev"]},
        }
        seen = await _visible(_user(["nobody"]), items=[item])
        assert seen == {"legacy-net"}

    async def test_an_unlabelled_empty_vpc_stays_admin_only(self) -> None:
        """No folder to ask about and nothing attached: nothing to grant on."""
        item = {"metadata": {"name": "orphan", "labels": {}}, "spec": {"namespaces": []}}
        seen = await _visible(_user(["kv-poc-transit-dev-admins"]), items=[item])
        assert seen == set()

    async def test_the_system_vpc_is_never_listed(self) -> None:
        item = {
            "metadata": {"name": "ovn-cluster", "labels": {}},
            "spec": {"namespaces": ["poc-transit-dev"]},
        }
        seen = await _visible(_user(["kv-poc-transit-dev-admins"]), items=[item])
        assert seen == set()

    async def test_no_folders_configured_is_not_an_error(self) -> None:
        """404 from the folders ConfigMap falls back to overlap alone."""
        from fastapi import HTTPException

        k8s = MagicMock()
        with (
            patch("app.core.vpc_access.get_user_namespaces",
                  AsyncMock(return_value=["poc-transit-dev"])),
            patch("app.core.vpc_access.load_folders",
                  AsyncMock(side_effect=HTTPException(status_code=404))),
        ):
            seen = await visible_vpcs(
                k8s, _user(["kv-poc-transit-dev-admins"]),
                [facts_from_item(i) for i in ITEMS], "ovn-cluster",
            )
        assert seen == set()

    async def test_a_broken_folder_read_is_not_read_as_you_see_nothing(self) -> None:
        from fastapi import HTTPException

        k8s = MagicMock()
        with (
            patch("app.core.vpc_access.get_user_namespaces",
                  AsyncMock(return_value=["poc-transit-dev"])),
            patch("app.core.vpc_access.load_folders",
                  AsyncMock(side_effect=HTTPException(status_code=500))),
        ):
            with pytest.raises(HTTPException):
                await visible_vpcs(
                    k8s, _user(["x"]), [VpcFacts(name="n")], "ovn-cluster",
                )


@pytest.mark.asyncio
async def test_the_subnet_reader_and_the_vpc_reader_agree() -> None:
    """The property the incident was about, measured across both call sites.

    Not "the helper is called" — the two endpoints' own filters, given the
    same cluster and the same user, admitting the same VPCs.
    """
    from app.api.v1 import network as network_mod

    user = _user(["kv-poc-transit-dev-admins"])

    k8s = MagicMock()
    k8s.custom_api.list_cluster_custom_object = AsyncMock(
        return_value={"items": ITEMS})

    with (
        patch("app.core.vpc_access.get_user_namespaces",
              AsyncMock(return_value=["poc-transit-dev"])),
        patch("app.core.vpc_access.load_folders", AsyncMock(return_value=FOLDERS)),
    ):
        through_subnets = await network_mod._user_visible_vpc_names(k8s, user)

    # What `list_vpcs` keeps, expressed the way it expresses it.
    through_vpcs = await _visible(user)

    assert through_subnets == through_vpcs == {
        "uat-net-vm", "uat-net-t1", "uat-net-t2",
    }
