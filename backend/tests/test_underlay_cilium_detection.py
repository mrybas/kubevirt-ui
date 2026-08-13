"""The underlay form should not ask the operator what the cluster already knows.

The Cilium workaround defaulted to off with the note "not chaining Cilium",
on a cluster whose `cilium-config` says `cni-chaining-mode: generic-veth` —
and the namespace field offered `kube-system` while Cilium lives in
`o0-cilium`. Both facts are readable from the cluster.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.vpc_underlay import detect_cilium


def _k8s(configmaps):
    k8s = MagicMock()
    items = []
    for ns, data in configmaps:
        cm = MagicMock()
        cm.data = data
        cm.metadata.namespace = ns
        items.append(cm)
    k8s.core_api.list_config_map_for_all_namespaces = AsyncMock(
        return_value=MagicMock(items=items),
    )
    return k8s


@pytest.mark.asyncio
class TestDetectCilium:
    async def test_finds_chaining_and_the_namespace(self) -> None:
        chaining, ns = await detect_cilium(
            _k8s([("o0-cilium", {"cni-chaining-mode": "generic-veth"})]),
        )
        assert chaining is True
        assert ns == "o0-cilium"

    async def test_none_means_not_chaining(self) -> None:
        chaining, ns = await detect_cilium(
            _k8s([("kube-system", {"cni-chaining-mode": "none"})]),
        )
        assert chaining is False

    async def test_absent_key_means_not_chaining(self) -> None:
        chaining, _ = await detect_cilium(_k8s([("kube-system", {"ipam": "cluster-pool"})]))
        assert chaining is False

    async def test_no_cilium_at_all_is_not_an_error(self) -> None:
        chaining, ns = await detect_cilium(_k8s([]))
        assert chaining is False
        assert ns == "kube-system"

    async def test_an_unreadable_cluster_does_not_break_the_build(self) -> None:
        k8s = MagicMock()
        k8s.core_api.list_config_map_for_all_namespaces = AsyncMock(
            side_effect=RuntimeError("no permission"),
        )
        assert await detect_cilium(k8s) == (False, "kube-system")


def test_the_request_leaves_both_answers_unset_by_default() -> None:
    from app.api.v1.vpc_underlay import VpcUnderlayRequest

    req = VpcUnderlayRequest(interface="eth1", external_cidr="10.0.0.0/24",
                             external_gateway="10.0.0.1")
    assert req.cilium_source_ip_exempt is None, "unset means: ask the cluster"
    assert req.cilium_namespace == ""
