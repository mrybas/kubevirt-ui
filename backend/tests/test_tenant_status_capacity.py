"""A tenant with no workers is not Ready, whatever CAPI says.

CAPI's `Ready` describes the control plane. On the lab a tenant sat at
`WORKERS 0/1` with an empty Kubernetes version and a CNI release wedged in
`uninstalling`, and the tenant list drew it green — the detail page was honest,
the list was not (backlog U27). Green there means "you can schedule work on
this", and you could not: the cluster had no capacity at all.
"""

import pytest

from app.api.v1.tenants_crud import apply_capacity_to_status
from app.models.tenant import TenantAddonStatus, TenantResponse


def _tenant(**kw) -> TenantResponse:
    base = dict(
        kubernetes_version="v1.32.1",
        name="ta", display_name="Team A Cluster", namespace="tenant-ta",
        status="Ready", phase="Provisioned", endpoint="https://ta.example",
        control_plane_ready=True, worker_count=1, workers_ready=1,
    )
    base.update(kw)
    return TenantResponse(**base)


def test_a_tenant_with_workers_stays_ready() -> None:
    assert apply_capacity_to_status(_tenant()).status == "Ready"


def test_zero_of_one_worker_is_degraded() -> None:
    out = apply_capacity_to_status(_tenant(workers_ready=0, worker_count=1))
    assert out.status == "Degraded"
    assert "0/1" in (out.status_detail or "")


def test_some_workers_missing_is_degraded_too() -> None:
    out = apply_capacity_to_status(_tenant(workers_ready=1, worker_count=3))
    assert out.status == "Degraded"


def test_a_tenant_that_asked_for_no_workers_is_not_degraded() -> None:
    """A control-plane-only tenant is a legitimate thing to have."""
    assert apply_capacity_to_status(_tenant(worker_count=0, workers_ready=0)).status == "Ready"


def test_a_wedged_addon_is_degraded() -> None:
    out = apply_capacity_to_status(_tenant(addons=[
        TenantAddonStatus(addon_id="calico", name="Calico CNI", ready=False,
                          message="Could not determine release state for release "
                                  "with status 'uninstalling'"),
    ]))
    assert out.status == "Degraded"
    assert "calico" in (out.status_detail or "")


def test_a_failing_tenant_is_not_relabelled_as_degraded() -> None:
    """Failed is worse than Degraded; do not soften it."""
    assert apply_capacity_to_status(
        _tenant(status="Failed", workers_ready=0)
    ).status == "Failed"


def test_a_tenant_still_provisioning_keeps_that_status() -> None:
    out = apply_capacity_to_status(_tenant(status="Provisioning", workers_ready=0))
    assert out.status == "Provisioning", "workers are expected to be missing here"
