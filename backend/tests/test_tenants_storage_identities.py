"""Unit tests for the two host-side tenant-storage identities.

`kubevirt-csi` and `capk-infra` must stay distinct: the CSI kubeconfig is
replicated into the tenant cluster (where any tenant admin can read it), so
it may only carry volume verbs, while CAPK — which `infraClusterSecretRef`
switches onto whichever identity that secret holds — needs secrets and
virtualmachines in the host namespace.
"""

import yaml

from app.api.v1.tenants_capi import _build_kubevirt_cluster_cr
from app.api.v1.tenants_storage import (
    CAPK_KUBECONFIG_SECRET_NAME,
    CAPK_SA_NAME,
    CSI_KUBECONFIG_SECRET_NAME,
    CSI_SA_NAME,
    _build_infra_kubeconfig,
    _capk_role_rules,
    _csi_role_rules,
)
from app.models.tenant import TenantCreateRequest


def _verbs_for(rules: list[dict], api_group: str, resource: str) -> set[str]:
    """Collect the verbs a rule set grants on one api_group/resource pair."""
    verbs: set[str] = set()
    for rule in rules:
        if api_group in rule["apiGroups"] and resource in rule["resources"]:
            verbs.update(rule["verbs"])
    return verbs


class TestRoleRuleSplit:
    """The CSI role must stay narrow; the CAPK role must be wide enough."""

    def test_capk_can_create_virtualmachines(self) -> None:
        verbs = _verbs_for(_capk_role_rules(), "kubevirt.io", "virtualmachines")
        assert "create" in verbs
        assert "delete" in verbs

    def test_capk_can_read_and_write_secrets(self) -> None:
        # Reads the CABPK bootstrap Secret, writes the cloud-init Secret.
        verbs = _verbs_for(_capk_role_rules(), "", "secrets")
        assert {"get", "create"} <= verbs

    def test_capk_can_read_virt_launcher_pods(self) -> None:
        assert "get" in _verbs_for(_capk_role_rules(), "", "pods")

    def test_csi_cannot_create_virtualmachines(self) -> None:
        # This is the whole point of the split — the CSI kubeconfig lands in
        # the tenant cluster, so it must not be able to make host VMs.
        verbs = _verbs_for(_csi_role_rules(), "kubevirt.io", "virtualmachines")
        assert "create" not in verbs

    def test_csi_cannot_read_secrets(self) -> None:
        assert _verbs_for(_csi_role_rules(), "", "secrets") == set()


class TestInfraKubeconfig:
    """One builder, two identities — the SA name must follow through."""

    def test_uses_the_requested_sa_name(self) -> None:
        raw = _build_infra_kubeconfig(
            api_server_url="https://10.0.0.1:6443",
            ca_data_b64="Y2E=",
            sa_token="tok",
            tenant_ns="tenant-demo",
            sa_name=CAPK_SA_NAME,
        )
        kc = yaml.safe_load(raw)
        assert kc["users"][0]["name"] == CAPK_SA_NAME
        assert kc["contexts"][0]["context"]["user"] == CAPK_SA_NAME
        assert kc["users"][0]["user"]["token"] == "tok"

    def test_defaults_to_the_csi_sa(self) -> None:
        kc = yaml.safe_load(_build_infra_kubeconfig(
            api_server_url="https://10.0.0.1:6443",
            ca_data_b64="Y2E=",
            sa_token="tok",
            tenant_ns="tenant-demo",
        ))
        assert kc["users"][0]["name"] == CSI_SA_NAME

    def test_falls_back_to_insecure_without_ca(self) -> None:
        kc = yaml.safe_load(_build_infra_kubeconfig(
            api_server_url="https://10.0.0.1:6443",
            ca_data_b64="",
            sa_token="tok",
            tenant_ns="tenant-demo",
        ))
        assert kc["clusters"][0]["cluster"]["insecure-skip-tls-verify"] is True


class TestKubevirtClusterSecretRef:
    """`infraClusterSecretRef` must point at the CAPK identity, not the CSI one."""

    def _req(self) -> TenantCreateRequest:
        return TenantCreateRequest(
            name="demo", display_name="Demo", folder="f1", environment="dev",
        )

    def test_references_the_capk_secret(self) -> None:
        cr = _build_kubevirt_cluster_cr(self._req(), {
            "secret_name": CSI_KUBECONFIG_SECRET_NAME,
            "secret_namespace": "tenant-demo",
            "capk_secret_name": CAPK_KUBECONFIG_SECRET_NAME,
        })
        ref = cr["spec"]["infraClusterSecretRef"]
        assert ref["name"] == CAPK_KUBECONFIG_SECRET_NAME
        assert ref["namespace"] == "tenant-demo"

    def test_falls_back_to_csi_secret_when_capk_absent(self) -> None:
        # storage_info from an older create path (no capk_secret_name key).
        cr = _build_kubevirt_cluster_cr(self._req(), {
            "secret_name": CSI_KUBECONFIG_SECRET_NAME,
            "secret_namespace": "tenant-demo",
        })
        assert cr["spec"]["infraClusterSecretRef"]["name"] == CSI_KUBECONFIG_SECRET_NAME

    def test_no_secret_ref_without_storage(self) -> None:
        cr = _build_kubevirt_cluster_cr(self._req(), None)
        assert cr["spec"] == {}
