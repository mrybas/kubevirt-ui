"""The last constant mine in the gateway path.

`gw_vpc_cidr` used to default to 10.199.0.0/24. On a cluster where that range
belongs to something else, an API caller who simply omitted the field got a
gateway VPC quietly built on top of a network in use — and the collision only
shows up later, as traffic that goes to the wrong place.

There is no safe default here: the right value depends on what the cluster is
already using, which is what GET /egress-gateways/suggest-cidrs computes. The
UI reads it from there; every other caller has to decide on purpose.
"""

import pytest
from pydantic import ValidationError

from app.models.egress_gateway import EgressGatewayCreateRequest


def _payload(**overrides):
    base = {
        "name": "shared-egress",
        "gw_vpc_cidr": "10.199.128.0/24",
        "transit_cidr": "10.199.129.0/24",
        "macvlan_subnet": "external",
    }
    base.update(overrides)
    return base


def test_omitting_the_gateway_cidr_is_refused() -> None:
    payload = _payload()
    del payload["gw_vpc_cidr"]

    with pytest.raises(ValidationError) as exc:
        EgressGatewayCreateRequest(**payload)

    assert "gw_vpc_cidr" in str(exc.value)


def test_no_default_is_silently_substituted() -> None:
    """Guards against the default coming back as a "convenience"."""
    field = EgressGatewayCreateRequest.model_fields["gw_vpc_cidr"]

    assert field.is_required()


def test_an_explicit_cidr_still_works() -> None:
    req = EgressGatewayCreateRequest(**_payload())

    assert req.gw_vpc_cidr == "10.199.128.0/24"
