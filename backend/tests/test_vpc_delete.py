"""Deleting a VPC has to take its subnets and the other side of its peerings.

`delete_vpc` unpacked `_get_vpc_subnets`, which returns `(subnets, isolated)`,
straight into a `for`:

    AttributeError: 'list' object has no attribute 'name'

so the request answered 500, the subnet stayed, and kube-ovn could then never
finish deleting the VPC — the exact mess that has to be untangled by hand with
finalizers.

It also tried to delete `vpc-peerings` objects, which do not exist: a peering
is an entry in `Vpc.spec.vpcPeerings` on *both* routers, and the remote side
was left pointing at a VPC that no longer exists.
"""

from pathlib import Path

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

SRC = Path("app/api/v1/vpcs.py").read_text()
BODY = SRC[SRC.index("async def delete_vpc("):SRC.index("\n@router.", SRC.index("async def delete_vpc("))]


def test_the_subnet_list_is_unpacked() -> None:
    assert "subnets, _isolated = await _get_vpc_subnets(k8s, name)" in BODY


def test_it_no_longer_deletes_a_kind_that_does_not_exist() -> None:
    assert '"vpc-peerings"' not in BODY


def test_the_remote_side_of_each_peering_is_removed() -> None:
    assert "_remove_peering_side(k8s, remote, name)" in BODY


def test_the_isolation_rules_are_re_scoped_afterwards() -> None:
    assert "reconcile_isolation_acls(k8s)" in BODY


# ---------------------------------------------------------------------------
# The router must outlive whatever kube-ovn finalizes against it.
# ---------------------------------------------------------------------------

def _delete_env(present, tracking=True):
    """A cluster where `present` is the set of objects still not gone."""
    from unittest.mock import AsyncMock, MagicMock

    k8s = MagicMock()
    live = set(present)

    async def _get(**kw):
        key = (kw["plural"], kw["name"])
        if key in live:
            return {"metadata": {"name": kw["name"]}}
        raise ApiException(status=404)

    async def _list(**kw):
        plural = kw["plural"]
        return {"items": [
            {"metadata": {"name": n}} for (p, n) in live if p == plural
        ]}

    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=_get)
    k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=_list)
    k8s.custom_api.delete_cluster_custom_object = AsyncMock()
    k8s.custom_api.patch_cluster_custom_object = AsyncMock()
    return k8s, live


@pytest.mark.asyncio
async def test_delete_waits_for_the_snat_rule_before_removing_the_router():
    """Two hours after deleting `gamma-net` on the lab cluster:

        ovn-snat-rules  snat-gamma-net  terminating
        ovn-eips        eip-gamma-net   terminating, holding 10.198.190.206
        E ... not found logical router "gamma-net", requeuing

    The finalizer runs against the VPC's logical router, so deleting the Vpc
    CR first strands them for good and leaks an address from a pool that had
    14 left. Refuse instead — they are already marked, a retry succeeds.
    """
    from app.api.v1.vpcs import _await_dependents_gone

    k8s, _ = _delete_env({("ovn-snat-rules", "snat-gamma-net"),
                          ("ovn-eips", "eip-gamma-net")})

    left = await _await_dependents_gone(
        k8s, [], {"ovn-snat-rules": ["snat-gamma-net"], "ovn-eips": ["eip-gamma-net"]},
        timeout=0,
    )

    assert left == {"ovn-snat-rules": ["snat-gamma-net"], "ovn-eips": ["eip-gamma-net"]}


@pytest.mark.asyncio
async def test_delete_waits_for_the_subnet_too():
    """`con3-default` outlived its VPC and sat on a finalizer for two hours,
    with the controller looping `vpc.kubeovn.io "con3" not found`."""
    from app.api.v1.vpcs import _await_dependents_gone

    k8s, _ = _delete_env({("subnets", "con3-default")})

    left = await _await_dependents_gone(k8s, ["con3-default"], {}, timeout=0)
    assert left == {"subnets": ["con3-default"]}


@pytest.mark.asyncio
async def test_delete_proceeds_once_they_are_gone():
    from app.api.v1.vpcs import _await_dependents_gone

    k8s, _ = _delete_env(set())
    assert await _await_dependents_gone(k8s, ["gone-default"], {"ovn-eips": ["eip-x"]}, timeout=0) == {}


@pytest.mark.asyncio
async def test_the_endpoint_refuses_while_the_subnet_is_still_terminating(monkeypatch):
    """The whole point, wired up: DELETE must not remove the Vpc CR while a
    dependent is mid-finalize. It used to answer `{"status": "deleted"}` and
    leave the cluster holding `con3-default` forever."""
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import HTTPException

    from app.api.v1 import vpcs as mod

    monkeypatch.setattr(mod, "DELETE_DRAIN_TIMEOUT", 0.0)
    k8s, _ = _delete_env({("subnets", "con3-default")})
    request = MagicMock()
    request.app.state.k8s_client = k8s

    subnet = MagicMock()
    subnet.name = "con3-default"
    monkeypatch.setattr(mod, "_get_vpc_subnets", AsyncMock(return_value=([subnet], True)))
    monkeypatch.setattr(mod, "_get_vpc_peerings", AsyncMock(return_value=[]))
    monkeypatch.setattr(mod, "_delete_vpc_dns_policy", AsyncMock())
    monkeypatch.setattr(mod, "reconcile_isolation_acls", AsyncMock(return_value=0))
    monkeypatch.setattr("app.api.v1.ovn_gateway._get_gateway_tracking",
                        AsyncMock(return_value=({}, None)))

    with pytest.raises(HTTPException) as e:
        await mod.delete_vpc(request, "con3", user=MagicMock())

    assert e.value.status_code == 409
    assert "con3-default" in e.value.detail
    deleted = [c.kwargs.get("plural") for c in k8s.custom_api.delete_cluster_custom_object.await_args_list]
    assert "vpcs" not in deleted, "the router must still be there for the finalizer"


@pytest.mark.asyncio
async def test_a_successful_delete_actually_returns(monkeypatch):
    """Every delete answered 500 — after doing all of the work.

        File "/app/app/api/v1/vpcs.py", line 1586, in delete_vpc
        NameError: name 'peerings' is not defined

    The summary at the end of the handler counted a variable that the
    peering-removal rewrite had turned into a loop-local. The VPC really was
    gone, so the UI showed a failure for an operation that had succeeded and
    could not be retried — `DELETE /vpcs/con4` then answered 404. Only the
    refusal path was covered by tests, which is exactly why it survived.
    """
    from unittest.mock import AsyncMock, MagicMock

    from app.api.v1 import vpcs as mod

    k8s, _ = _delete_env(set())
    request = MagicMock()
    request.app.state.k8s_client = k8s

    subnet = MagicMock()
    subnet.name = "reco1-default"
    peering = MagicMock()
    peering.remote_vpc = "ovn-cluster"

    monkeypatch.setattr(mod, "_get_vpc_subnets", AsyncMock(return_value=([subnet], True)))
    monkeypatch.setattr(mod, "_get_vpc_peerings", AsyncMock(return_value=[peering]))
    monkeypatch.setattr(mod, "_remove_peering_side", AsyncMock())
    monkeypatch.setattr(mod, "_delete_vpc_dns_policy", AsyncMock())
    monkeypatch.setattr(mod, "reconcile_isolation_acls", AsyncMock(return_value=2))
    monkeypatch.setattr("app.api.v1.ovn_gateway._get_gateway_tracking",
                        AsyncMock(return_value=({}, None)))

    result = await mod.delete_vpc(request, "reco1", user=MagicMock())

    assert result["status"] == "deleted"
    assert result["subnets_deleted"] == 1
    assert result["peerings_deleted"] == 1
    deleted = [c.kwargs.get("plural") for c in k8s.custom_api.delete_cluster_custom_object.await_args_list]
    assert "vpcs" in deleted
