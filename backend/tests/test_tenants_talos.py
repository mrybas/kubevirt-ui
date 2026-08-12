"""Unit tests for Talos-flavoured tenants.

Talos nodes do not take a cloud-init blob and join — they ask a trustd signer
for a certificate on port 50001. Most of what these tests pin down is the
chain that makes that request reach a signer which answers it, and the
several places where a wrong value fails silently or only on the second node.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client import ApiException

from app.api.v1 import tenants_talos
from app.api.v1.tenants_talos import (
    TALOS_TRUSTD_PORT,
    apply_sni_route,
    build_bootstrap_token_secret,
    build_talos_pki,
    build_talos_secrets,
    build_talos_worker_config,
    generate_bootstrap_token,
    parse_sni_routes,
    remove_sni_route,
    render_sni_routes,
    signer_dns_names,
    sni_route_entry,
    talos_control_plane_additions,
    update_sni_router_map,
    validate_worker_binding,
    worker_endpoint,
)

TENANT = "t1"
NS = "tenant-t1"
VIP = "10.198.190.10"


class TestNaming:
    def test_signer_carries_both_dns_forms(self) -> None:
        # Talos dials the short name; in-cluster clients resolve the long one.
        assert signer_dns_names(TENANT, NS) == [
            f"{TENANT}.{NS}.svc", f"{TENANT}.{NS}.svc.cluster.local",
        ]

    def test_worker_endpoint_is_a_name_not_an_address(self) -> None:
        # The name is what produces SNI, which is what lets tenants share
        # port 50001 on one VIP. An IP endpoint sends no SNI at all.
        import ipaddress

        endpoint = worker_endpoint(TENANT, NS, 6443)
        assert endpoint == f"https://{TENANT}.{NS}.svc:6443"

        host = endpoint.split("//")[1].rsplit(":", 1)[0]
        with pytest.raises(ValueError):
            ipaddress.ip_address(host)


class TestSecrets:
    def test_token_is_kubeadm_format(self) -> None:
        token_id, token_secret = generate_bootstrap_token()
        assert len(token_id) == 6
        assert len(token_secret) == 16
        assert (token_id + token_secret).isalnum()

    def test_tokens_are_not_reused(self) -> None:
        assert generate_bootstrap_token() != generate_bootstrap_token()

    def test_secret_holds_the_three_stable_values(self) -> None:
        data = build_talos_secrets(TENANT, NS)["stringData"]
        assert set(data) == {"machine.token", "cluster.id", "cluster.secret"}

    def test_machine_token_is_the_trustd_token_format(self) -> None:
        token = build_talos_secrets(TENANT, NS)["stringData"]["machine.token"]
        left, right = token.split(".")
        assert (len(left), len(right)) == (6, 16)

    def test_cluster_values_differ_from_each_other(self) -> None:
        data = build_talos_secrets(TENANT, NS)["stringData"]
        assert data["cluster.id"] != data["cluster.secret"]

    def test_bootstrap_token_secret_is_typed_and_grouped(self) -> None:
        secret = build_bootstrap_token_secret("abcdef", "0123456789abcdef")

        assert secret["type"] == "bootstrap.kubernetes.io/token"
        assert secret["metadata"]["name"] == "bootstrap-token-abcdef"
        # Kamaji creates the RBAC around this group already.
        assert secret["stringData"]["auth-extra-groups"] == (
            "system:bootstrappers:kubeadm:default-node-token"
        )

    def test_bootstrap_token_lands_in_kube_system(self) -> None:
        secret = build_bootstrap_token_secret("abcdef", "0123456789abcdef")
        assert secret["metadata"]["namespace"] == "kube-system"


class TestPki:
    def _by_kind(self, objs: list[dict], kind: str) -> list[dict]:
        return [o for o in objs if o["kind"] == kind]

    def test_chain_is_selfsigned_then_ca_then_signer(self) -> None:
        objs = build_talos_pki(TENANT, NS)
        assert [o["kind"] for o in objs] == [
            "Issuer", "Certificate", "Issuer", "Certificate",
        ]

    def test_signer_certificate_has_no_ip_sans(self) -> None:
        # An IP SAN can only be added once the address is known, which means
        # patching the cert afterwards — and then the control plane must be
        # restarted, because the signer reads its cert once at startup.
        signer = self._by_kind(build_talos_pki(TENANT, NS), "Certificate")[1]
        assert "ipAddresses" not in signer["spec"]
        assert signer["spec"]["dnsNames"] == signer_dns_names(TENANT, NS)

    def test_ca_is_ed25519_and_long_lived(self) -> None:
        ca = self._by_kind(build_talos_pki(TENANT, NS), "Certificate")[0]
        assert ca["spec"]["isCA"] is True
        assert ca["spec"]["privateKey"]["algorithm"] == "Ed25519"
        assert ca["spec"]["duration"] == "87600h"

    def test_signer_is_issued_by_the_ca_not_the_selfsigned_issuer(self) -> None:
        objs = build_talos_pki(TENANT, NS)
        signer = self._by_kind(objs, "Certificate")[1]
        assert signer["spec"]["issuerRef"]["name"] == f"{TENANT}-talos-ca-issuer"

    def test_everything_lands_in_the_tenant_namespace(self) -> None:
        assert all(o["metadata"]["namespace"] == NS for o in build_talos_pki(TENANT, NS))


class TestControlPlaneAdditions:
    def test_sidecar_and_volume_are_paired(self) -> None:
        adds = talos_control_plane_additions(
            TENANT, NS, "signer:v1", shared_vip=False,
        )
        volume_name = adds["additionalVolumes"][0]["name"]
        mount = adds["additionalContainers"][0]["volumeMounts"][0]["name"]
        assert volume_name == mount

    def test_cert_sans_match_the_worker_endpoint_name(self) -> None:
        # If these drift, the join fails TLS before trustd is reached.
        adds = talos_control_plane_additions(
            TENANT, NS, "signer:v1", shared_vip=False,
        )
        assert adds["certSANs"] == signer_dns_names(TENANT, NS)

    def test_own_vip_gets_port_50001_on_the_service(self) -> None:
        adds = talos_control_plane_additions(
            TENANT, NS, "signer:v1", shared_vip=False,
        )
        assert adds["additionalPorts"][0]["port"] == TALOS_TRUSTD_PORT

    def test_shared_vip_must_not_add_the_port(self) -> None:
        # MetalLB refuses identical ports on one shared address; the router
        # fronts a per-tenant ClusterIP service instead.
        adds = talos_control_plane_additions(
            TENANT, NS, "signer:v1", shared_vip=True,
        )
        assert "additionalPorts" not in adds


class TestWorkerConfig:
    def _config(self, **kw: object) -> dict:
        base: dict = {
            "api_port": 6443,
            "control_plane_vip": VIP,
            "machine_token": "abcdef.0123456789abcdef",
            "cluster_id": "aWQ=",
            "cluster_secret": "c2VjcmV0",
            "pod_cidr": "10.244.0.0/16",
            "service_cidr": "10.112.0.0/12",
        }
        base.update(kw)
        return build_talos_worker_config(TENANT, NS, **base)  # type: ignore[arg-type]

    def test_endpoint_is_the_name(self) -> None:
        endpoint = self._config()["cluster"]["controlPlane"]["endpoint"]
        assert endpoint == worker_endpoint(TENANT, NS, 6443)

    def test_host_entry_pins_the_name_to_the_vip(self) -> None:
        # Joining needs no DNS — which matters, because the node has none
        # until it has joined.
        entry = self._config()["machine"]["network"]["extraHostEntries"][0]
        assert entry["ip"] == VIP
        assert entry["aliases"] == signer_dns_names(TENANT, NS)

    def test_kubeprism_is_disabled(self) -> None:
        # It proxies the apiserver via localhost, bypassing the name and
        # taking the SNI with it.
        assert self._config()["machine"]["features"]["kubePrism"]["enabled"] is False

    def test_kubelet_rotates_its_certificate(self) -> None:
        args = self._config()["machine"]["kubelet"]["extraArgs"]
        assert args["rotate-certificates"] == "true"

    def test_both_discovery_registries_are_off(self) -> None:
        registries = self._config()["cluster"]["discovery"]["registries"]
        assert registries["kubernetes"]["disabled"] is True
        assert registries["service"]["disabled"] is True

    def test_cidrs_are_carried_through(self) -> None:
        network = self._config()["cluster"]["network"]
        assert network["podSubnets"] == ["10.244.0.0/16"]
        assert network["serviceSubnets"] == ["10.112.0.0/12"]

    def test_ca_is_optional(self) -> None:
        assert "ca" not in self._config()["machine"]
        assert self._config(ca_cert_b64="Y2E=")["machine"]["ca"]["crt"] == "Y2E="


class TestSniMap:
    def test_entry_points_the_short_name_at_the_long_one(self) -> None:
        name, backend = sni_route_entry(TENANT, NS)
        assert name == f"{TENANT}.{NS}.svc"
        assert backend == f"{TENANT}.{NS}.svc.cluster.local:{TALOS_TRUSTD_PORT}"

    def test_parse_ignores_comments_and_blanks(self) -> None:
        raw = "# a comment\n\nt1.tenant-t1.svc t1.tenant-t1.svc.cluster.local:50001\n"
        assert parse_sni_routes(raw) == {
            "t1.tenant-t1.svc": "t1.tenant-t1.svc.cluster.local:50001",
        }

    def test_render_is_sorted_so_rewrites_do_not_churn(self) -> None:
        routes = {"b.svc": "b:50001", "a.svc": "a:50001"}
        assert render_sni_routes(routes).splitlines() == [
            "a.svc a:50001", "b.svc b:50001",
        ]

    def test_round_trip(self) -> None:
        routes = apply_sni_route({}, TENANT, NS)
        assert parse_sni_routes(render_sni_routes(routes)) == routes

    def test_add_is_idempotent(self) -> None:
        once = apply_sni_route({}, TENANT, NS)
        assert apply_sni_route(once, TENANT, NS) == once

    def test_remove_leaves_other_tenants_alone(self) -> None:
        routes = apply_sni_route(apply_sni_route({}, TENANT, NS), "t2", "tenant-t2")
        remaining = remove_sni_route(routes, TENANT, NS)
        assert list(remaining) == ["t2.tenant-t2.svc"]

    def test_remove_of_absent_tenant_is_a_no_op(self) -> None:
        assert remove_sni_route({}, TENANT, NS) == {}


@pytest.mark.asyncio
class TestSniRouterUpdate:
    def _k8s(self, raw: str = "") -> MagicMock:
        k8s = MagicMock()
        cm = MagicMock()
        cm.data = {tenants_talos.SNI_ROUTER_MAP_KEY: raw}
        k8s.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)
        k8s.core_api.patch_namespaced_config_map = AsyncMock()
        return k8s

    async def test_adds_the_route(self) -> None:
        k8s = self._k8s()

        assert await update_sni_router_map(k8s, TENANT, NS, add=True) is True

        body = k8s.core_api.patch_namespaced_config_map.await_args.kwargs["body"]
        assert f"{TENANT}.{NS}.svc" in body["data"][tenants_talos.SNI_ROUTER_MAP_KEY]

    async def test_missing_router_is_not_an_error(self) -> None:
        # Expected in per-VIP mode, where no router is deployed at all.
        k8s = self._k8s()
        k8s.core_api.read_namespaced_config_map = AsyncMock(
            side_effect=ApiException(status=404, reason="Not Found"),
        )

        assert await update_sni_router_map(k8s, TENANT, NS, add=True) is False

    async def test_unchanged_map_is_not_rewritten(self) -> None:
        # It is a cluster-wide object; churning it has a blast radius.
        name, backend = sni_route_entry(TENANT, NS)
        k8s = self._k8s(f"{name} {backend}\n")

        assert await update_sni_router_map(k8s, TENANT, NS, add=True) is True
        k8s.core_api.patch_namespaced_config_map.assert_not_awaited()

    async def test_removal_drops_only_this_tenant(self) -> None:
        k8s = self._k8s(
            "t1.tenant-t1.svc t1.tenant-t1.svc.cluster.local:50001\n"
            "t2.tenant-t2.svc t2.tenant-t2.svc.cluster.local:50001\n"
        )

        await update_sni_router_map(k8s, TENANT, NS, add=False)

        body = k8s.core_api.patch_namespaced_config_map.await_args.kwargs["body"]
        rendered = body["data"][tenants_talos.SNI_ROUTER_MAP_KEY]
        assert "t2.tenant-t2.svc" in rendered
        assert "t1.tenant-t1.svc" not in rendered


class TestWorkerBinding:
    def test_bridge_is_accepted(self) -> None:
        validate_worker_binding("bridge")

    def test_masquerade_is_refused_with_the_reason(self) -> None:
        # Every guest would see itself as 10.0.2.2 and register under it, so
        # the first node joins and the second cannot.
        with pytest.raises(ValueError, match="10.0.2.2"):
            validate_worker_binding("masquerade")
