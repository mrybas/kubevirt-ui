"""OVN NAT that exists in the cluster must appear on the OVN Gateways page.

The page was driven purely by the tracking ConfigMaps the UI writes for its own
gateways, so anything created by CLI or GitOps was invisible. On the lab it read
"No OVN gateways" while the cluster held five `OvnEip` and two `OvnSnatRule` —
including `cpt-snat-team-a`, the rule that actually NATs a tenant onto the
transit plane (backlog U24).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.ovn_gateway import list_ovn_gateways
from app.core.auth import User

USER = User(id="admin", email="a@l", username="admin", groups=["kubevirt-ui-admins"])


def _k8s(eips: list[dict], snats: list[dict], tracked: list[str]) -> MagicMock:
    k8s = MagicMock()

    cms = MagicMock()
    cms.items = []
    for vpc in tracked:
        cm = MagicMock()
        cm.metadata.labels = {"kubevirt-ui.io/ovn-gateway": vpc}
        cm.data = {"vpc_name": vpc, "subnet_name": f"{vpc}-default"}
        cm.metadata.resource_version = "1"
        cms.items.append(cm)
    k8s.core_api.list_namespaced_config_map = AsyncMock(return_value=cms)

    async def read_cm(name: str, namespace: str):
        for cm in cms.items:
            if cm.metadata.labels.get("kubevirt-ui.io/ovn-gateway") in name:
                return cm
        from kubernetes_asyncio.client.exceptions import ApiException
        raise ApiException(status=404, reason="NotFound")

    k8s.core_api.read_namespaced_config_map = AsyncMock(side_effect=read_cm)

    async def list_crd(**kw):
        plural = kw["plural"]
        sel = kw.get("label_selector") or ""
        pool = {"ovn-eips": eips, "ovn-snat-rules": snats}.get(plural, [])
        if not sel:
            return {"items": pool}
        key, _, val = sel.partition("=")
        return {
            "items": [
                i for i in pool
                if (i.get("metadata", {}).get("labels") or {}).get(key) == val
            ]
        }

    k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_crd)
    return k8s


def _request(k8s: MagicMock) -> MagicMock:
    r = MagicMock()
    r.app.state.k8s_client = k8s
    return r


@pytest.mark.asyncio
async def test_a_cli_made_snat_rule_shows_up() -> None:
    eips = [{
        "metadata": {"name": "cpt-eip-team-a", "labels": {}},
        "spec": {"externalSubnet": "cp-transit", "type": "nat"},
        "status": {"v4Ip": "10.199.1.1", "ready": True},
    }]
    snats = [{
        "metadata": {"name": "cpt-snat-team-a", "labels": {}},
        "spec": {"ovnEip": "cpt-eip-team-a", "vpc": "team-a", "vpcSubnet": "team-a-default"},
        "status": {"ready": True, "v4Ip": "10.199.1.1", "vpc": "team-a"},
    }]

    out = await list_ovn_gateways(request=_request(_k8s(eips, snats, tracked=[])), user=USER)

    names = {g.vpc_name for g in out.items}
    assert "team-a" in names, "a SNAT rule created outside the UI is invisible"
    gw = next(g for g in out.items if g.vpc_name == "team-a")
    assert [r.name for r in gw.snat_rules] == ["cpt-snat-team-a"]
    assert gw.eip and gw.eip.v4ip == "10.199.1.1"
    assert gw.origin == "external"


@pytest.mark.asyncio
async def test_a_tracked_gateway_is_not_listed_twice() -> None:
    eips = [{
        "metadata": {"name": "eip-t1", "labels": {"kubevirt-ui.io/ovn-gateway": "t1"}},
        "spec": {"externalSubnet": "external", "type": "nat"},
        "status": {"v4Ip": "10.199.4.5", "ready": True},
    }]
    snats = [{
        "metadata": {"name": "snat-t1", "labels": {"kubevirt-ui.io/ovn-gateway": "t1"}},
        "spec": {"ovnEip": "eip-t1", "vpc": "t1", "vpcSubnet": "t1-default"},
        "status": {"ready": True, "vpc": "t1"},
    }]

    out = await list_ovn_gateways(request=_request(_k8s(eips, snats, tracked=["t1"])), user=USER)

    assert [g.vpc_name for g in out.items] == ["t1"]
    assert out.items[0].origin == "ui"
