"""The VPC list must show the whole cluster, not only what the UI created.

Filtering the list by `kubevirt-ui.io/managed=true` hid every VPC made by CLI,
GitOps or a previous operator. On the lab that meant two of four VPCs were
invisible — while the Subnets tab happily listed `team-b-default` with
`VPC = team-b`, a VPC the console claimed did not exist (backlog U24).

The knock-on effects are worse than the cosmetics: the "Attach VPC" dialog on
an egress gateway is populated from this list, so it offered only the gateway's
own VPC, and the CIDR-overlap check walked the same filtered set and therefore
could not see the ranges it was supposed to protect.

Provenance is still worth showing — it just belongs in a badge, not in a
`WHERE` clause.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.vpcs import list_vpcs
from app.core.auth import User


def _vpc(name: str, managed: bool = False) -> dict:
    labels = {"kubevirt-ui.io/managed": "true"} if managed else {}
    return {
        "metadata": {"name": name, "labels": labels},
        "spec": {},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }


def _k8s(items: list[dict]) -> MagicMock:
    k8s = MagicMock()

    async def list_obj(**kw):
        # The point of the fix: no label selector narrows this call.
        assert "kubevirt-ui.io/managed" not in kw.get("label_selector", ""), (
            "the VPC list is still filtered by the managed label"
        )
        return {"items": items}

    k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
    k8s.custom_api.get_cluster_custom_object = AsyncMock(return_value={"spec": {}})
    return k8s


def _request(k8s: MagicMock) -> MagicMock:
    request = MagicMock()
    request.app.state.k8s_client = k8s
    return request


ADMIN = User(
    id="admin", email="admin@local", username="admin",
    groups=["kubevirt-ui-admins"],
)


@pytest.mark.asyncio
async def test_a_vpc_made_outside_the_ui_is_listed() -> None:
    k8s = _k8s([_vpc("team-a", managed=True), _vpc("team-b"), _vpc("ovn-cluster")])

    out = await list_vpcs(request=_request(k8s), user=ADMIN)

    assert {v.name for v in out.items} >= {"team-a", "team-b"}, (
        "a CLI-created VPC is invisible in the console"
    )


@pytest.mark.asyncio
async def test_provenance_is_reported_as_a_field() -> None:
    k8s = _k8s([_vpc("team-a", managed=True), _vpc("team-b"), _vpc("ovn-cluster")])

    out = await list_vpcs(request=_request(k8s), user=ADMIN)
    origin = {v.name: v.origin for v in out.items}

    assert origin["team-a"] == "ui"
    assert origin["team-b"] == "external"
    assert origin["ovn-cluster"] == "system"
