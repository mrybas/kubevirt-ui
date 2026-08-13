"""Unit tests for DNS inside a custom kube-ovn VPC.

The failure this guards against is silent: a VpcDns pod's secondary NIC gets a
single `10.96.0.1/32` route into the default overlay, so the kube-dns ClusterIP
is unreachable and the Corefile used to pin CoreDNS *pod* IPs instead. Pod IPs
move. Measured on the lab: after a `rollout restart coredns` the pinned
Corefile answered nothing while VpcDns still reported ACTIVE=true with both
pods 1/1 Running.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client import ApiException

from app.api.v1 import vpcs
from app.api.v1.vpcs import (
    VPCDNS_ROUTES_ANNOTATION,
    _build_vpc_dns_corefile,
    _ensure_vpc_dns_service_route,
    _overlay_gateway,
)

HOST_SERVICE_CIDR = "10.96.0.0/12"
OVERLAY_GW = "10.16.0.1"
CLUSTER_DNS = "10.96.0.10"


class TestCorefile:
    def test_forwards_everything_to_cluster_dns(self) -> None:
        # Not a split zone config: the public half used to go to 8.8.8.8 and
        # timed out, because a VpcDns pod runs in the kube-ovn namespace while
        # the egress gateway selects the tenant namespace — it is never
        # steered and has no route out at all.
        corefile = _build_vpc_dns_corefile(CLUSTER_DNS)

        assert f"forward . {CLUSTER_DNS}" in corefile
        assert "8.8.8.8" not in corefile
        assert corefile.count("forward .") == 1

    def test_has_no_separate_cluster_local_zone(self) -> None:
        corefile = _build_vpc_dns_corefile(CLUSTER_DNS)
        assert "cluster.local:53" not in corefile
        assert corefile.lstrip().startswith(".:53")

    def test_keeps_reload_so_configmap_edits_apply_without_restart(self) -> None:
        assert "reload" in _build_vpc_dns_corefile(CLUSTER_DNS)

    def test_never_pins_a_pod_ip(self) -> None:
        # Whatever it forwards to must be what the caller passed — a stable
        # ClusterIP — with no discovered pod addresses baked in.
        corefile = _build_vpc_dns_corefile(CLUSTER_DNS)
        assert "10.16.0." not in corefile


@pytest.mark.asyncio
class TestOverlayGateway:
    async def test_reads_the_gateway_from_the_subnet(self) -> None:
        k8s = MagicMock()
        k8s.custom_api.get_cluster_custom_object = AsyncMock(
            return_value={"spec": {"gateway": OVERLAY_GW}},
        )

        assert await _overlay_gateway(k8s) == OVERLAY_GW

    async def test_missing_subnet_yields_none(self) -> None:
        # Assuming 10.16.0.1 would produce exactly the silent failure the
        # route exists to prevent, so a missing subnet must return nothing.
        k8s = MagicMock()
        k8s.custom_api.get_cluster_custom_object = AsyncMock(
            side_effect=ApiException(status=404, reason="Not Found"),
        )

        assert await _overlay_gateway(k8s) is None


@pytest.mark.asyncio
class TestServiceRoute:
    @pytest.fixture(autouse=True)
    def cluster_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            vpcs, "_host_service_cidr", lambda: HOST_SERVICE_CIDR,
        )
        monkeypatch.setattr(
            vpcs, "_overlay_gateway", AsyncMock(return_value=OVERLAY_GW),
        )

        async def _ns(_k8s: object) -> str:
            return "kube-ovn"

        import app.api.v1.network as network_mod
        monkeypatch.setattr(network_mod, "_find_kubeovn_namespace", _ns)

    def _k8s(self) -> MagicMock:
        k8s = MagicMock()
        k8s.apps_api.patch_namespaced_deployment = AsyncMock()
        return k8s

    async def test_annotates_the_deployment_with_the_service_route(self) -> None:
        k8s = self._k8s()

        assert await _ensure_vpc_dns_service_route(k8s, "t1-vpc") is True

        kwargs = k8s.apps_api.patch_namespaced_deployment.await_args.kwargs
        assert kwargs["name"] == "vpc-dns-t1-vpc-dns"
        assert kwargs["namespace"] == "kube-ovn"
        annotations = (
            kwargs["body"]["spec"]["template"]["metadata"]["annotations"]
        )
        assert annotations[VPCDNS_ROUTES_ANNOTATION] == (
            f'[{{"dst": "{HOST_SERVICE_CIDR}", "gw": "{OVERLAY_GW}"}}]'
        )

    async def test_deployment_not_created_yet_is_not_an_error(self) -> None:
        # kube-ovn creates the Deployment after the CR, so the create path
        # regularly runs before it exists; the recreate endpoint applies it.
        k8s = self._k8s()
        k8s.apps_api.patch_namespaced_deployment = AsyncMock(
            side_effect=ApiException(status=404, reason="Not Found"),
        )

        assert await _ensure_vpc_dns_service_route(k8s, "t1-vpc") is False

    async def test_other_api_errors_do_not_raise(self) -> None:
        k8s = self._k8s()
        k8s.apps_api.patch_namespaced_deployment = AsyncMock(
            side_effect=ApiException(status=403, reason="Forbidden"),
        )

        assert await _ensure_vpc_dns_service_route(k8s, "t1-vpc") is False

    async def test_unknown_service_cidr_skips_the_patch(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(vpcs, "_host_service_cidr", lambda: None)
        k8s = self._k8s()

        assert await _ensure_vpc_dns_service_route(k8s, "t1-vpc") is False
        k8s.apps_api.patch_namespaced_deployment.assert_not_awaited()

    async def test_unknown_overlay_gateway_skips_the_patch(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(vpcs, "_overlay_gateway", AsyncMock(return_value=None))
        k8s = self._k8s()

        assert await _ensure_vpc_dns_service_route(k8s, "t1-vpc") is False
        k8s.apps_api.patch_namespaced_deployment.assert_not_awaited()


class TestVpcDnsConfigRoutesTheForwardTarget:
    """kube-ovn turns `k8s-service-host` into the one /32 route it puts on the
    VpcDns pod's secondary NIC. Left at its default — the kubernetes API
    address — the pod can reach the API and nothing else, so every forward to
    the kube-dns ClusterIP times out and each query answers SERVFAIL while
    VpcDns still reports ACTIVE=true. Measured on the lab before the fix."""

    def _config_data(self, source: str) -> dict:
        import re
        block = re.search(r'name="vpc-dns-config".*?\n\s*\)', source, re.S)
        assert block, "vpc-dns-config ConfigMap literal not found"
        return block.group(0)

    def test_forward_target_is_named_as_the_service_host(self) -> None:
        import inspect
        source = inspect.getsource(vpcs._ensure_vpc_dns_prereqs)
        block = self._config_data(source)

        assert '"k8s-service-host": _vpcdns_forward_dns()' in block
        assert '"k8s-service-port": "53"' in block

    def test_an_existing_configmap_is_brought_forward(self) -> None:
        # Create-if-missing alone would leave every cluster built before this
        # fix with a route to the API server and no DNS in any VPC.
        import inspect
        source = inspect.getsource(vpcs._ensure_vpc_dns_prereqs)
        after_conflict = source.split('Created VpcDns prereq ConfigMap', 1)[1]
        patch_call = after_conflict.split('vpc-dns-corefile', 1)[0]

        assert 'patch_namespaced_config_map' in patch_call
        assert 'k8s-service-host' in patch_call
