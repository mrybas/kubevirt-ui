"""VPCs as one object the operator reconciles.

The switch is worth making for one thing in particular. A VpcDns pod cannot
reach the cluster resolver's ClusterIP without a route the create path applied
once, best-effort, at a moment when kube-ovn has not created the Deployment it
belongs on — and which nothing applied afterwards except a person calling the
recreate endpoint.

What is guarded here is the division of ownership, because that is where this
migration can hurt. With the flag on the endpoint writes intent and nothing
else; the CIDR allocator, the namespace checks and above all `Subnet.spec.acls`
stay exactly where they are.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from app.models.vpc import VpcCreateRequest


def _request(**overrides: Any) -> VpcCreateRequest:
    base: dict[str, Any] = {"name": "opnet", "subnet_cidr": "10.200.40.0/22"}
    base.update(overrides)
    return VpcCreateRequest(**base)


def _harness(*, ready: bool = True, exists: bool = False, policy: str = "Delete"):
    """A client that records every write and answers as the operator would."""
    calls: dict[str, list[dict[str, Any]]] = {
        "create_cluster": [], "delete_cluster": [], "patch_cluster": [],
        "create_ns": [],
    }

    async def _create_cluster(**kw: Any) -> dict[str, Any]:
        calls["create_cluster"].append(kw)
        return kw["body"]

    async def _delete_cluster(**kw: Any) -> dict[str, Any]:
        calls["delete_cluster"].append(kw)
        return {}

    async def _get_cluster(**kw: Any) -> dict[str, Any]:
        if kw.get("plural") == "managednetworks":
            if not exists and not calls["create_cluster"]:
                raise ApiException(status=404, reason="NotFound")
            conditions = [{"type": "Ready", "status": "True" if ready else "False"}]
            return {"metadata": {"name": kw["name"]},
                    "spec": {"deletionPolicy": policy},
                    "status": {"conditions": conditions}}
        if kw.get("plural") == "subnets":
            # The normal outcome: the isolation pass found the subnet and wrote
            # its rules. Tests that care about the other outcome say so.
            return {"spec": {"acls": [
                {"action": "drop", "match": "ip4.src == 10.200.0.0/14",
                 "priority": 3000, "direction": "to-lport"},
            ]}}
        raise ApiException(status=404, reason="NotFound")

    async def _list_cluster(**kw: Any) -> dict[str, Any]:
        return {"items": []}

    custom = MagicMock()
    custom.create_cluster_custom_object = AsyncMock(side_effect=_create_cluster)
    custom.delete_cluster_custom_object = AsyncMock(side_effect=_delete_cluster)
    custom.patch_cluster_custom_object = AsyncMock(
        side_effect=lambda **kw: calls["patch_cluster"].append(kw) or {})
    custom.get_cluster_custom_object = AsyncMock(side_effect=_get_cluster)
    custom.list_cluster_custom_object = AsyncMock(side_effect=_list_cluster)
    custom.create_namespaced_custom_object = AsyncMock(
        side_effect=lambda **kw: calls["create_ns"].append(kw) or {})

    core = MagicMock()
    core.read_namespace = AsyncMock(return_value=MagicMock(
        metadata=MagicMock(labels={"kubevirt-ui.io/managed": "true"})))

    client = MagicMock()
    client.custom_api = custom
    client.core_api = core

    request = MagicMock()
    request.app.state.k8s_client = client
    return request, calls


@pytest.mark.asyncio
async def test_the_object_carries_what_the_operator_needs():
    """A field dropped here is one the operator silently defaults."""
    from app.api.v1.vpcs import build_managed_network

    data = _request(
        name="t1", tenant="acme", folder="poc", environment="dev",
        role="infrastructure", isolated=False, shared_cidrs=["10.1.0.0/16"],
    )
    with patch("app.api.v1.vpcs.b3_enabled", return_value=True), \
         patch("app.api.v1.vpcs.transit_subnet_name", return_value="cp-transit"), \
         patch("app.api.v1.vpcs.external_subnet", return_value="external"), \
         patch("app.api.v1.vpcs._vpc_nat_gateway_enabled", return_value=False):
        spec = build_managed_network(
            data, cidr="10.200.40.0/22", gateway="10.200.40.1",
            namespaces=["tenant-acme"], dns_server="10.96.0.200",
        )["spec"]

    assert spec == {
        "cidr": "10.200.40.0/22",
        "gateway": "10.200.40.1",
        "isolated": False,
        "natGateway": False,
        # Created here, so the object really does own it. Adoption is the other
        # case and defaults to Retain.
        "deletionPolicy": "Delete",
        "namespaces": ["tenant-acme"],
        "tenant": "acme",
        "folder": "poc",
        "environment": "dev",
        "role": "infrastructure",
        "dnsServer": "10.96.0.200",
        "sharedCIDRs": ["10.1.0.0/16"],
        "externalPlane": {
            "attachments": ["cp-transit", "external"],
            "egressSubnet": "external",
        },
    }


@pytest.mark.asyncio
async def test_the_next_hop_is_not_carried():
    """B3_VPC_GATEWAY must equal the external subnet's gateway, so it is the
    same number in two places. The operator reads it from the subnet instead."""
    from app.api.v1.vpcs import build_managed_network

    with patch("app.api.v1.vpcs.b3_enabled", return_value=True), \
         patch("app.api.v1.vpcs.transit_subnet_name", return_value="cp-transit"), \
         patch("app.api.v1.vpcs.external_subnet", return_value="external"), \
         patch("app.api.v1.vpcs._vpc_nat_gateway_enabled", return_value=False):
        plane = build_managed_network(
            _request(), cidr="10.200.40.0/22", gateway="10.200.40.1",
            namespaces=[], dns_server=None,
        )["spec"]["externalPlane"]

    assert set(plane) == {"attachments", "egressSubnet"}


@pytest.mark.asyncio
async def test_create_writes_intent_and_leaves_the_acls_alone():
    """One writer per object. The ACL list keeps the writer it has."""
    from app.api.v1 import vpcs

    request, calls = _harness()
    isolation = AsyncMock(return_value=0)
    with patch.object(vpcs, "network_path_enabled", return_value=True), \
         patch.object(vpcs, "_ensure_cluster_config", AsyncMock()), \
         patch.object(vpcs, "assert_cidr_free", AsyncMock()), \
         patch.object(vpcs, "_vpcdns_vip", return_value="10.96.0.200"), \
         patch.object(vpcs, "b3_enabled", return_value=False), \
         patch.object(vpcs, "_tenant_vpc_cidrs", AsyncMock(return_value=[])), \
         patch.object(vpcs, "_peer_shared_cidrs", AsyncMock()), \
         patch.object(vpcs, "reconcile_infra_peerings", AsyncMock(return_value=0)), \
         patch.object(vpcs, "reconcile_isolation_acls", isolation):
        result = await vpcs.create_vpc(
            request, _request(isolated=False), user=MagicMock(),
        )

    assert result.name == "opnet"
    plurals = [c["plural"] for c in calls["create_cluster"]]
    assert plurals == ["managednetworks"], plurals
    # The Vpc, the Subnet and the VpcDns are the operator's now.
    assert calls["create_ns"] == []
    # And the isolation pass still runs, because nothing else writes those rules.
    assert isolation.await_count == 1


@pytest.mark.asyncio
async def test_isolation_waits_for_the_subnet_to_exist():
    """The ACLs used to be written into the subnet manifest itself, so a VPC was
    isolated the instant it existed. Now the subnet arrives a moment later and
    this pass is the only thing that will ever write its rules — run it first
    and the network stays open with nothing scheduled to look again."""
    from app.api.v1 import vpcs

    request, _ = _harness()
    order: list[str] = []

    async def _await(k8s: Any, name: str, attempts: int = 20) -> bool:
        order.append("waited")
        return True

    async def _isolate(k8s: Any) -> int:
        order.append("isolated")
        return 1

    with patch.object(vpcs, "network_path_enabled", return_value=True), \
         patch.object(vpcs, "_ensure_cluster_config", AsyncMock()), \
         patch.object(vpcs, "assert_cidr_free", AsyncMock()), \
         patch.object(vpcs, "_vpcdns_vip", return_value="10.96.0.200"), \
         patch.object(vpcs, "b3_enabled", return_value=False), \
         patch.object(vpcs, "_tenant_vpc_cidrs", AsyncMock(return_value=[])), \
         patch.object(vpcs, "_peer_shared_cidrs", AsyncMock()), \
         patch.object(vpcs, "_await_managed_network", _await), \
         patch.object(vpcs, "reconcile_infra_peerings", AsyncMock(return_value=0)), \
         patch.object(vpcs, "reconcile_isolation_acls", _isolate):
        await vpcs.create_vpc(request, _request(isolated=False), user=MagicMock())

    assert order == ["waited", "isolated"], order


@pytest.mark.asyncio
async def test_a_slow_operator_does_not_fail_the_create():
    """The network is being built either way; refusing would be a worse answer
    than a slower one."""
    from app.api.v1 import vpcs

    request, _ = _harness(ready=False)
    with patch.object(vpcs, "asyncio", MagicMock(sleep=AsyncMock())):
        got = await vpcs._await_managed_network(request.app.state.k8s_client, "opnet", attempts=2)
    assert got is False


@pytest.mark.asyncio
async def test_delete_hands_the_teardown_to_whoever_owns_it():
    """Ownership is a property of the object, not of a flag: tearing the network
    down from here would race the cascade the operator is already running."""
    from app.api.v1 import vpcs

    request, calls = _harness(exists=True)
    with patch.object(vpcs, "network_path_enabled", return_value=False):
        result = await vpcs.delete_vpc(request, "opnet", user=MagicMock())

    assert "being removed" in result["message"]
    assert [c["plural"] for c in calls["delete_cluster"]] == ["managednetworks"]


@pytest.mark.asyncio
async def test_delete_of_an_unmanaged_vpc_takes_the_old_path():
    """A network created before the switch is not the operator's, and the only
    thing that can tear it down is the code that built it."""
    from app.api.v1 import vpcs

    request, calls = _harness(exists=False)
    with patch.object(vpcs, "network_path_enabled", return_value=True), \
         patch.object(vpcs, "_get_gateway_tracking", AsyncMock(return_value=({}, None)),
                      create=True):
        try:
            await vpcs.delete_vpc(request, "legacy-net", user=MagicMock())
        except Exception:
            # The legacy path needs far more of the cluster than this harness
            # provides; what matters is that it was entered at all.
            pass

    assert [c["plural"] for c in calls["delete_cluster"]] != ["managednetworks"]


@pytest.mark.asyncio
async def test_an_isolated_network_goes_through_too():
    """It did not, until isolation became durable desired state.

    The objection was never timing. It was that the subnet is written first and
    the rules afterwards, so a process dying in between leaves a tenant network
    reachable from every other tenant with nothing scheduled to notice. Waiting
    does not fix that and neither does undoing afterwards.

    What fixed it is the operator writing the rules itself and not reporting the
    network ready until they are on the subnet. This process is no longer part
    of the guarantee.
    """
    from app.api.v1 import vpcs

    request, calls = _harness()
    with patch.object(vpcs, "network_path_enabled", return_value=True), \
         patch.object(vpcs, "_ensure_cluster_config", AsyncMock()), \
         patch.object(vpcs, "assert_cidr_free", AsyncMock()), \
         patch.object(vpcs, "_vpcdns_vip", return_value="10.96.0.200"), \
         patch.object(vpcs, "b3_enabled", return_value=False), \
         patch.object(vpcs, "_tenant_vpc_cidrs", AsyncMock(return_value=["10.200.0.0/22"])), \
         patch.object(vpcs, "_peer_shared_cidrs", AsyncMock()), \
         patch.object(vpcs, "reconcile_infra_peerings", AsyncMock(return_value=0)), \
         patch.object(vpcs, "reconcile_isolation_acls", AsyncMock(return_value=0)):
        result = await vpcs.create_vpc(request, _request(isolated=True), user=MagicMock())

    assert result.name == "opnet"
    assert [c["plural"] for c in calls["create_cluster"]] == ["managednetworks"]
    # The intent travels in the object, which is what makes it durable.
    body = calls["create_cluster"][0]["body"]
    assert body["spec"]["isolated"] is True


@pytest.mark.asyncio
async def test_a_network_that_asked_not_to_be_isolated_goes_through():
    """There is no isolation to lose, so there is nothing to be durable about."""
    from app.api.v1 import vpcs

    request, calls = _harness()
    with patch.object(vpcs, "network_path_enabled", return_value=True), \
         patch.object(vpcs, "_ensure_cluster_config", AsyncMock()), \
         patch.object(vpcs, "assert_cidr_free", AsyncMock()), \
         patch.object(vpcs, "_vpcdns_vip", return_value="10.96.0.200"), \
         patch.object(vpcs, "b3_enabled", return_value=False), \
         patch.object(vpcs, "_tenant_vpc_cidrs", AsyncMock(return_value=[])), \
         patch.object(vpcs, "_peer_shared_cidrs", AsyncMock()), \
         patch.object(vpcs, "reconcile_infra_peerings", AsyncMock(return_value=0)), \
         patch.object(vpcs, "reconcile_isolation_acls", AsyncMock(return_value=0)):
        result = await vpcs.create_vpc(
            request, _request(isolated=False), user=MagicMock(),
        )

    assert result.name == "opnet"
    assert [c["plural"] for c in calls["create_cluster"]] == ["managednetworks"]


@pytest.mark.asyncio
async def test_the_isolation_pass_leaves_the_operators_subnets_alone():
    """Two writers of one ACL list is the failure this migration exists to
    remove, so ownership is read off the object and honoured.

    The subnet still counts in the census — every other VPC has to know its
    prefix — but its own rules are not written from here.
    """
    from app.api.v1 import vpcs

    patched: list[dict[str, Any]] = []

    def _subnet(name: str, vpc: str, cidr: str, owned: bool) -> dict[str, Any]:
        metadata: dict[str, Any] = {"name": name}
        if owned:
            metadata["annotations"] = {vpcs.ACL_OWNER_ANNOTATION: vpcs.ACL_OWNER_OPERATOR}
        return {"metadata": metadata,
                "spec": {"vpc": vpc, "cidrBlock": cidr, "acls": []}}

    async def _list(**kw: Any) -> dict[str, Any]:
        if kw.get("plural") == "vpcs":
            return {"items": [{"metadata": {"name": "a"}, "spec": {}},
                              {"metadata": {"name": "b"}, "spec": {}}]}
        return {"items": [
            _subnet("a-default", "a", "10.200.0.0/22", owned=True),
            _subnet("b-default", "b", "10.200.4.0/22", owned=False),
        ]}

    k8s = MagicMock()
    k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=_list)
    k8s.custom_api.patch_cluster_custom_object = AsyncMock(
        side_effect=lambda **kw: patched.append(kw) or {})

    with patch.object(vpcs, "b3_enabled", return_value=False):
        await vpcs.reconcile_isolation_acls(k8s)

    touched = {c["name"] for c in patched}
    assert "a-default" not in touched, "the pass wrote to a subnet it does not own"
    assert "b-default" in touched, "the pass stopped writing the subnets it does own"

    # And the owned network's prefix is still denied on the unowned one: leaving
    # its list alone must not mean forgetting it exists.
    body = next(c for c in patched if c["name"] == "b-default")["body"]
    matches = " ".join(a["match"] for a in body["spec"]["acls"])
    assert "10.200.0.0/22" in matches, matches


@pytest.mark.asyncio
async def test_a_describing_cr_does_not_get_handed_the_teardown():
    """A `Retain` object describes a network it does not own.

    Deleting it removes the description and nothing else, so handing the
    teardown to it reports "being removed" and removes nothing — a delete
    endpoint that lies. Measured on the stand: the CR went, the Vpc and Subnet
    stayed, and the caller was told the network was going away.
    """
    from app.api.v1 import vpcs

    request, calls = _harness(exists=True, policy="Retain")
    entered_legacy = False

    async def _tracking(k8s: Any, name: str) -> tuple[dict, None]:
        nonlocal entered_legacy
        entered_legacy = True
        raise RuntimeError("stop here; the legacy path needs a whole cluster")

    # Patched where the legacy path imports it from, not where it is used: that
    # import happens inside the function, so patching the caller's module misses
    # it entirely.
    with patch("app.api.v1.ovn_gateway._get_gateway_tracking", _tracking), \
         patch.object(vpcs.asyncio, "sleep", AsyncMock()):
        try:
            await vpcs.delete_vpc(request, "described", user=MagicMock())
        except RuntimeError:
            pass

    # The description was removed…
    assert [c["plural"] for c in calls["delete_cluster"]] == ["managednetworks"]
    # …and the network is still this endpoint's to take apart.
    assert entered_legacy, "the teardown was handed to a CR that does not cascade"


@pytest.mark.asyncio
async def test_a_cascading_cr_is_handed_the_teardown():
    from app.api.v1 import vpcs

    request, calls = _harness(exists=True, policy="Delete")
    result = await vpcs.delete_vpc(request, "owned", user=MagicMock())

    assert "being removed" in result["message"]
    assert [c["plural"] for c in calls["delete_cluster"]] == ["managednetworks"]


@pytest.mark.asyncio
async def test_the_teardown_waits_for_the_description_to_be_gone():
    """Ordering, not politeness.

    The operator keeps reconciling that object until it observes the deletion,
    and its writes are CreateOrUpdate — so a subnet deleted by the legacy path
    can be kept alive, or recreated, by a controller still working from a
    description that no longer exists. Two reconcilers pulling in opposite
    directions is how a teardown wedges.
    """
    from app.api.v1 import vpcs

    request, calls = _harness(exists=True, policy="Retain")
    order: list[str] = []
    still_there = {"n": 2}

    async def _get_cluster(**kw: Any) -> dict[str, Any]:
        if kw.get("plural") != "managednetworks":
            raise ApiException(status=404, reason="NotFound")
        if calls["delete_cluster"]:
            # After the delete, it lingers for a couple of reads.
            if still_there["n"] > 0:
                still_there["n"] -= 1
                return {"metadata": {"name": kw["name"]}, "spec": {"deletionPolicy": "Retain"}}
            raise ApiException(status=404, reason="NotFound")
        return {"metadata": {"name": kw["name"]}, "spec": {"deletionPolicy": "Retain"}}

    async def _delete_cluster(**kw: Any) -> dict[str, Any]:
        order.append("cr-deleted")
        calls["delete_cluster"].append(kw)
        return {}

    async def _tracking(k8s: Any, name: str) -> tuple[dict, None]:
        order.append("legacy-started")
        raise RuntimeError("stop here")

    request.app.state.k8s_client.custom_api.get_cluster_custom_object = AsyncMock(
        side_effect=_get_cluster)
    request.app.state.k8s_client.custom_api.delete_cluster_custom_object = AsyncMock(
        side_effect=_delete_cluster)

    with patch("app.api.v1.ovn_gateway._get_gateway_tracking", _tracking), \
         patch.object(vpcs.asyncio, "sleep", AsyncMock()):
        try:
            await vpcs.delete_vpc(request, "described", user=MagicMock())
        except RuntimeError:
            pass

    assert order == ["cr-deleted", "legacy-started"], order
    # And it really waited: the reads after the delete are the wait.
    assert still_there["n"] == 0, "the teardown started before the object was gone"
