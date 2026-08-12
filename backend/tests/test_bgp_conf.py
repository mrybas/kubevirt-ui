"""Unit tests for BgpConf and the egress gateway's BGP wiring.

A VpcEgressGateway without `spec.bgpConf` never runs FRR, so a UI-created VPC
is reachable only via NAT and hand-written static routes. One shared BgpConf
serves every gateway — verified on the lab with four gateways on one config,
all four sessions Established.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.api.v1 import egress_gateway
from app.api.v1.bgp import _build_bgpconf_spec, _parse_bgpconf
from app.models.bgp import BgpConfRequest
from app.models.egress_gateway import EgressGatewayCreateRequest


class TestBgpConfSpec:
    def _req(self, **kw: object) -> BgpConfRequest:
        base = {"local_asn": 65001, "peer_asn": 65000, "neighbours": ["10.198.191.254"]}
        base.update(kw)
        return BgpConfRequest(**base)  # type: ignore[arg-type]

    def test_spec_carries_the_session_parameters(self) -> None:
        spec = _build_bgpconf_spec(self._req())
        assert spec["localASN"] == 65001
        assert spec["peerASN"] == 65000
        assert spec["neighbours"] == ["10.198.191.254"]

    def test_router_id_is_never_set(self) -> None:
        # One shared config serves every gateway only because FRR derives a
        # router-id per gateway from its internal address. Pinning one here
        # would give them all the same id, which the peer rejects in one AS.
        assert "routerId" not in _build_bgpconf_spec(self._req())

    def test_timers_default_to_the_verified_values(self) -> None:
        spec = _build_bgpconf_spec(self._req())
        assert spec["holdTime"] == "30s"
        assert spec["keepaliveTime"] == "10s"
        assert spec["gracefulRestart"] is True

    def test_default_name_is_the_shared_one(self) -> None:
        assert self._req().name == "lab-gateway-common"

    def test_neighbour_addresses_are_validated(self) -> None:
        with pytest.raises(ValidationError):
            self._req(neighbours=["not-an-ip"])

    def test_at_least_one_neighbour_required(self) -> None:
        with pytest.raises(ValidationError):
            self._req(neighbours=[])

    def test_parse_round_trips(self) -> None:
        item = {
            "metadata": {"name": "lab-gateway-common"},
            "spec": _build_bgpconf_spec(self._req()),
        }
        parsed = _parse_bgpconf(item)
        assert parsed.name == "lab-gateway-common"
        assert parsed.local_asn == 65001
        assert parsed.router_id == ""


class TestGatewayRequestDefaults:
    def _req(self, **kw: object) -> EgressGatewayCreateRequest:
        base = {"name": "t1-egress", "macvlan_subnet": "ext-sub"}
        base.update(kw)
        return EgressGatewayCreateRequest(**base)  # type: ignore[arg-type]

    def test_snat_defaults_on(self) -> None:
        # Unchanged behaviour for the masqueraded case.
        assert self._req().snat is True

    def test_snat_can_be_turned_off_for_a_routed_tenant(self) -> None:
        assert self._req(snat=False).snat is False

    def test_bgp_conf_defaults_to_none(self) -> None:
        assert self._req().bgp_conf is None

    def test_external_ips_default_empty(self) -> None:
        assert self._req().external_ips == []


@pytest.mark.asyncio
class TestUpdateVegPolicies:
    """Attach/detach writes policies kube-ovn actually reads."""

    def _k8s(self, policies: list[dict]) -> MagicMock:
        k8s = MagicMock()
        k8s.custom_api.patch_namespaced_custom_object = AsyncMock()
        return k8s

    async def _run(
        self, policies: list[dict], monkeypatch: pytest.MonkeyPatch, **kw: object,
    ) -> list[dict]:
        k8s = self._k8s(policies)
        monkeypatch.setattr(
            egress_gateway, "_get_vpc_egress_gateway",
            AsyncMock(return_value={"spec": {"policies": policies}}),
        )
        await egress_gateway._update_veg_policies(k8s, "t1-egress", **kw)  # type: ignore[arg-type]
        patch = k8s.custom_api.patch_namespaced_custom_object.await_args.kwargs["body"]
        return patch["spec"]["policies"]

    async def test_added_cidr_uses_ipblocks_not_cidr(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `cidr` is not a field on the CRD — kube-ovn silently ignored it, so
        # the tenant's traffic never went through the gateway.
        result = await self._run(
            [{"snat": True, "subnets": ["gw-sub"]}], monkeypatch,
            add_cidr="10.198.224.0/20",
        )
        added = result[-1]
        assert added["ipBlocks"] == ["10.198.224.0/20"]
        assert "cidr" not in added

    async def test_snat_is_inherited_not_forced(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A routed tenant runs snat=false on purpose; forcing true would
        # masquerade it and undo the BGP announcement.
        result = await self._run(
            [{"snat": False, "subnets": ["gw-sub"]}], monkeypatch,
            add_cidr="10.198.224.0/20",
        )
        assert result[-1]["snat"] is False

    async def test_adding_an_existing_cidr_is_a_no_op(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = await self._run(
            [{"snat": True, "ipBlocks": ["10.198.224.0/20"]}], monkeypatch,
            add_cidr="10.198.224.0/20",
        )
        assert len(result) == 1

    async def test_removing_a_cidr_drops_the_emptied_policy(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = await self._run(
            [{"snat": True, "ipBlocks": ["10.198.224.0/20"]}], monkeypatch,
            remove_cidr="10.198.224.0/20",
        )
        assert result == []

    async def test_removing_keeps_the_other_blocks_and_subnet_policies(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = await self._run(
            [
                {"snat": True, "subnets": ["gw-sub"]},
                {"snat": True, "ipBlocks": ["10.198.224.0/20", "10.198.240.0/20"]},
            ],
            monkeypatch, remove_cidr="10.198.224.0/20",
        )
        assert result[0]["subnets"] == ["gw-sub"]
        assert result[1]["ipBlocks"] == ["10.198.240.0/20"]

    async def test_missing_gateway_is_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        k8s = self._k8s([])
        monkeypatch.setattr(
            egress_gateway, "_get_vpc_egress_gateway", AsyncMock(return_value=None),
        )
        await egress_gateway._update_veg_policies(k8s, "gone", add_cidr="10.0.0.0/24")
        k8s.custom_api.patch_namespaced_custom_object.assert_not_awaited()

