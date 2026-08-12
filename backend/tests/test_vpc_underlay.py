"""Unit tests for the VPC underlay fabric.

A wizard-created VPC has no way out until a ProviderNetwork, a Vlan and an
external Subnet reachable through a NAD exist. The UI used to look for that
fabric and never build it, so it had to be applied by hand.

The shapes below are the ones verified on the lab; several fields are
load-bearing in ways that fail silently if they drift.
"""

from unittest.mock import AsyncMock, MagicMock

import json
import pytest
from kubernetes_asyncio.client import ApiException
from pydantic import ValidationError

from app.api.v1.vpc_underlay import (
    CILIUM_EXEMPT_NAME,
    LINK_WATCHER_NAME,
    WORKAROUND_LABEL,
    WORKAROUND_REMOVE_WHEN,
    VpcUnderlayRequest,
    build_cilium_exempt,
    build_external_nad,
    build_external_subnet,
    build_link_watcher,
    build_provider_network,
    build_vlan,
    ensure_vpc_underlay,
    subnet_provider,
)

KUBEOVN_NS = "o0-kube-ovn"


def _req(**kw: object) -> VpcUnderlayRequest:
    base: dict = {
        "interface": "eth1",
        "external_cidr": "10.198.176.0/20",
        "external_gateway": "10.198.191.254",
    }
    base.update(kw)
    return VpcUnderlayRequest(**base)  # type: ignore[arg-type]


class TestProviderNetwork:
    def test_uses_the_dedicated_interface(self) -> None:
        assert build_provider_network(_req())["spec"]["defaultInterface"] == "eth1"

    def test_excludes_nodes_without_the_nic(self) -> None:
        # Control planes usually have one NIC; kube-ovn would enslave it.
        pn = build_provider_network(_req(exclude_nodes=["cp-1", "cp-2"]))
        assert pn["spec"]["excludeNodes"] == ["cp-1", "cp-2"]

    def test_no_exclude_key_when_every_node_qualifies(self) -> None:
        assert "excludeNodes" not in build_provider_network(_req())["spec"]


class TestVlan:
    def test_binds_the_vlan_to_the_provider_network(self) -> None:
        vlan = build_vlan(_req(provider_network_name="external"))
        assert vlan["spec"]["provider"] == "external"

    def test_untagged_is_expressible(self) -> None:
        # id 0 = untagged, which is what works under an overlay that eats tags.
        assert build_vlan(_req(vlan_id=0))["spec"]["id"] == 0

    def test_rejects_an_out_of_range_id(self) -> None:
        with pytest.raises(ValidationError):
            _req(vlan_id=5000)


class TestProviderString:
    def test_is_nad_dot_namespace_dot_ovn(self) -> None:
        assert subnet_provider(_req(subnet_name="ext-sub"), KUBEOVN_NS) == (
            f"ext-sub.{KUBEOVN_NS}.ovn"
        )

    def test_subnet_and_nad_agree_character_for_character(self) -> None:
        # kube-ovn compares these exactly; a mismatch makes it refuse the
        # egress gateway with "please set correct provider of subnet ...".
        data = _req()
        subnet = build_external_subnet(data, KUBEOVN_NS)
        nad_config = json.loads(build_external_nad(data, KUBEOVN_NS)["spec"]["config"])
        assert subnet["spec"]["provider"] == nad_config["provider"]


class TestExternalSubnet:
    def test_carries_the_infrastructure_label_the_ui_looks_for(self) -> None:
        # network._find_infra_subnet selects on this.
        labels = build_external_subnet(_req(), KUBEOVN_NS)["metadata"]["labels"]
        assert labels["kubevirt-ui.io/purpose"] == "infrastructure"

    def test_does_not_nat_outgoing(self) -> None:
        # The gateways handle SNAT (or deliberately do not); the underlay must
        # not do it underneath them.
        assert build_external_subnet(_req(), KUBEOVN_NS)["spec"]["natOutgoing"] is False

    def test_gateway_check_disabled(self) -> None:
        # The upstream gateway need not answer kube-ovn's ping, and a failed
        # check blocks the subnet.
        assert build_external_subnet(_req(), KUBEOVN_NS)["spec"]["disableGatewayCheck"] is True

    def test_exclude_ranges_are_passed_through(self) -> None:
        spec = build_external_subnet(
            _req(exclude_ips=["10.198.176.1..10.198.190.199"]), KUBEOVN_NS,
        )["spec"]
        assert spec["excludeIps"] == ["10.198.176.1..10.198.190.199"]

    def test_references_the_vlan(self) -> None:
        spec = build_external_subnet(_req(vlan_name="vlan-external"), KUBEOVN_NS)["spec"]
        assert spec["vlan"] == "vlan-external"


class TestNad:
    def test_lives_where_the_gateways_run(self) -> None:
        nad = build_external_nad(_req(), KUBEOVN_NS)
        assert nad["metadata"]["namespace"] == KUBEOVN_NS

    def test_config_is_kube_ovn_type(self) -> None:
        config = json.loads(build_external_nad(_req(), KUBEOVN_NS)["spec"]["config"])
        assert config["type"] == "kube-ovn"
        assert config["server_socket"].endswith("kube-ovn-daemon.sock")


class TestWorkaroundsAreLabelled:
    def test_link_watcher_says_what_retires_it(self) -> None:
        meta = build_link_watcher(_req(), KUBEOVN_NS)["metadata"]
        assert WORKAROUND_LABEL in meta["labels"]
        assert "kube-ovn" in meta["labels"][WORKAROUND_LABEL]
        assert meta["annotations"][WORKAROUND_REMOVE_WHEN]

    def test_cilium_exempt_says_what_retires_it(self) -> None:
        meta = build_cilium_exempt(_req())["metadata"]
        assert WORKAROUND_LABEL in meta["labels"]
        assert meta["annotations"][WORKAROUND_REMOVE_WHEN]

    def test_link_watcher_targets_the_configured_interface(self) -> None:
        args = build_link_watcher(_req(interface="eno2"), KUBEOVN_NS)[
            "spec"]["template"]["spec"]["containers"][0]["args"][0]
        assert "ip link set dev eno2 up" in args

    def test_link_watcher_only_runs_on_gateway_nodes(self) -> None:
        spec = build_link_watcher(_req(), KUBEOVN_NS)["spec"]["template"]["spec"]
        assert spec["nodeSelector"] == {"ovn.kubernetes.io/external-gw": "true"}

    def test_cilium_exempt_is_per_endpoint_not_global(self) -> None:
        # The global switch would disable anti-spoofing for every pod; tenant
        # worker VMs are root-accessible to their tenants.
        args = build_cilium_exempt(_req())[
            "spec"]["template"]["spec"]["containers"][0]["args"][0]
        assert "SourceIPVerification=disable" in args
        assert "enable-source-ip-verification" not in args


@pytest.mark.asyncio
class TestEnsureEndpoint:
    def _k8s(self) -> MagicMock:
        k8s = MagicMock()
        k8s.custom_api.create_cluster_custom_object = AsyncMock()
        k8s.custom_api.create_namespaced_custom_object = AsyncMock()
        k8s.apps_api.create_namespaced_daemon_set = AsyncMock()
        return k8s

    def _request(self, k8s: MagicMock) -> MagicMock:
        request = MagicMock()
        request.app.state.k8s_client = k8s
        return request

    @pytest.fixture(autouse=True)
    def kubeovn_ns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _ns(_k8s: object) -> str:
            return KUBEOVN_NS

        import app.api.v1.network as network_mod
        monkeypatch.setattr(network_mod, "_find_kubeovn_namespace", _ns)

    async def test_builds_the_whole_fabric(self) -> None:
        k8s = self._k8s()

        result = await ensure_vpc_underlay(
            request=self._request(k8s), data=_req(), user=MagicMock(),
        )

        kinds = {o.kind for o in result.objects if not o.workaround}
        assert kinds == {
            "ProviderNetwork", "Vlan", "NetworkAttachmentDefinition", "Subnet",
        }
        assert result.ready is True

    async def test_is_idempotent(self) -> None:
        # Everything already there — a re-run must report success, not 409.
        k8s = self._k8s()
        k8s.custom_api.create_cluster_custom_object = AsyncMock(
            side_effect=ApiException(status=409, reason="AlreadyExists"),
        )
        k8s.custom_api.create_namespaced_custom_object = AsyncMock(
            side_effect=ApiException(status=409, reason="AlreadyExists"),
        )
        k8s.apps_api.create_namespaced_daemon_set = AsyncMock(
            side_effect=ApiException(status=409, reason="AlreadyExists"),
        )
        k8s.apps_api.patch_namespaced_daemon_set = AsyncMock()

        result = await ensure_vpc_underlay(
            request=self._request(k8s), data=_req(), user=MagicMock(),
        )

        assert result.ready is True
        assert all(o.state in ("exists", "skipped") for o in result.objects)

    async def test_existing_daemonset_is_reconciled_not_recreated(self) -> None:
        k8s = self._k8s()
        k8s.apps_api.create_namespaced_daemon_set = AsyncMock(
            side_effect=ApiException(status=409, reason="AlreadyExists"),
        )
        k8s.apps_api.patch_namespaced_daemon_set = AsyncMock()

        await ensure_vpc_underlay(
            request=self._request(k8s), data=_req(), user=MagicMock(),
        )

        k8s.apps_api.patch_namespaced_daemon_set.assert_awaited_once()

    async def test_one_failure_does_not_abort_the_rest(self) -> None:
        # A half-built fabric is easier to finish than to diagnose from a
        # single error, so every object is attempted and reported.
        k8s = self._k8s()
        k8s.custom_api.create_cluster_custom_object = AsyncMock(
            side_effect=ApiException(status=403, reason="Forbidden"),
        )

        result = await ensure_vpc_underlay(
            request=self._request(k8s), data=_req(), user=MagicMock(),
        )

        assert result.ready is False
        assert len(result.objects) == 6  # nothing skipped silently

    async def test_workarounds_are_opt_out(self) -> None:
        k8s = self._k8s()

        result = await ensure_vpc_underlay(
            request=self._request(k8s),
            data=_req(link_watcher=False), user=MagicMock(),
        )

        watcher = next(o for o in result.objects if o.name == LINK_WATCHER_NAME)
        assert watcher.state == "skipped"
        assert result.ready is True

    async def test_cilium_exemption_is_off_by_default(self) -> None:
        # A no-op cost on clusters that do not chain Cilium.
        k8s = self._k8s()

        result = await ensure_vpc_underlay(
            request=self._request(k8s), data=_req(), user=MagicMock(),
        )

        exempt = next(o for o in result.objects if o.name == CILIUM_EXEMPT_NAME)
        assert exempt.state == "skipped"
