"""Every pod-level check asked for a label kube-ovn never sets.

Measured on the lab, 2026-08-17, on a healthy `shared-egress`:

    labels: app=vpc-egress-gateway
            ovn.kubernetes.io/vpc-egress-gateway=shared-egress
            pod-template-hash=bb9f8bbd4

The code asked for `kubevirt-ui.io/egress-gateway=shared-egress` — our own
label, which lives on the VPC and the subnets we create, never on a Deployment
kube-ovn owns. So the pod list came back empty and every check built on it
reported nothing wrong:

  * `assigned_ips` was permanently empty;
  * the announcement-lag check — written precisely because spec-level checks
    all passed while a freshly attached VPC had no internet — never fired. It
    was proved live: attach t3-vpc, and the API went Ready → Not Ready →
    Ready without ever reporting the lag, while the pods demonstrably carried
    the old route set for ~35 seconds.

Its unit tests stayed green throughout, because they hand the pod list in
directly and never exercise the selector. Hence this test, which asserts the
one thing those cannot: that the string we send to the API server is the one
kube-ovn answers to.
"""

import inspect

import pytest

from app.api.v1 import egress_gateway
from app.api.v1.egress_gateway import gateway_pod_selector

# Verbatim from `kubectl get pods -n kube-system -l app=vpc-egress-gateway
# -o jsonpath='{.items[*].metadata.labels}'` on the lab.
LIVE_POD_LABELS = {
    "app": "vpc-egress-gateway",
    "ovn.kubernetes.io/vpc-egress-gateway": "shared-egress",
    "pod-template-hash": "bb9f8bbd4",
}


def _matches(selector: str, labels: dict[str, str]) -> bool:
    """Equality-based selector semantics, which is all we use here."""
    for term in selector.split(","):
        key, _, value = term.partition("=")
        if labels.get(key.strip()) != value.strip():
            return False
    return True


def test_the_selector_matches_a_real_gateway_pod() -> None:
    assert _matches(gateway_pod_selector("shared-egress"), LIVE_POD_LABELS)


def test_it_does_not_match_another_gateways_pods() -> None:
    other = {**LIVE_POD_LABELS, "ovn.kubernetes.io/vpc-egress-gateway": "other-egress"}

    assert not _matches(gateway_pod_selector("shared-egress"), other)


def test_our_own_label_is_not_used_for_pods() -> None:
    """The exact mistake: GATEWAY_LABEL is for the VPC and subnets, not pods."""
    assert egress_gateway.GATEWAY_LABEL not in gateway_pod_selector("shared-egress")


@pytest.mark.parametrize(
    "func",
    [
        egress_gateway._get_gateway_pod_ips,
        egress_gateway._unannounced_cidrs,
        egress_gateway._pod_cause,
    ],
)
def test_every_pod_lookup_goes_through_the_helper(func) -> None:
    """No hand-written selector may drift away from kube-ovn's again."""
    source = inspect.getsource(func)

    assert "gateway_pod_selector(" in source
    assert "kubevirt-ui.io/egress-gateway=" not in source


class TestTheExternalAddressIsReadFromTheRightKey:
    """`ovn.kubernetes.io/provider_network_ip` is not a key kube-ovn writes.

    Multus puts each extra attachment under its own prefix, so the external leg
    is `external.o0-kube-ovn.ovn.kubernetes.io/ip_address`. Reading the invented
    key left `external_ip` empty on every pod — and, being empty rather than
    wrong, it looked like "no address yet" instead of "we asked for nothing".
    """

    # Verbatim from the lab pod.
    LIVE = {
        "ovn.kubernetes.io/ip_address": "10.199.128.5",
        "ovn.kubernetes.io/logical_switch": "egw-shared-egress-subnet",
        "external.o0-kube-ovn.ovn.kubernetes.io/ip_address": "10.199.4.7",
        "external.o0-kube-ovn.ovn.kubernetes.io/provider_network": "external",
    }

    def test_the_external_leg_is_found(self) -> None:
        assert egress_gateway._secondary_ip(self.LIVE) == "10.199.4.7"

    def test_the_internal_leg_is_not_mistaken_for_it(self) -> None:
        assert egress_gateway._secondary_ip(self.LIVE) != "10.199.128.5"

    def test_a_pod_with_no_second_attachment_yields_empty(self) -> None:
        assert egress_gateway._secondary_ip(
            {"ovn.kubernetes.io/ip_address": "10.199.128.5"}
        ) == ""
