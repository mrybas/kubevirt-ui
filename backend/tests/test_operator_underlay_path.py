"""The egress underlay as one object the operator reconciles.

The move is worth doing for exactly one reason. With the flag off, the
`ovn.kubernetes.io/external-gw` node label is repaired by the GET handler: it
comes back when somebody opens the page, and not otherwise. On the lab it sat at
an explicit `false` on all three workers, the link-watcher DaemonSet that
selects on it was scheduled nowhere, and `kubectl rollout status` reported
success — zero desired pods are all ready. Two hours later both provider links
were down and had taken the transit and egress planes with them.

So what is guarded here is not that the same YAML comes out. It is that with the
flag on the backend writes intent and nothing else, that it stops healing on
read, and that the operator's verdict is reported rather than re-derived.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client.exceptions import ApiException


def _request(**overrides: Any):
    from app.api.v1.vpc_underlay import VpcUnderlayRequest

    base = {
        "interface": "eth0.310",
        "external_cidr": "10.199.4.0/22",
        "external_gateway": "10.199.4.254",
        "vlan_id": 0,
        "exclude_nodes": ["cp-1"],
        "exclude_ips": ["10.199.4.1..10.199.4.9"],
        "provider_network_name": "extnet",
        "vlan_name": "vlan-extnet",
        "subnet_name": "external",
    }
    base.update(overrides)
    return VpcUnderlayRequest(**base)


def _cr(
    *,
    fabric: str = "True",
    labelled: str = "True",
    heals: int = 0,
    daemon_sets: list[dict[str, Any]] | None = None,
    generation: int = 1,
    observed: int | None = 1,
) -> dict[str, Any]:
    status: dict[str, Any] = {
        "conditions": [
            {"type": "FabricReady", "status": fabric,
             "message": "" if fabric == "True" else "Subnet/external: forbidden"},
            {"type": "NodesLabelled", "status": labelled,
             "message": "3 node(s) carry the provider NIC: w1, w2, w3"
                        if labelled == "True"
                        else "ProviderNetwork/extnet reports no ready nodes. Check that "
                             "eth0.310 exists on the workers"},
        ],
        "labelledNodes": ["w1", "w2", "w3"] if labelled == "True" else [],
        "labelHeals": heals,
        "daemonSets": daemon_sets if daemon_sets is not None else [
            {"name": "provider-link-up", "namespace": "o0-kube-ovn",
             "desired": 3, "ready": 3, "state": "running", "detail": "3/3 ready"},
        ],
    }
    if observed is not None:
        status["observedGeneration"] = observed
    return {
        "metadata": {"name": "external", "generation": generation},
        "spec": {
            "interface": "eth0.310",
            "providerNetworkName": "extnet",
            "vlanName": "vlan-extnet",
            "subnetName": "external",
            "kubeOVNNamespace": "o0-kube-ovn",
        },
        "status": status,
    }


def _k8s(cr: dict[str, Any] | None, *, create_conflicts: bool = False):
    """A client that records every write and answers with `cr`."""
    calls: dict[str, list[dict[str, Any]]] = {
        "create_cluster": [], "patch_cluster": [], "create_ns": [],
        "patch_node": [], "create_ds": [], "patch_ds": [],
    }

    async def _create_cluster(**kwargs: Any) -> dict[str, Any]:
        calls["create_cluster"].append(kwargs)
        if create_conflicts and kwargs.get("plural") == "managedunderlays":
            raise ApiException(status=409, reason="AlreadyExists")
        return kwargs["body"]

    async def _patch_cluster(**kwargs: Any) -> dict[str, Any]:
        calls["patch_cluster"].append(kwargs)
        return kwargs["body"]

    async def _get_cluster(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("plural") == "managedunderlays":
            if cr is None:
                raise ApiException(status=404, reason="NotFound")
            return cr
        raise ApiException(status=404, reason="NotFound")

    async def _create_ns(**kwargs: Any) -> dict[str, Any]:
        calls["create_ns"].append(kwargs)
        return kwargs["body"]

    async def _patch_node(**kwargs: Any) -> dict[str, Any]:
        calls["patch_node"].append(kwargs)
        return {}

    custom = MagicMock()
    custom.create_cluster_custom_object = AsyncMock(side_effect=_create_cluster)
    custom.patch_cluster_custom_object = AsyncMock(side_effect=_patch_cluster)
    custom.get_cluster_custom_object = AsyncMock(side_effect=_get_cluster)
    custom.create_namespaced_custom_object = AsyncMock(side_effect=_create_ns)

    core = MagicMock()
    core.read_node = AsyncMock(return_value=MagicMock(
        metadata=MagicMock(labels={"ovn.kubernetes.io/external-gw": "false"}),
    ))
    core.patch_node = AsyncMock(side_effect=_patch_node)
    core.list_node = AsyncMock(return_value=MagicMock(items=[]))
    core.list_config_map_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[]))

    apps = MagicMock()
    apps.create_namespaced_daemon_set = AsyncMock(
        side_effect=lambda **kw: calls["create_ds"].append(kw) or {})
    apps.patch_namespaced_daemon_set = AsyncMock(
        side_effect=lambda **kw: calls["patch_ds"].append(kw) or {})
    apps.read_namespaced_daemon_set = AsyncMock(
        side_effect=ApiException(status=404, reason="NotFound"))
    apps.list_daemon_set_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[]))

    client = MagicMock()
    client.custom_api = custom
    client.core_api = core
    client.apps_api = apps

    request = MagicMock()
    request.app.state.k8s_client = client
    return request, calls


@pytest.mark.asyncio
async def test_build_carries_every_field_the_operator_needs():
    """A field dropped here is a field the operator silently defaults.

    Derived from the request model rather than listed by hand: a new option on
    the form that nobody maps would otherwise be accepted, ignored, and reported
    as applied.
    """
    from app.api.v1.vpc_underlay import build_underlay_cr

    data = _request(
        link_watcher=False,
        link_watcher_image="example.invalid/w:1",
        cilium_source_ip_exempt=True,
        cilium_namespace="o0-cilium",
        cilium_image="quay.io/cilium/cilium:v1.20.0",
    )
    spec = build_underlay_cr(data, "o0-kube-ovn")["spec"]

    assert spec == {
        "interface": "eth0.310",
        "externalCIDR": "10.199.4.0/22",
        "externalGateway": "10.199.4.254",
        "vlanID": 0,
        "providerNetworkName": "extnet",
        "vlanName": "vlan-extnet",
        "subnetName": "external",
        "kubeOVNNamespace": "o0-kube-ovn",
        "linkWatcher": False,
        "excludeNodes": ["cp-1"],
        "excludeIPs": ["10.199.4.1..10.199.4.9"],
        "linkWatcherImage": "example.invalid/w:1",
        "ciliumSourceIPExempt": True,
        "ciliumNamespace": "o0-cilium",
        "ciliumImage": "quay.io/cilium/cilium:v1.20.0",
    }


@pytest.mark.asyncio
async def test_unset_cilium_stays_unset():
    """"Decide by looking at the cluster" has to survive the trip.

    The form's default was "not chaining", which was wrong on the one cluster it
    was ever asked about. Resolving it in the backend would freeze today's
    answer into the object and re-create that bug one layer down.
    """
    from app.api.v1.vpc_underlay import build_underlay_cr

    spec = build_underlay_cr(_request(cilium_source_ip_exempt=None), "o0-kube-ovn")["spec"]
    assert "ciliumSourceIPExempt" not in spec


@pytest.mark.asyncio
async def test_post_writes_intent_and_nothing_else():
    """One writer per object. With the flag on the fabric is the operator's."""
    from app.api.v1 import vpc_underlay

    request, calls = _k8s(_cr())
    with patch.object(vpc_underlay, "underlay_path_enabled", return_value=True), \
         patch("app.api.v1.network._find_kubeovn_namespace",
               AsyncMock(return_value="o0-kube-ovn")):
        result = await vpc_underlay.ensure_vpc_underlay(
            request, _request(), user=MagicMock(),
        )

    assert result.ready is True
    plurals = [c["plural"] for c in calls["create_cluster"]]
    assert plurals == ["managedunderlays"], plurals
    # The four objects the backend used to build, and the DaemonSets, are the
    # operator's now. Writing them from both sides is the exact bug the operator
    # exists to remove.
    assert calls["create_ns"] == []
    assert calls["create_ds"] == []
    assert calls["patch_ds"] == []
    assert calls["patch_node"] == []


@pytest.mark.asyncio
async def test_post_updates_an_existing_underlay_by_spec_only():
    """A rewrite of the whole object would race the operator's status writes."""
    from app.api.v1 import vpc_underlay

    request, calls = _k8s(_cr(), create_conflicts=True)
    with patch.object(vpc_underlay, "underlay_path_enabled", return_value=True), \
         patch("app.api.v1.network._find_kubeovn_namespace",
               AsyncMock(return_value="o0-kube-ovn")):
        await vpc_underlay.ensure_vpc_underlay(request, _request(), user=MagicMock())

    assert len(calls["patch_cluster"]) == 1
    patch_call = calls["patch_cluster"][0]
    assert set(patch_call["body"]) == {"spec"}
    assert patch_call["_content_type"] == "application/merge-patch+json"


@pytest.mark.asyncio
async def test_get_does_not_heal_when_the_operator_owns_it():
    """Two healers is one healer too many, and it hides the number.

    A reader that also repaired would make `labelHeals` count its own writes,
    and how long the label had been wrong — the only evidence that something
    else is writing it — would stop being visible.
    """
    from app.api.v1 import vpc_underlay

    request, calls = _k8s(_cr(heals=4))
    with patch.object(vpc_underlay, "underlay_path_enabled", return_value=True), \
         patch("app.api.v1.network._find_kubeovn_namespace",
               AsyncMock(return_value="o0-kube-ovn")):
        result = await vpc_underlay.get_vpc_underlay(
            request, provider_network_name="extnet", vlan_name="vlan-extnet",
            subnet_name="external", user=MagicMock(),
        )

    assert calls["patch_node"] == []
    heal_note = [o for o in result.objects if "heals" in o.name]
    assert len(heal_note) == 1
    assert "4 time(s)" in heal_note[0].detail


@pytest.mark.asyncio
async def test_get_falls_back_to_the_cluster_when_there_is_no_object_yet():
    """A hand-built fabric must not read as absent the moment the flag goes on.

    The flag says where new underlays are written. It does not say that what is
    already running stopped existing.
    """
    from app.api.v1 import vpc_underlay

    request, calls = _k8s(None)
    with patch.object(vpc_underlay, "underlay_path_enabled", return_value=True), \
         patch("app.api.v1.network._find_kubeovn_namespace",
               AsyncMock(return_value="o0-kube-ovn")):
        result = await vpc_underlay.get_vpc_underlay(
            request, provider_network_name="extnet", vlan_name="vlan-extnet",
            subnet_name="external", user=MagicMock(),
        )

    kinds = {o.kind for o in result.objects}
    assert "ProviderNetwork" in kinds and "NodeLabel" in kinds
    # And the old path is allowed to heal again, because it is the only healer
    # there is when no operator owns this fabric.
    assert result.ready is False


@pytest.mark.asyncio
async def test_a_refusal_is_reported_in_the_operator_s_own_words():
    """Not-ready has to say what to read next, or it is noise."""
    from app.api.v1.vpc_underlay import response_from_cr

    result = response_from_cr(_cr(labelled="False", daemon_sets=[
        {"name": "provider-link-up", "namespace": "o0-kube-ovn",
         "desired": 0, "ready": 0, "state": "scheduled-nowhere",
         "detail": "nothing matches its nodeSelector"},
    ]))

    assert result.ready is False
    assert "eth0.310" in result.detail
    watcher = [o for o in result.objects if o.name == "provider-link-up"][0]
    # A DaemonSet at 0/0 desired reads as healthy everywhere else in Kubernetes.
    assert watcher.state == "failed"
    assert watcher.workaround is True


@pytest.mark.asyncio
async def test_a_stale_status_is_not_reported_as_this_request_s_answer():
    """observedGeneration behind generation says nothing about what was just written."""
    from app.api.v1 import vpc_underlay

    request, _ = _k8s(_cr(generation=7, observed=6))
    with patch.object(vpc_underlay, "underlay_path_enabled", return_value=True), \
         patch("app.api.v1.network._find_kubeovn_namespace",
               AsyncMock(return_value="o0-kube-ovn")), \
         patch.object(vpc_underlay, "_READY_NODE_POLL_ATTEMPTS", 2), \
         patch.object(vpc_underlay, "_READY_NODE_POLL_SECONDS", 0):
        result = await vpc_underlay.ensure_vpc_underlay(
            request, _request(), user=MagicMock(),
        )

    # The poll gave up rather than passing off the previous generation's verdict
    # as this one's; the last read is still returned, so the caller gets the
    # object's current state and not an empty answer.
    assert result.objects
