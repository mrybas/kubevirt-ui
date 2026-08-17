"""The provider-NIC label is re-checked on every read, not set once.

The link-watcher DaemonSet selects on `ovn.kubernetes.io/external-gw=true`.
On the lab that label was found at an explicit `false` on all three workers,
with nothing in `managedFields` claiming it — so the author is not necessarily
us, and arguing about it does not bring the underlay back.

What it cost: no label, no watcher pod, and `kubectl rollout status` still
reports "successfully rolled out" because zero desired pods are all ready.
Nothing looked wrong for two hours, until both provider links went down on
their own and took the transit and egress planes with them.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.vpc_underlay import EXTERNAL_GW_LABEL, _heal_gateway_labels

PN = "external"


def _node(name: str, label: str | None):
    n = MagicMock()
    n.metadata.name = name
    n.metadata.labels = {} if label is None else {EXTERNAL_GW_LABEL: label}
    return n


def _k8s(ready: list[str], labels: dict[str, str | None]) -> tuple[MagicMock, list]:
    patched: list = []

    async def get_obj(**kw):
        return {"status": {"readyNodes": ready}}

    async def read_node(name):
        return _node(name, labels.get(name))

    async def patch_node(name, body):
        patched.append((name, body["metadata"]["labels"][EXTERNAL_GW_LABEL]))
        return _node(name, "true")

    k8s = MagicMock()
    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=get_obj)
    k8s.core_api.read_node = AsyncMock(side_effect=read_node)
    k8s.core_api.patch_node = AsyncMock(side_effect=patch_node)
    return k8s, patched


@pytest.mark.asyncio
async def test_an_explicit_false_is_restored() -> None:
    k8s, patched = _k8s(["w1", "w2"], {"w1": "false", "w2": "false"})

    ok, healed, failed = await _heal_gateway_labels(k8s, PN)

    assert healed == ["w1", "w2"], "a false label is drift, not a decision"
    assert patched == [("w1", "true"), ("w2", "true")]
    assert (ok, failed) == ([], [])


@pytest.mark.asyncio
async def test_a_missing_label_is_restored() -> None:
    k8s, patched = _k8s(["w1"], {"w1": None})

    _, healed, _ = await _heal_gateway_labels(k8s, PN)

    assert healed == ["w1"]


@pytest.mark.asyncio
async def test_a_correct_label_is_left_alone() -> None:
    """No write means no resourceVersion churn on every page load."""
    k8s, patched = _k8s(["w1", "w2"], {"w1": "true", "w2": "true"})

    ok, healed, failed = await _heal_gateway_labels(k8s, PN)

    assert (ok, healed, failed) == (["w1", "w2"], [], [])
    assert patched == []


@pytest.mark.asyncio
async def test_only_ready_nodes_of_this_network_are_touched() -> None:
    """A node without the provider NIC must not be told it has one."""
    k8s, patched = _k8s(["w1"], {"w1": "false", "cp1": "false"})

    _, healed, _ = await _heal_gateway_labels(k8s, PN)

    assert healed == ["w1"]
    assert [n for n, _ in patched] == ["w1"]


@pytest.mark.asyncio
async def test_a_node_that_cannot_be_patched_is_reported_not_swallowed() -> None:
    from kubernetes_asyncio.client.exceptions import ApiException

    k8s, _ = _k8s(["w1"], {"w1": "false"})
    k8s.core_api.patch_node = AsyncMock(side_effect=ApiException(status=403))

    ok, healed, failed = await _heal_gateway_labels(k8s, PN)

    assert failed == ["w1"] and healed == []
