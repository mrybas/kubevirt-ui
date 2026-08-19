"""A rollout that cannot start is indistinguishable from a healthy cluster.

Measured in UAT 2026-08-19: changing worker vCPU through the UI's own `Apply`
button created the replacement Machine, the namespace quota had no room for
its pod, and there it stayed. Every existing worker was untouched and Ready,
`readyReplicas` equalled `replicas`, and the tenant list went on printing
`Ready 2/2` while the change the operator asked for went nowhere. The only
trace was a `FailedCreate` event buried in `kubectl describe`.

`updatedReplicas` is what separates old from new, and the quota fix makes the
stall rare rather than impossible — a tenant sized before the fix, or one
whose folder ceiling is genuinely full, still lands here.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.api.v1.tenants_crud import _rollout_stall, apply_capacity_to_status
from app.models.tenant import TenantResponse


def _k8s(events: list[str] | None = None, explode: bool = False):
    k8s = MagicMock()

    async def _list_events(**kw):
        if explode:
            raise RuntimeError("events should not have been read")
        return SimpleNamespace(
            items=[SimpleNamespace(message=m) for m in (events or [])],
        )

    k8s.core_api.list_namespaced_event = _list_events
    return k8s


@pytest.mark.asyncio
async def test_a_stalled_rollout_names_the_quota() -> None:
    stall = await _rollout_stall(
        _k8s(['pods "virt-launcher-x" is forbidden: exceeded quota: t-quota']),
        "tenant-t1",
        {"replicas": 2},
        {"replicas": 3, "readyReplicas": 2, "updatedReplicas": 0},
    )

    assert stall is not None
    assert "2 of 2 still on the old template" in stall
    assert "quota" in stall


@pytest.mark.asyncio
async def test_a_finished_rollout_is_silent() -> None:
    # And costs nothing: the events are never read on the healthy path.
    assert await _rollout_stall(
        _k8s(explode=True), "tenant-t1",
        {"replicas": 2},
        {"replicas": 2, "readyReplicas": 2, "updatedReplicas": 2},
    ) is None


@pytest.mark.asyncio
async def test_an_unreconciled_deployment_is_not_called_stalled() -> None:
    # Absent is not zero. A MachineDeployment CAPI has not touched yet has no
    # `updatedReplicas`, and reading that as "none updated" would report every
    # fresh tenant as stuck.
    assert await _rollout_stall(
        _k8s(explode=True), "tenant-t1",
        {"replicas": 2},
        {"replicas": 2, "readyReplicas": 2},
    ) is None


@pytest.mark.asyncio
async def test_an_unreadable_event_list_still_reports_the_stall() -> None:
    # The reason is a nicety; the stall itself is the finding.
    k8s = MagicMock()

    async def _boom(**kw):
        from kubernetes_asyncio.client import ApiException
        raise ApiException(status=403)

    k8s.core_api.list_namespaced_event = _boom
    stall = await _rollout_stall(
        k8s, "tenant-t1", {"replicas": 2},
        {"replicas": 3, "updatedReplicas": 1},
    )
    assert stall is not None and "1 of 2" in stall


def test_the_tenant_stops_reading_ready() -> None:
    tenant = TenantResponse(
        name="t1", display_name="t1", namespace="tenant-t1", status="Ready",
        kubernetes_version="v1.32.1",
        worker_count=2, workers_ready=2,
        rollout_stalled="worker rollout stalled: 1 of 2 still on the old template",
    )

    result = apply_capacity_to_status(tenant)

    assert result.status == "Degraded", (
        "every worker is healthy, so nothing else downgrades this — without "
        "the stall the list keeps printing Ready while the rollout is dead"
    )
    assert "rollout stalled" in (result.status_detail or "")
