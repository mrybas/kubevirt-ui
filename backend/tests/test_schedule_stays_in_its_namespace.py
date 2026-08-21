"""A schedule cannot reach into another environment.

The authorization on these routes is on the `namespace` query parameter. The
CronJob it creates runs `kubectl` against `vm_namespace` **from the body**,
under `kubevirt-ui-scheduler`, which is cluster-wide. Nothing tied the two
together, so a member of one environment could schedule

    kubectl delete vm <name> -n <someone-else's-environment>

and it would fire on a cron, from a namespace they are entitled to, at a machine
they are not.

The mismatch has no legitimate caller: the UI sends the VM's own namespace, and
the schedule is created beside the VM. It was already a degraded case — an
ownerReference cannot cross namespaces, so such a schedule was left untied and
outlived the machine it acts on.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1.schedules import (
    CreateScheduleRequest,
    NS_LABEL,
    create_scheduled_action,
    trigger_scheduled_action,
)


def _request():
    request = MagicMock()
    request.app.state.k8s_client = MagicMock()
    return request


def _ask(vm_namespace: str) -> CreateScheduleRequest:
    return CreateScheduleRequest(
        display_name="nightly stop", action="stop", schedule="0 18 * * *",
        vm_name="victim", vm_namespace=vm_namespace,
    )


@pytest.mark.asyncio
async def test_a_schedule_for_another_environment_is_refused() -> None:
    with pytest.raises(HTTPException) as e:
        await create_scheduled_action(
            _request(), namespace="poc-transit-dev",
            schedule_request=_ask("finance-prod"), user=MagicMock(),
        )
    assert e.value.status_code == 403
    assert "finance-prod" in e.value.detail


@pytest.mark.asyncio
async def test_the_ordinary_case_still_works(monkeypatch) -> None:
    """Same namespace on both sides — which is all the UI ever sends."""
    import app.api.v1.schedules as mod

    created: dict = {}
    batch = MagicMock()
    batch.create_namespaced_cron_job = AsyncMock(
        return_value=SimpleNamespace(metadata=SimpleNamespace(
            name="sched-victim-stop", namespace="poc-transit-dev")),
    )

    def batch_api(_client):
        return batch

    monkeypatch.setattr(mod.client, "BatchV1Api", batch_api)
    monkeypatch.setattr(mod, "_own_the_schedule", AsyncMock(side_effect=
        lambda *a, **k: created.setdefault("owned", True)))

    out = await create_scheduled_action(
        _request(), namespace="poc-transit-dev",
        schedule_request=_ask("poc-transit-dev"), user=MagicMock(),
    )

    assert out["name"] == "sched-victim-stop"
    batch.create_namespaced_cron_job.assert_awaited()


@pytest.mark.asyncio
async def test_an_older_cross_namespace_schedule_will_not_be_run(monkeypatch) -> None:
    """Creation refuses these now; the ones already written do not know that.

    Running one on demand is the same act as creating it, and the target is read
    off the object rather than taken from the caller.
    """
    import app.api.v1.schedules as mod

    batch = MagicMock()
    batch.read_namespaced_cron_job = AsyncMock(
        return_value=SimpleNamespace(
            metadata=SimpleNamespace(
                name="sched-victim-delete",
                labels={NS_LABEL: "finance-prod"},
            ),
            spec=SimpleNamespace(job_template=SimpleNamespace(
                spec=SimpleNamespace(to_dict=lambda: {}))),
        ),
    )
    batch.create_namespaced_job = AsyncMock()
    monkeypatch.setattr(mod.client, "BatchV1Api", lambda _c: batch)

    with pytest.raises(HTTPException) as e:
        await trigger_scheduled_action(
            _request(), namespace="poc-transit-dev",
            name="sched-victim-delete", user=MagicMock(),
        )

    assert e.value.status_code == 403
    batch.create_namespaced_job.assert_not_awaited()
