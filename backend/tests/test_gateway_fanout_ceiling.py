"""A hub has a hard fan-out limit, and it should not arrive as a surprise.

Every attached VPC takes one address on the gateway's transit network for its
peering leg, so the transit width *is* the number of VPCs the gateway can ever
serve — 253 for a /24. It cannot be widened afterwards: the transit subnet's
prefix is fixed when the gateway is created.

Before this, the limit lived at the bottom of the allocator as
"Transit CIDR 10.199.129.0/24 exhausted", with nothing to tell the reader that
the fix is a second gateway rather than a retry.
"""

import ipaddress

import pytest
from fastapi import HTTPException

from app.api.v1.egress_gateway import (
    _allocate_transit_ip,
    gateway_subnet_prefix,
    transit_capacity,
)


class TestTheCeilingIsAKnownNumber:
    def test_a_24_hub_holds_253_vpcs(self) -> None:
        # 256 addresses, minus network, broadcast, and the gateway's own .1
        assert transit_capacity("10.199.129.0/24") == 253

    def test_a_wider_transit_holds_proportionally_more(self) -> None:
        assert transit_capacity("10.199.128.0/22") == 1021

    def test_it_matches_what_the_allocator_will_actually_hand_out(self) -> None:
        """The stated ceiling has to be the real one, or it is just decoration."""
        cidr = "10.199.129.0/28"
        used: set[str] = set()
        handed_out = 0
        while True:
            try:
                used.add(_allocate_transit_ip(cidr, used))
                handed_out += 1
            except HTTPException:
                break

        assert handed_out == transit_capacity(cidr)

    def test_an_unreadable_cidr_claims_no_capacity(self) -> None:
        assert transit_capacity("") == 0
        assert transit_capacity("not-a-cidr") == 0


class TestRunningOutSaysWhatToDo:
    def test_the_refusal_names_the_ceiling_and_the_way_out(self) -> None:
        cidr = "10.199.129.0/29"
        network = ipaddress.IPv4Network(cidr)
        used = {str(h) for h in network.hosts()}

        with pytest.raises(HTTPException) as e:
            _allocate_transit_ip(cidr, used)

        assert e.value.status_code == 409
        detail = e.value.detail
        assert cidr in detail
        assert str(transit_capacity(cidr)) in detail
        assert "second egress gateway" in detail
        assert "EGRESS_GW_SUBNET_PREFIX" in detail


class TestTheWidthIsADeploymentSetting:
    def test_it_defaults_to_a_24(self, monkeypatch) -> None:
        monkeypatch.delenv("EGRESS_GW_SUBNET_PREFIX", raising=False)

        assert gateway_subnet_prefix() == 24

    def test_a_deployment_expecting_more_vpcs_can_widen_it(self, monkeypatch) -> None:
        monkeypatch.setenv("EGRESS_GW_SUBNET_PREFIX", "22")

        assert gateway_subnet_prefix() == 22
        assert transit_capacity("10.199.128.0/22") > 1000

    @pytest.mark.parametrize("bad", ["", "twenty-four", "0", "31", "-1"])
    def test_nonsense_falls_back_instead_of_crashing_the_form(
        self, monkeypatch, bad: str,
    ) -> None:
        """A typo in an env var must not take the create dialog down with it."""
        monkeypatch.setenv("EGRESS_GW_SUBNET_PREFIX", bad)

        assert gateway_subnet_prefix() == 24
