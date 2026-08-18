"""A worker that cannot get the time cannot get a kubelet.

Measured in T8: Talos parks on

    waiting for time sync

and starts nothing. While `t8v` had no way out this read as a join failure —
nothing named the clock, and the symptom is indistinguishable from a network
fault, which is why it cost a debugging session.

The tempting fix, public NTP over egress, quietly deletes the property B3
exists for: an egress outage must not stop a node joining. Worse, it deletes
it *only for new workers*, so the cluster looks fine until the day something
has to be replaced.

The time therefore comes from the address the node already dials for its API,
konnectivity and trustd — reachable before any gateway exists — with the
public servers kept behind it.
"""

import pytest

from app.api.v1.tenants_ntp import (
    NTP_APP,
    NTP_PORT,
    build_chrony_deployment,
    build_tenant_ntp_service,
    worker_time_servers,
)


class TestTheOrderOfTheServerList:
    def test_the_tenant_vip_comes_first(self) -> None:
        """Order is the whole design: the first entry is the one that answers
        with no egress at all."""
        servers = worker_time_servers("10.199.0.101")

        assert servers[0] == "10.199.0.101"

    def test_public_servers_stay_behind_it(self) -> None:
        """They are better sources when the internet is there; they must never
        be the only ones."""
        servers = worker_time_servers("10.199.0.101")

        assert len(servers) > 1
        assert any("." in s and not s[0].isdigit() for s in servers[1:])

    def test_a_tenant_with_no_vip_gets_the_public_list(self) -> None:
        """The default overlay reaches the internet by construction; refusing
        to give it any time source would be a regression for the common case."""
        assert worker_time_servers(None) == worker_time_servers(None)
        assert worker_time_servers(None), "no time servers at all"
        assert all(not s[0].isdigit() for s in worker_time_servers(None))

    def test_the_fallback_list_is_configurable(self, monkeypatch) -> None:
        monkeypatch.setenv("TENANTS_NTP_FALLBACK", "ntp.corp.internal, 10.0.0.5")

        assert worker_time_servers("10.199.0.101") == [
            "10.199.0.101", "ntp.corp.internal", "10.0.0.5",
        ]

    def test_an_air_gapped_deployment_can_clear_the_fallbacks(
        self, monkeypatch,
    ) -> None:
        """Empty means empty — not "use the defaults". A cluster with no
        internet should not have its workers waiting on pool.ntp.org."""
        monkeypatch.setenv("TENANTS_NTP_FALLBACK", "")

        assert worker_time_servers("10.199.0.101") == ["10.199.0.101"]


class TestTheMachineConfigCarriesIt:
    def test_time_servers_land_in_the_worker_config(self) -> None:
        from app.api.v1.tenants_talos import build_talos_worker_config

        cfg = build_talos_worker_config(
            "t9", "tenant-t9", api_port=6443, control_plane_vip="10.199.0.9",
            machine_token="t", cluster_id="c", cluster_secret="s",
            pod_cidr="10.244.0.0/16", service_cidr="10.96.0.0/12",
            time_servers=["10.199.0.9", "pool.ntp.org"],
        )

        assert cfg["machine"]["time"]["servers"] == ["10.199.0.9", "pool.ntp.org"]

    def test_omitted_when_there_are_none(self) -> None:
        """An empty `time.servers` is not the same as absent: Talos would have
        no source at all rather than falling back to its own defaults."""
        from app.api.v1.tenants_talos import build_talos_worker_config

        cfg = build_talos_worker_config(
            "t9", "tenant-t9", api_port=6443, control_plane_vip="10.199.0.9",
            machine_token="t", cluster_id="c", cluster_secret="s",
            pod_cidr="10.244.0.0/16", service_cidr="10.96.0.0/12",
            time_servers=[],
        )

        assert "time" not in cfg["machine"]

    def test_the_create_path_passes_the_tenant_vip(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/tenants_capi.py").read_text()

        assert "time_servers=worker_time_servers(vip if own_vip else None)" in src


class TestPublishingItOnTheTenantVip:
    def test_it_is_udp_123(self) -> None:
        svc = build_tenant_ntp_service("t8", "tenant-t8", vip="10.199.0.101")
        [port] = svc["spec"]["ports"]

        assert (port["port"], port["protocol"]) == (NTP_PORT, "UDP")

    def test_it_shares_the_control_plane_address(self) -> None:
        """A second address per tenant would be an allocation nobody asked
        for; MetalLB allows the sharing because the ports cannot collide."""
        svc = build_tenant_ntp_service("t8", "tenant-t8", vip="10.199.0.101")
        ann = svc["metadata"]["annotations"]

        assert ann["metallb.universe.tf/loadBalancerIPs"] == "10.199.0.101"
        assert ann["metallb.universe.tf/allow-shared-ip"] == "t8-cp"

    def test_its_port_cannot_collide_with_the_control_plane_ones(self) -> None:
        """The sharing is only legal while that holds, and MetalLB refuses the
        pair outright if it stops holding."""
        from app.api.v1.tenants_cp_vip import tenant_cp_ports

        cp = {p for _, p in tenant_cp_ports("talos")}

        assert NTP_PORT not in cp

    def test_it_selects_chrony_and_not_the_control_plane(self) -> None:
        svc = build_tenant_ntp_service("t8", "tenant-t8", vip="10.199.0.101")

        assert svc["spec"]["selector"] == {"app": NTP_APP}

    def test_traffic_policy_is_cluster(self) -> None:
        """chrony does not run on every node; `Local` would black-hole the
        request from any node without a replica — during a join, which is
        exactly when nobody is watching."""
        svc = build_tenant_ntp_service("t8", "tenant-t8", vip="10.199.0.101")

        assert svc["spec"]["externalTrafficPolicy"] == "Cluster"


class TestTheServerItself:
    def test_it_serves_udp_123(self) -> None:
        dep = build_chrony_deployment()
        [port] = dep["spec"]["template"]["spec"]["containers"][0]["ports"]

        assert (port["containerPort"], port["protocol"]) == (NTP_PORT, "UDP")

    def test_more_than_one_replica_spread_across_nodes(self) -> None:
        """A tenant that cannot get the time cannot get a node, so a single
        replica would put every future join behind one drain."""
        spec = build_chrony_deployment()["spec"]

        assert spec["replicas"] >= 2
        assert spec["template"]["spec"]["topologySpreadConstraints"][0][
            "topologyKey"] == "kubernetes.io/hostname"

    def test_it_never_asks_to_step_the_clock(self) -> None:
        """The claim worth guarding. The rest of the set was measured, not
        chosen: with `drop: ALL` and NET_BIND_SERVICE alone the pod
        crash-looped on `chown: /var/lib/chrony: Operation not permitted`,
        because chronyd's entrypoint takes ownership of its own directories
        and then drops privileges itself. SYS_TIME is the one that would let
        it change the host's time, and it is absent."""
        c = build_chrony_deployment()["spec"]["template"]["spec"]["containers"][0]
        caps = c["securityContext"]["capabilities"]

        assert caps["drop"] == ["ALL"]
        assert "SYS_TIME" not in caps["add"]
        assert c["securityContext"]["allowPrivilegeEscalation"] is False

    def test_it_declares_itself_a_local_reference(self) -> None:
        """Measured the hard way: without `local stratum`, chronyd refuses to
        answer at all until it considers itself synchronised — so the pod runs,
        reports Ready, and every query times out. There is no upstream here by
        design; the node's clock is the source."""
        from app.api.v1.tenants_ntp import CHRONY_CONF

        assert "local stratum" in CHRONY_CONF
        assert "allow all" in CHRONY_CONF


class TestFailureIsNotFatalToTheTenant:
    @pytest.mark.asyncio
    async def test_an_existing_deployment_is_left_alone(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from kubernetes_asyncio.client import ApiException

        from app.api.v1.tenants_ntp import ensure_ntp_server

        k8s = MagicMock()
        k8s.core_api.create_namespaced_config_map = AsyncMock()
        k8s.apps_api.create_namespaced_deployment = AsyncMock(
            side_effect=ApiException(status=409))

        await ensure_ntp_server(k8s)      # no raise

    @pytest.mark.asyncio
    async def test_a_failure_is_logged_loudly_and_swallowed(self, caplog) -> None:
        """Aborting a tenant create over the clock would be worse than the
        symptom — but a silent failure reappears later as an unjoinable node."""
        from unittest.mock import AsyncMock, MagicMock

        from kubernetes_asyncio.client import ApiException

        from app.api.v1.tenants_ntp import ensure_ntp_server

        k8s = MagicMock()
        k8s.core_api.create_namespaced_config_map = AsyncMock()
        k8s.apps_api.create_namespaced_deployment = AsyncMock(
            side_effect=ApiException(status=403))

        with caplog.at_level("ERROR"):
            await ensure_ntp_server(k8s)

        assert any("time sync" in r.message for r in caplog.records)


class TestTheTransitGuardLetsTheTimeThrough:
    """The guard is a whitelist, and it whitelisted TCP only.

    An NTP service published on the tenant's VIP is not reachable until the
    transit ACL says so, and a request dropped there presents as — again — a
    node that never joins, with nothing in any log naming the clock. The port
    and the guard have to ship together or the feature is decorative.
    """

    def test_udp_123_is_allowed_alongside_the_tcp_ports(self) -> None:
        from app.core.tenant_transit import build_transit_acls

        acls = build_transit_acls("10.199.1.27", "10.199.0.101",
                                  [6443, 8132, 50001], [NTP_PORT])
        matches = [a["match"] for a in acls]

        assert any("udp.dst == 123" in m for m in matches)
        assert sum("tcp.dst" in m for m in matches) == 3

    def test_the_udp_rule_is_scoped_to_the_same_pair(self) -> None:
        """Scoped to this tenant's EIP and this tenant's VIP, like every other
        rule here — a blanket UDP allow would open the transit plane."""
        from app.core.tenant_transit import build_transit_acls

        [acl] = build_transit_acls("10.199.1.27", "10.199.0.101", [], [NTP_PORT])

        assert "ip4.src == 10.199.1.27" in acl["match"]
        assert "ip4.dst == 10.199.0.101" in acl["match"]
        assert acl["action"] == "allow-related"

    def test_no_udp_rule_when_none_is_asked_for(self) -> None:
        """cloud-init tenants have no business with this port."""
        from app.core.tenant_transit import build_transit_acls

        acls = build_transit_acls("10.199.1.20", "10.199.0.100", [20000, 20001])

        assert all("udp" not in a["match"] for a in acls)

    def test_talos_tenants_get_it_and_cloud_init_ones_do_not(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/tenants_capi.py").read_text()
        block = src[src.index("transit_ports = [api_port, konn_port]"):]
        block = block[:block.index("_wire_tenant_to_transit")]

        assert 'req.worker_os == "talos"' in block
        assert "transit_udp_ports.append(NTP_PORT)" in block
