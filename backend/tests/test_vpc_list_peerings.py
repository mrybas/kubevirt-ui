"""The Networks table said "Peerings 0" for a VPC that was peered.

On the lab cluster `Vpc/acme-net` carried

    spec.vpcPeerings: [{remoteVpc: ovn-cluster, localConnectIP: 169.254.101.25/30}]

and `GET /api/v1/vpcs/acme-net` returned it — while `GET /api/v1/vpcs`
returned `peerings: []` for every VPC, so the only screen that shows all VPCs
at once reported none of them as connected to anything.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


def _vpc_item(name: str, peerings: list[dict]) -> dict:
    return {
        "metadata": {"name": name, "labels": {"kubevirt-ui.io/managed": "true"}},
        "spec": {"vpcPeerings": peerings},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }


@pytest.mark.asyncio
async def test_the_listing_reports_the_peerings_it_has_in_hand(monkeypatch):
    from app.api.v1 import vpcs as mod

    item = _vpc_item("acme-net", [
        {"remoteVpc": "ovn-cluster", "localConnectIP": "169.254.101.25/30"},
    ])
    k8s = MagicMock()
    k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value={"items": [item]})
    request = MagicMock()
    request.app.state.k8s_client = k8s

    monkeypatch.setattr(mod, "_get_vpc_subnets", AsyncMock(return_value=([], False)))

    user = MagicMock()
    user.groups = ["system:masters"]
    monkeypatch.setattr(mod, "is_admin", lambda *a, **k: True)

    resp = await mod.list_vpcs(request, user=user)

    assert len(resp.items) == 1
    peerings = resp.items[0].peerings
    assert [p.remote_vpc for p in peerings] == ["ovn-cluster"], \
        "the list has the Vpc object already; it must not report zero"
    assert peerings[0].local_connect_ip == "169.254.101.25"


@pytest.mark.asyncio
async def test_an_unpeered_vpc_still_lists_none(monkeypatch):
    from app.api.v1 import vpcs as mod

    k8s = MagicMock()
    k8s.custom_api.list_cluster_custom_object = AsyncMock(
        return_value={"items": [_vpc_item("beta-net", [])]},
    )
    request = MagicMock()
    request.app.state.k8s_client = k8s
    monkeypatch.setattr(mod, "_get_vpc_subnets", AsyncMock(return_value=([], False)))
    monkeypatch.setattr(mod, "is_admin", lambda *a, **k: True)

    resp = await mod.list_vpcs(request, user=MagicMock())

    assert resp.items[0].peerings == []


def test_the_listing_does_not_refetch_each_vpc() -> None:
    """One object per VPC is already in the list response; re-reading it per
    row turns a page render into N+1 API calls."""
    from pathlib import Path

    src = Path("app/api/v1/vpcs.py").read_text()
    body = src[src.index("async def list_vpcs("):src.index("\n@router.", src.index("async def list_vpcs("))]
    assert "_peerings_from_item(item" in body
    assert "_get_vpc_peerings(" not in body
