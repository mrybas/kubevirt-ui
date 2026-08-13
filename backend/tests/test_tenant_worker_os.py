"""Unit tests for the worker-OS choice on tenant creation.

Talos is a different bootstrap mechanism, not a different image: the nodes ask
a trustd signer for a certificate instead of running kubeadm. So picking it
has to swap the CAPI bootstrap provider and add a signer to the control plane
— while leaving the existing cloud-init path byte-for-byte as it was.
"""

import time

import pytest

from app.api.v1 import tenants_common
from app.api.v1.tenants_capi import (
    _build_kamaji_cp_cr,
    _build_machine_deployment_cr,
)
from app.api.v1.tenants_talos import TALOS_TRUSTD_PORT, signer_dns_names
from app.models.tenant import TenantCreateRequest


@pytest.fixture(autouse=True)
def cluster_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed the cluster-config cache the CR builders read from.

    Normally populated by `_ensure_cluster_config` against a live cluster;
    these tests only exercise the shapes, so a fixed set of values is enough
    and keeps them independent of discovery.
    """
    monkeypatch.setattr(tenants_common, "_cluster_config", {
        "ingress_ip": "10.0.0.1",
        "ingress_domain": "lab.example",
        "ingress_class": "traefik",
        "ingress_controller": "traefik",
        "mgmt_cidr": "10.0.0.0/24",
        "vpcdns_forward_dns": "10.96.0.10",
        "vpcdns_vip": "10.96.0.200",
        "host_service_cidr": "10.96.0.0/12",
        "fetched_at": time.time(),
    })


def _req(**kw: object) -> TenantCreateRequest:
    base: dict = {
        "name": "t1", "display_name": "T1", "folder": "f", "environment": "e",
    }
    base.update(kw)
    return TenantCreateRequest(**base)  # type: ignore[arg-type]


class TestDefaultIsUnchanged:
    def test_cloud_init_is_the_default(self) -> None:
        assert _req().worker_os == "cloud-init"

    def test_default_still_bootstraps_with_kubeadm(self) -> None:
        md = _build_machine_deployment_cr(_req())
        ref = md["spec"]["template"]["spec"]["bootstrap"]["configRef"]
        assert ref["kind"] == "KubeadmConfigTemplate"

    def test_default_control_plane_has_no_signer(self) -> None:
        spec = _build_kamaji_cp_cr(_req())["spec"]
        assert "additionalContainers" not in spec["deployment"]
        assert "additionalPorts" not in spec["network"]


class TestTalosSwapsTheBootstrapProvider:
    def test_bootstrap_ref_points_at_a_talos_config_template(self) -> None:
        md = _build_machine_deployment_cr(_req(worker_os="talos"))
        ref = md["spec"]["template"]["spec"]["bootstrap"]["configRef"]

        assert ref["kind"] == "TalosConfigTemplate"
        assert ref["name"] == "t1-workers"

    def test_infrastructure_ref_is_untouched(self) -> None:
        # Talos changes how the node is configured, not what runs it.
        md = _build_machine_deployment_cr(_req(worker_os="talos"))
        infra = md["spec"]["template"]["spec"]["infrastructureRef"]
        assert infra["kind"] == "KubevirtMachineTemplate"


class TestTalosControlPlane:
    def test_signer_sidecar_and_volume_are_added(self) -> None:
        # Under the KamajiControlPlane names. TenantControlPlane calls the same
        # things `additionalContainers`/`additionalVolumes`, and using those
        # here is not an error the API reports: unknown fields are pruned
        # silently, so the tenant comes up Ready with no signer at all and the
        # Talos worker waits forever for a certificate.
        spec = _build_kamaji_cp_cr(_req(worker_os="talos"))["spec"]

        containers = spec["deployment"]["extraContainers"]
        assert containers[0]["name"] == "talos-csr-signer"
        assert spec["deployment"]["extraVolumes"]
        assert "additionalContainers" not in spec["deployment"]

    def test_worker_dns_names_are_added_to_cert_sans(self) -> None:
        # Without these the join fails TLS before trustd is reached.
        spec = _build_kamaji_cp_cr(_req(worker_os="talos"))["spec"]
        for name in signer_dns_names("t1", "tenant-t1"):
            assert name in spec["network"]["certSANs"]

    def test_the_ingress_cert_san_survives(self) -> None:
        # The Talos names are added to the existing entry, not instead of it.
        plain = _build_kamaji_cp_cr(_req())["spec"]["network"]["certSANs"]
        talos = _build_kamaji_cp_cr(_req(worker_os="talos"))["spec"]["network"]["certSANs"]
        assert set(plain) <= set(talos)

    def test_own_vip_publishes_port_50001(self) -> None:
        spec = _build_kamaji_cp_cr(_req(worker_os="talos"))["spec"]
        assert spec["network"]["additionalPorts"][0]["port"] == TALOS_TRUSTD_PORT

    def test_shared_vip_does_not_publish_the_port(self) -> None:
        # MetalLB refuses identical ports on one shared address.
        spec = _build_kamaji_cp_cr(
            _req(worker_os="talos"), advertise_vip="10.198.190.10",
        )["spec"]
        assert "additionalPorts" not in spec["network"]


class TestRequestValidation:
    def test_only_the_two_known_values_are_accepted(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _req(worker_os="flatcar")

    def test_talos_is_accepted(self) -> None:
        assert _req(worker_os="talos").worker_os == "talos"


class TestTrustdRoute:
    """The signer is exposed by the same mechanism as the apiserver.

    It used to be a bespoke nginx SNI router with a shared route map: one
    cluster-wide object, edited per tenant, needing a manual restart to take
    effect and manual cleanup on delete — the very shape criticised elsewhere
    in this codebase. The apiserver's own path already does TLS passthrough
    matched on HostSNI with a configurable backend port, so trustd is one more
    per-tenant route through it.
    """

    def _traefik(self, **kw: object):
        from app.api.v1.tenants_capi import _build_ingressroutetcp_traefik

        return _build_ingressroutetcp_traefik(_req(**kw), "t1.tenant-t1.svc", 50001)

    def test_route_is_tls_passthrough_matched_on_the_sni_name(self) -> None:
        body = self._traefik()
        assert body["spec"]["tls"]["passthrough"] is True
        assert body["spec"]["routes"][0]["match"] == "HostSNI(`t1.tenant-t1.svc`)"

    def test_backend_port_is_the_fixed_trustd_port(self) -> None:
        from app.api.v1.tenants_talos import TALOS_TRUSTD_PORT

        body = self._traefik()
        assert body["spec"]["routes"][0]["services"][0]["port"] == TALOS_TRUSTD_PORT

    def test_route_lives_in_the_tenant_namespace(self) -> None:
        # So the namespace cascade removes it — no cleanup helper needed.
        assert self._traefik()["metadata"]["namespace"] == "tenant-t1"

    def test_no_shared_object_is_involved(self) -> None:
        # Guards the regression: nothing here may reach outside the tenant.
        import app.api.v1.tenants_talos as talos

        assert not hasattr(talos, "build_sni_router")
        assert not hasattr(talos, "update_sni_router_map")
