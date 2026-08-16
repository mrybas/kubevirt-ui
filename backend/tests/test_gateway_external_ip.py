"""The External IP column must show the addresses the gateway actually holds.

The column read `assigned_ips`, which came from listing pods by
`kubevirt-ui.io/egress-gateway=<name>`. kube-ovn creates those pods itself and
labels them `ovn.kubernetes.io/vpc-egress-gateway=<gw-vpc>`, so the selector
matched nothing and the column rendered "-" on a gateway that was up with two
Established BGP sessions and `status.externalIPs = [10.199.4.14, 10.199.4.16]`
(backlog U17).

`status` on the VpcEgressGateway is the authoritative answer and needs no label
guessing, so it is the source now; per-pod detail stays as an enrichment.
"""

from unittest.mock import MagicMock

import pytest

from app.api.v1.egress_gateway import _parse_gateway

GW_VPC = {
    "metadata": {
        "name": "egw-team-a",
        "labels": {"kubevirt-ui.io/egress-gateway": "team-a"},
        "annotations": {"kubevirt-ui.io/transit-cidr": "10.255.0.0/24"},
    },
    "spec": {},
    "status": {},
}


def test_external_ips_come_from_the_gateway_status() -> None:
    veg = {
        "spec": {"replicas": 2},
        "status": {
            "ready": True,
            "internalIPs": ["10.199.16.2", "10.199.16.4"],
            "externalIPs": ["10.199.4.14", "10.199.4.16"],
        },
    }

    out = _parse_gateway(GW_VPC, veg, attached=[], assigned_ips=[])

    assert out.external_ips == ["10.199.4.14", "10.199.4.16"]
    assert out.internal_ips == ["10.199.16.2", "10.199.16.4"]


def test_no_status_means_no_addresses_not_a_crash() -> None:
    out = _parse_gateway(GW_VPC, {"spec": {}, "status": {}}, attached=[], assigned_ips=[])
    assert out.external_ips == []


def test_pod_derived_ips_still_ride_along() -> None:
    from app.models.egress_gateway import GatewayPodInfo

    veg = {"spec": {}, "status": {"externalIPs": ["10.199.4.14"]}}
    pods = [GatewayPodInfo(pod="p1", node="w1", internal_ip="10.199.16.2", external_ip="10.199.4.14")]

    out = _parse_gateway(GW_VPC, veg, attached=[], assigned_ips=pods)

    assert out.external_ips == ["10.199.4.14"]
    assert out.assigned_ips[0].pod == "p1"


def test_a_condition_older_than_ready_is_not_reported() -> None:
    """A Validated=False left over from generation 1 is history, not a problem.

    kube-ovn does not clear conditions when a later reconcile succeeds, so a
    gateway that failed validation once and then went Ready keeps both. Passing
    the raw list through invites every consumer to draw the stale one
    (backlog U21).
    """
    veg = {
        "spec": {},
        "status": {
            "ready": True,
            "conditions": [
                {"type": "Validated", "status": "False",
                 "lastTransitionTime": "2026-08-16T09:20:23Z",
                 "message": "vpc egw-egw-team-a bfd port is not enabled or not ready"},
                {"type": "Ready", "status": "True",
                 "lastTransitionTime": "2026-08-16T09:58:25Z"},
            ],
        },
    }

    out = _parse_gateway(GW_VPC, veg, attached=[], assigned_ips=[])

    types = {c["type"] for c in (out.status or {}).get("conditions", [])}
    assert "Validated" not in types, "a condition that predates Ready=True is still shown"
    assert "Ready" in types


def test_a_condition_newer_than_ready_is_kept() -> None:
    veg = {
        "spec": {},
        "status": {
            "ready": False,
            "conditions": [
                {"type": "Ready", "status": "True",
                 "lastTransitionTime": "2026-08-16T09:00:00Z"},
                {"type": "Validated", "status": "False",
                 "lastTransitionTime": "2026-08-16T10:00:00Z", "message": "broke later"},
            ],
        },
    }

    out = _parse_gateway(GW_VPC, veg, attached=[], assigned_ips=[])

    types = {c["type"] for c in (out.status or {}).get("conditions", [])}
    assert "Validated" in types, "a failure newer than Ready must survive"
