"""Every VPC is a hybrid: its egress is the shared gateway, not a per-VPC NAT.

kube-ovn keeps one SNAT per logical IP. On a hybrid VPC that slot belongs to
the control-plane transit path — the tenant's workers reach their API VIP
under a transit address, and the reply only finds its way back because the
node already knows that address on `br-cptransit`. A per-VPC NAT gateway takes
the slot and SNATs everything to the external network instead. Measured on the
lab as the `t2` incident: internet worked, the control plane was unreachable,
and every CR was green.

The code path stays behind `VPC_NAT_GATEWAY_ENABLED` because the topology
where it is the right answer — an external subnet the upstream already NATs —
is on the roadmap. The default is not that topology.
"""

import pytest

from app.api.v1.vpcs import _vpc_nat_gateway_enabled


def test_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("VPC_NAT_GATEWAY_ENABLED", raising=False)
    assert _vpc_nat_gateway_enabled() is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes"])
def test_explicit_opt_in_is_honoured(monkeypatch, value: str) -> None:
    monkeypatch.setenv("VPC_NAT_GATEWAY_ENABLED", value)
    assert _vpc_nat_gateway_enabled() is True


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off"])
def test_anything_else_is_off(monkeypatch, value: str) -> None:
    monkeypatch.setenv("VPC_NAT_GATEWAY_ENABLED", value)
    assert _vpc_nat_gateway_enabled() is False


def test_the_request_field_alone_does_not_enable_it(monkeypatch) -> None:
    """A client that still sends the old flag must not get the old behaviour."""
    monkeypatch.delenv("VPC_NAT_GATEWAY_ENABLED", raising=False)
    from app.models.vpc import VpcCreateRequest

    req = VpcCreateRequest(name="v1", enable_nat_gateway=True)

    assert req.enable_nat_gateway is True, "the field is still accepted"
    assert _vpc_nat_gateway_enabled() is False, "but it is not what decides"
