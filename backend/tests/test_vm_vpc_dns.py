"""A VM on a VPC overlay must be told a resolver it can reach.

Measured in the guest of a VM created through the wizard onto `t1-default`:

    resolvectl status
        Current DNS Server: 10.96.0.10
    getent hosts one.one.one.one            -> (nothing)
    getent hosts kubernetes.default.svc...  -> (nothing)

10.96.0.10 is the cluster CoreDNS ClusterIP and has no route from inside a
VPC — proven separately from a pod on the same subnet (rc=2). The subnet's own
DHCP offers the right address:

    dhcpV4Options: ... dns_server=10.96.0.200

but never reaches the guest: with bridge binding KubeVirt serves the guest
from the launcher pod, and that pod had dnsPolicy=ClusterFirst with no
dnsConfig. So the launcher is told directly.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1 import vms as vms_mod

VIP = "10.96.0.200"


def _subnet(vpc: str, vlan: str | None = None) -> dict:
    spec: dict = {"vpc": vpc}
    if vlan:
        spec["vlan"] = vlan
    return {"spec": spec}


@pytest.fixture
def vip(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(vms_mod, "_vpc_dns_vip_or_none", lambda: VIP)


def _stub_config(monkeypatch: pytest.MonkeyPatch, vip):
    """Replace the lazy cluster-config load and the VIP getter."""
    import app.api.v1.tenants_common as tc

    async def _ensure(_k8s):
        return {}

    monkeypatch.setattr(tc, "_ensure_cluster_config", _ensure)
    if callable(vip):
        monkeypatch.setattr(tc, "_vpcdns_vip", vip)
    else:
        monkeypatch.setattr(tc, "_vpcdns_vip", lambda: vip)


@pytest.mark.asyncio
async def test_the_vip_helper_survives_a_missing_cluster_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A VM must still be creatable when the VIP is not configured."""
    def _boom() -> str:
        raise RuntimeError("cluster config not loaded")

    _stub_config(monkeypatch, _boom)
    assert await vms_mod._vpc_dns_vip_or_none(MagicMock()) is None


@pytest.mark.asyncio
async def test_the_vip_helper_returns_the_configured_vip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_config(monkeypatch, VIP)
    assert await vms_mod._vpc_dns_vip_or_none(MagicMock()) == VIP


@pytest.mark.asyncio
async def test_an_empty_vip_reads_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_config(monkeypatch, "")
    assert await vms_mod._vpc_dns_vip_or_none(MagicMock()) is None


@pytest.mark.asyncio
async def test_it_loads_the_cluster_config_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config is lazy; without the await the VIP getter refuses.

    That is exactly how the first version of this shipped: it logged
    "_ensure_cluster_config(k8s) must be awaited before this call" and left
    every VM on the cluster resolver.
    """
    import app.api.v1.tenants_common as tc

    loaded = []

    async def _ensure(_k8s):
        loaded.append(True)
        return {}

    monkeypatch.setattr(tc, "_ensure_cluster_config", _ensure)
    monkeypatch.setattr(
        tc, "_vpcdns_vip",
        lambda: VIP if loaded else (_ for _ in ()).throw(
            RuntimeError("_ensure_cluster_config(k8s) must be awaited before this call")
        ),
    )

    assert await vms_mod._vpc_dns_vip_or_none(MagicMock()) == VIP
    assert loaded, "the cluster config was never loaded"


def test_the_spec_names_the_vip_and_keeps_cluster_search_domains() -> None:
    spec = vms_mod.build_vpc_dns_spec("e2e-lab-prod", VIP)
    assert spec["dnsPolicy"] == "None", (
        "ClusterFirst would merge the cluster resolver back in"
    )
    assert spec["dnsConfig"]["nameservers"] == [VIP]
    assert spec["dnsConfig"]["searches"] == [
        "e2e-lab-prod.svc.cluster.local", "svc.cluster.local", "cluster.local",
    ]
    assert {"name": "ndots", "value": "5"} in spec["dnsConfig"]["options"]


def test_it_matches_what_the_kyverno_policy_writes() -> None:
    """Two mechanisms, one answer — a cluster may run both."""
    from app.api.v1.vpcs import _build_kyverno_dns_policy

    policy = _build_kyverno_dns_policy("t1", VIP)
    mutated = policy["spec"]["rules"][0]["mutate"]["patchStrategicMerge"]["spec"]
    ours = vms_mod.build_vpc_dns_spec("ns", VIP)

    assert mutated["dnsPolicy"] == ours["dnsPolicy"]
    assert mutated["dnsConfig"]["nameservers"] == ours["dnsConfig"]["nameservers"]
    assert mutated["dnsConfig"]["options"] == ours["dnsConfig"]["options"]


def test_the_vm_path_actually_applies_it() -> None:
    """A helper nobody calls passes its own tests.

    Asserted over the source of `create_vm_from_template`, because the way
    this comes back is the call being dropped, not the helper changing.
    """
    import inspect

    src = inspect.getsource(vms_mod.create_vm_from_template)
    assert "vpc_dns_needed" in src, "nothing decides whether the VM is on a VPC"
    assert "build_vpc_dns_spec" in src, "the VPC branch never sets the resolver"
    assert 'not vm_spec.get("dnsConfig")' in src, (
        "an explicit dnsConfig from the caller must win"
    )
    assert "await _vpc_dns_vip_or_none(" in src, (
        "the VIP lookup must be awaited — the cluster config is lazy"
    )
