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


def _k8s(events: list[tuple[str, str]] | None = None, explode: bool = False):
    """`events` is (reason, message).

    The fake honours `field_selector=reason=...` because the real API does,
    and the version of this code that ignored it shipped: a namespace with
    664 events returned an unsorted page of 50 holding none of the ones being
    looked for, so a live stall went unreported while the scheduler refused
    the pod every few seconds. A fake that hands back everything regardless
    of the filter cannot see that.
    """
    k8s = MagicMock()

    async def _list_events(namespace, field_selector=None, limit=None, **kw):
        if explode:
            raise RuntimeError("events should not have been read")
        wanted = (field_selector or "").removeprefix("reason=")
        items = [
            SimpleNamespace(reason=r, message=m, last_timestamp=i)
            for i, (r, m) in enumerate(events or [])
            if not wanted or r == wanted
        ]
        return SimpleNamespace(items=items[:limit or len(items)])

    k8s.core_api.list_namespaced_event = _list_events
    return k8s


@pytest.mark.asyncio
async def test_the_reason_is_found_in_a_namespace_full_of_noise() -> None:
    """The failure measured live, and the only one that mattered.

    A tenant namespace accumulates hundreds of routine events. Asking for a
    page of them and hoping the interesting one is inside is not a filter —
    it is a lottery, and it lost.
    """
    noise = [("SuccessfulCreate", f"created pod {i}") for i in range(300)]
    blocking = [("FailedScheduling", "0/6 nodes are available: 3 Insufficient memory")]

    stall = await _rollout_stall(
        _k8s(noise + blocking + noise),
        "tenant-t1",
        {"replicas": 2},
        {"replicas": 3, "updatedReplicas": 0},
    )

    assert stall is not None and "Insufficient memory" in stall


@pytest.mark.asyncio
async def test_a_stalled_rollout_names_the_quota() -> None:
    stall = await _rollout_stall(
        _k8s([("FailedCreate",
               'pods "virt-launcher-x" is forbidden: exceeded quota: t-quota')]),
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
async def test_a_rollout_that_is_merely_in_progress_says_nothing() -> None:
    """The counts alone cannot tell "stalled" from "working".

    Measured on the acceptance run: a healthy vCPU change through the UI
    reported itself as stalled for the two minutes it took to roll, because
    `updatedReplicas < replicas` is true throughout a perfectly good rollout.
    Crying wolf on every routine change is how a real stall stops being
    noticed, so nothing is said unless something can be named.
    """
    stall = await _rollout_stall(
        _k8s([("SuccessfulCreate", "created pod virt-launcher-x")]),
        "tenant-t1",
        {"replicas": 2},
        {"replicas": 3, "readyReplicas": 2, "updatedReplicas": 1},
    )

    assert stall is None


@pytest.mark.asyncio
async def test_the_scheduler_refusing_counts_too() -> None:
    # Quota is one way a replacement never lands; a node that cannot hold it
    # is another, and it arrives as FailedScheduling instead.
    stall = await _rollout_stall(
        _k8s([("FailedScheduling", "0/6 nodes are available: insufficient memory")]),
        "tenant-t1",
        {"replicas": 2},
        {"replicas": 3, "updatedReplicas": 0},
    )

    assert stall is not None and "insufficient memory" in stall


@pytest.mark.asyncio
async def test_unreadable_events_stay_quiet() -> None:
    # Without the events there is no reason to give, and "stalled, cause
    # unknown" on a rollout that is simply slow is worse than silence.
    k8s = MagicMock()

    async def _boom(**kw):
        from kubernetes_asyncio.client import ApiException
        raise ApiException(status=403)

    k8s.core_api.list_namespaced_event = _boom

    assert await _rollout_stall(
        k8s, "tenant-t1", {"replicas": 2}, {"replicas": 3, "updatedReplicas": 1},
    ) is None


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
