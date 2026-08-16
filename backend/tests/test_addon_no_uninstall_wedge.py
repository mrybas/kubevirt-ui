"""An addon install must not wedge itself on a cluster with no nodes.

A fresh tenant has no Ready node until its CNI is running, and the CNI *is* one
of these releases. With Helm waiting for workloads, the install times out, Flux
remediates by uninstalling, and the uninstall's own hook pod has nowhere to run
either — so the release sits in `uninstalling` forever and does not recover
when nodes finally appear.

Measured on the lab: `tigera-operator-uninstall-*` Pending, zero nodes
registered, and the tenant page reporting "Could not determine release state
for release with status 'uninstalling'" (backlog U27).
"""

from app.api.v1.tenants_addons import _build_flux_helmrelease_cr
from app.models.tenant import AddonCatalog, AddonComponent


def _cr() -> dict:
    return _build_flux_helmrelease_cr(
        tenant_name="ta",
        addon_id="calico",
        component=AddonComponent(id="calico", name="Calico CNI", chartPath="networking/calico",
                                 namespace="tigera-operator"),
        catalog=AddonCatalog(
            base_path="charts",
            git_repository_ref={"name": "addons", "namespace": "flux-system"},
        ),
        helm_values={},
    )


def test_install_does_not_wait_for_workloads_that_need_a_cni() -> None:
    assert _cr()["spec"]["install"]["disableWait"] is True


def test_retries_are_still_configured() -> None:
    """Retrying is fine; it is the uninstall on give-up that traps the release."""
    assert _cr()["spec"]["install"]["remediation"]["retries"] == 5
