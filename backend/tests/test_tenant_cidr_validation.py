"""Unit tests for tenant/host CIDR collision checks.

A tenant whose service CIDR contains the HOST cluster's DNS address makes its
own kube-proxy hijack that address to the tenant's CoreDNS, which has no
endpoints until a CNI is running — and the CNI image cannot be pulled because
resolution is already dead. It only surfaces once kube-proxy starts, i.e.
after the node joined, so it reads as anything but DNS.
"""

import pytest
from fastapi import HTTPException

from app.api.v1 import tenants_common
from app.api.v1.tenants_common import (
    assert_tenant_cidrs_free,
    find_tenant_cidr_conflicts,
)

HOST_SERVICE = "10.96.0.0/12"
HOST_DNS = "10.96.0.10"


class TestFindConflicts:
    def test_service_cidr_containing_host_dns_is_rejected(self) -> None:
        # The exact case observed on the lab: tenant on 10.96.0.0/16 against a
        # host CoreDNS at 10.96.0.10.
        reasons = find_tenant_cidr_conflicts(
            service_cidr="10.96.0.0/16", pod_cidr="10.244.0.0/16",
            host_service_cidr=HOST_SERVICE, host_dns_ip=HOST_DNS,
        )
        assert len(reasons) == 1
        assert "10.96.0.10" in reasons[0]
        assert "kube-proxy" in reasons[0]

    def test_pod_cidr_containing_host_dns_is_rejected(self) -> None:
        reasons = find_tenant_cidr_conflicts(
            service_cidr="10.32.0.0/16", pod_cidr="10.96.0.0/16",
            host_service_cidr=None, host_dns_ip=HOST_DNS,
        )
        assert any("pod CIDR" in r for r in reasons)

    def test_overlap_without_the_dns_address_is_still_rejected(self) -> None:
        # 10.100.0.0/16 sits inside the host's 10.96.0.0/12 but does not hold
        # 10.96.0.10 — still a collision, just a less spectacular one.
        reasons = find_tenant_cidr_conflicts(
            service_cidr="10.100.0.0/16", pod_cidr="192.168.0.0/16",
            host_service_cidr=HOST_SERVICE, host_dns_ip=HOST_DNS,
        )
        assert len(reasons) == 1
        assert "overlaps" in reasons[0]

    def test_disjoint_ranges_pass(self) -> None:
        assert find_tenant_cidr_conflicts(
            service_cidr="10.32.0.0/16", pod_cidr="10.244.0.0/16",
            host_service_cidr=HOST_SERVICE, host_dns_ip=HOST_DNS,
        ) == []

    def test_one_reason_per_range_not_two(self) -> None:
        # A service CIDR that both contains the DNS address and overlaps the
        # host range should report the DNS reason only — it explains more.
        reasons = find_tenant_cidr_conflicts(
            service_cidr="10.96.0.0/16", pod_cidr="10.244.0.0/16",
            host_service_cidr=HOST_SERVICE, host_dns_ip=HOST_DNS,
        )
        assert len(reasons) == 1

    def test_both_ranges_can_conflict_at_once(self) -> None:
        reasons = find_tenant_cidr_conflicts(
            service_cidr="10.100.0.0/16", pod_cidr="10.101.0.0/16",
            host_service_cidr=HOST_SERVICE, host_dns_ip=None,
        )
        assert len(reasons) == 2

    def test_undiscovered_host_ranges_never_block(self) -> None:
        # A failed lookup must not stop tenant creation.
        assert find_tenant_cidr_conflicts(
            service_cidr="10.96.0.0/16", pod_cidr="10.96.0.0/16",
            host_service_cidr=None, host_dns_ip=None,
        ) == []

    def test_malformed_tenant_cidr_is_not_a_conflict(self) -> None:
        # Pydantic already rejects these; the checker must not raise on them.
        assert find_tenant_cidr_conflicts(
            service_cidr="not-a-cidr", pod_cidr="also-bad",
            host_service_cidr=HOST_SERVICE, host_dns_ip=HOST_DNS,
        ) == []

    def test_malformed_host_values_are_ignored(self) -> None:
        assert find_tenant_cidr_conflicts(
            service_cidr="10.96.0.0/16", pod_cidr="10.244.0.0/16",
            host_service_cidr="garbage", host_dns_ip="also-garbage",
        ) == []

    def test_ipv6_tenant_range_is_not_compared_to_ipv4_host(self) -> None:
        assert find_tenant_cidr_conflicts(
            service_cidr="fd00::/108", pod_cidr="fd01::/64",
            host_service_cidr=HOST_SERVICE, host_dns_ip=HOST_DNS,
        ) == []


class TestAssertRaises:
    @pytest.fixture(autouse=True)
    def host_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            tenants_common, "_host_service_cidr", lambda: HOST_SERVICE,
        )
        monkeypatch.setattr(
            tenants_common, "_vpcdns_forward_dns", lambda: HOST_DNS,
        )

    def test_conflict_is_422_naming_the_address(self) -> None:
        with pytest.raises(HTTPException) as exc:
            assert_tenant_cidrs_free("10.96.0.0/16", "10.244.0.0/16")

        assert exc.value.status_code == 422
        assert HOST_DNS in exc.value.detail

    def test_clean_plan_passes(self) -> None:
        assert_tenant_cidrs_free("10.32.0.0/16", "10.244.0.0/16")

    def test_shipped_defaults_pass_on_a_standard_cluster(self) -> None:
        # The defaults used to be 10.96.0.0/12 — byte-for-byte the host's own
        # service CIDR on kubeadm and Talos — so the out-of-the-box tenant was
        # exactly the broken combination. Guard that they stay disjoint.
        from app.models.tenant import TenantCreateRequest

        req = TenantCreateRequest(
            name="t", display_name="T", folder="f", environment="e",
        )
        assert_tenant_cidrs_free(req.service_cidr, req.pod_cidr)

    def test_the_old_default_would_now_be_caught(self) -> None:
        with pytest.raises(HTTPException):
            assert_tenant_cidrs_free("10.96.0.0/12", "10.244.0.0/16")
