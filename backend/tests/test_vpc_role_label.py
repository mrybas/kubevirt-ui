"""Infrastructure is a role, and a role is declared — never read off the shape.

The peer census filtered subnets on `vpc != system && no vlan`, which describes
a *shape*. The shared egress gateway's own VPC has that shape, so it was
counted as a tenant and every tenant was handed

    3000 drop  ip4.src == 10.199.128.0/24
    3000 drop  ip4.dst == 10.199.128.0/24

— the address several of them egress *through*. It survived only because the
hub tenants also carry a legacy `allow` at 3100 that shadows the drop: right
behaviour resting on rule ordering rather than on the classification being
right, with the same CIDR emitted by the allow-writer and the drop-writer at
once.

The same lesson as the isolation guard, and the same fix: the role is written
down. `kubevirt-ui.io/egress-gateway` was already such a declaration, so it is
honoured as one rather than replaced.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.vpcs import _vpc_role


def _subnet(name, vpc, cidr, *, labels=None, acls=None):
    return {
        "metadata": {"name": name, "labels": labels or {}},
        "spec": {"vpc": vpc, "cidrBlock": cidr, "acls": acls or []},
    }


class TestWhatCountsAsARole:
    def test_the_general_label(self) -> None:
        assert _vpc_role(_subnet("s", "v", "10.0.0.0/24",
                                 labels={"kubevirt-ui.io/role": "infrastructure"})
                         ) == "infrastructure"

    def test_the_gateway_label_is_a_declaration_too(self) -> None:
        """It predates the general one and already says exactly this; treating
        it as a role avoids a migration whose only purpose is renaming."""
        assert _vpc_role(_subnet("s", "v", "10.0.0.0/24",
                                 labels={"kubevirt-ui.io/egress-gateway": "shared"})
                         ) == "egress-gateway"

    def test_shape_is_not_a_role(self) -> None:
        """`managed` says who made it, not what it is for."""
        assert _vpc_role(_subnet("s", "v", "10.0.0.0/24",
                                 labels={"kubevirt-ui.io/managed": "true"})) is None


class TestTheCensusStopsCallingInfrastructureAPeer:
    @pytest.mark.asyncio
    async def test_the_gateway_vpc_is_not_offered_as_a_peer_to_drop(self) -> None:
        from app.api.v1.vpcs import _tenant_vpc_cidrs

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value={"items": [
            _subnet("egw-shared-egress-subnet", "egw-shared-egress", "10.199.128.0/24",
                    labels={"kubevirt-ui.io/egress-gateway": "shared-egress"}),
            _subnet("t1-vpc-default", "t1-vpc", "10.200.8.0/22"),
        ]})

        assert await _tenant_vpc_cidrs(k8s, exclude="b3v") == ["10.200.8.0/22"]

    @pytest.mark.asyncio
    async def test_a_new_vpc_is_not_born_dropping_the_gateway(self, monkeypatch) -> None:
        """This is the whole point: without it the drop is written and only
        an allow that happens to sit higher keeps egress alive."""
        from app.api.v1 import vpcs

        monkeypatch.setenv("B3_BGP_PEER", "10.198.175.254")
        monkeypatch.setenv("B3_VPC_GATEWAY", "10.199.4.254")
        monkeypatch.setattr(vpcs, "_mgmt_deny_sources", AsyncMock(return_value=[]))

        patched: list[dict] = []

        async def list_obj(**kw):
            return {"items": [
                _subnet("egw-shared-egress-subnet", "egw-shared-egress",
                        "10.199.128.0/24",
                        labels={"kubevirt-ui.io/egress-gateway": "shared-egress"}),
                _subnet("new-default", "new", "10.200.60.0/22",
                        acls=[{"action": "drop", "direction": "to-lport",
                               "match": "ip4.src == 10.200.8.0/22", "priority": 3000}]),
            ]}

        async def patch_obj(**kw):
            patched.append(kw)
            return {}

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(side_effect=list_obj)
        k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=patch_obj)

        await vpcs.reconcile_isolation_acls(k8s)

        new = [c for c in patched if c["name"] == "new-default"]
        assert new, "the tenant subnet was not reconciled at all"
        assert not [a for a in new[0]["body"]["spec"]["acls"]
                    if a["action"] == "drop" and "10.199.128.0/24" in a["match"]]


class TestInfrastructureIsRepaired_NotMerelySkipped:
    @pytest.mark.asyncio
    async def test_blocking_rules_already_written_are_taken_back_off(
        self, monkeypatch,
    ) -> None:
        """Skipping alone would leave the damage in place: this pass had
        already written 18 tenant drops and the management baseline onto the
        shared gateway's own subnet."""
        from app.api.v1 import vpcs

        monkeypatch.setenv("B3_BGP_PEER", "10.198.175.254")
        monkeypatch.setenv("B3_VPC_GATEWAY", "10.199.4.254")
        monkeypatch.setattr(vpcs, "_mgmt_deny_sources",
                            AsyncMock(return_value=["10.198.160.3/32"]))

        gw = _subnet(
            "egw-shared-egress-subnet", "egw-shared-egress", "10.199.128.0/24",
            labels={"kubevirt-ui.io/egress-gateway": "shared-egress"},
            acls=[
                {"action": "allow-related", "direction": "from-lport",
                 "match": "ip4.dst == 10.199.128.0/24", "priority": 3200},
                {"action": "drop", "direction": "to-lport",
                 "match": "ip4.src == 10.200.8.0/22", "priority": 3000},
                {"action": "drop", "direction": "to-lport",
                 "match": "ip4.src == 10.198.160.3/32", "priority": 3300},
            ])

        patched: list[dict] = []

        async def patch_obj(**kw):
            patched.append(kw)
            return {}

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(
            return_value={"items": [gw]})
        k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=patch_obj)

        await vpcs.reconcile_isolation_acls(k8s)

        assert patched, "the damage was left in place"
        written = patched[0]["body"]["spec"]["acls"]
        assert all(a["priority"] not in (3000, 3300) for a in written)
        assert any(a["priority"] == 3200 for a in written), (
            "it stripped rules it did not write"
        )

    @pytest.mark.asyncio
    async def test_a_clean_infrastructure_subnet_is_not_patched_every_pass(
        self, monkeypatch,
    ) -> None:
        from app.api.v1 import vpcs

        monkeypatch.setattr(vpcs, "_mgmt_deny_sources", AsyncMock(return_value=[]))

        k8s = MagicMock()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value={"items": [
            _subnet("egw-shared-egress-subnet", "egw-shared-egress", "10.199.128.0/24",
                    labels={"kubevirt-ui.io/egress-gateway": "shared-egress"}),
        ]})
        k8s.custom_api.patch_cluster_custom_object = AsyncMock()

        await vpcs.reconcile_isolation_acls(k8s)

        k8s.custom_api.patch_cluster_custom_object.assert_not_awaited()
