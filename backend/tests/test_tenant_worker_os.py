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
        spec = _build_kamaji_cp_cr(_req(worker_os="talos"))["spec"]

        containers = spec["deployment"]["additionalContainers"]
        assert containers[0]["name"] == "talos-csr-signer"
        assert spec["deployment"]["additionalVolumes"]

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
