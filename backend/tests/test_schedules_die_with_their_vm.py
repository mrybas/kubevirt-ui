"""A schedule that outlives its machine is worse than no schedule.

The link between a scheduled action and the VM it acts on was a name in a label
and a name inside a shell command. Nothing enforced that the machine existed, so
a schedule kept firing kubectl at something that had been deleted — and a new
machine created with the same name silently inherited every schedule the old one
had.

An ownerReference hands that to the cluster's own garbage collector: no
controller, no finalizer, and it still works when nothing of ours is running.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.v1.schedules import CreateScheduleRequest


async def _create_schedule(
    *, vm_exists: bool = True, vm_namespace: str = "opdev-dev",
) -> dict[str, Any]:
    from app.api.v1 import schedules

    captured: dict[str, Any] = {}

    async def _create_cron(namespace: str, body: dict[str, Any]) -> Any:
        captured["body"] = body
        result = MagicMock()
        result.metadata.name = "nightly-stop-x7k2p"
        result.metadata.namespace = namespace
        result.metadata.creation_timestamp = None
        return result

    async def _get_vm(**kwargs: Any) -> dict[str, Any]:
        if not vm_exists:
            raise Exception("no such VM")
        return {"metadata": {"name": kwargs["name"], "uid": "11111111-2222-3333-4444-555555555555"}}

    batch = MagicMock()
    batch.create_namespaced_cron_job = AsyncMock(side_effect=_create_cron)
    custom = MagicMock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_get_vm)

    k8s = MagicMock()
    request = MagicMock()
    request.app.state.k8s_client = k8s

    req = CreateScheduleRequest(
        display_name="Nightly stop",
        vm_name="web-01",
        vm_namespace=vm_namespace,
        action="stop",
        schedule="0 22 * * *",
    )

    with (
        patch.object(schedules.client, "BatchV1Api", return_value=batch),
        patch.object(schedules.client, "CustomObjectsApi", return_value=custom),
    ):
        await schedules.create_scheduled_action(
            request=request, namespace="opdev-dev",
            schedule_request=req, user=MagicMock(),
        )
    return captured["body"]


@pytest.mark.asyncio
class TestSchedulesAreOwnedByTheirMachine:
    async def test_the_schedule_is_tied_to_the_machine(self) -> None:
        body = await _create_schedule()
        owners = body["metadata"]["ownerReferences"]
        assert len(owners) == 1
        assert owners[0]["kind"] == "VirtualMachine"
        assert owners[0]["name"] == "web-01"
        assert owners[0]["uid"] == "11111111-2222-3333-4444-555555555555"

    async def test_the_link_does_not_block_deleting_the_machine(self) -> None:
        """A machine should not wait on its schedules to be swept up."""
        body = await _create_schedule()
        assert body["metadata"]["ownerReferences"][0]["blockOwnerDeletion"] is False

    async def test_a_cross_namespace_schedule_never_gets_this_far(self) -> None:
        """This used to assert the degraded case, and the case is gone.

        An ownerReference cannot cross namespaces — the garbage collector would
        read one as an owner that does not exist and delete the schedule at
        once — so a mismatched schedule was created untied, which is exactly the
        outliving-its-machine problem this file is about.

        It turned out to be a security hole as well: authorization is on the
        namespace the CronJob lives in, and the command inside it targets the
        one from the body. Creation refuses the mismatch now, so the untied
        schedule cannot be made at all. `_own_the_schedule` keeps its own check
        below, unreachable through the endpoint and correct on its own terms.
        """
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as e:
            await _create_schedule(vm_namespace="somewhere-else")
        assert e.value.status_code == 403

    async def test_the_ownership_helper_still_refuses_to_cross_namespaces(
        self,
    ) -> None:
        """Belt and braces: the helper is not the guard, but it must not be the
        thing that creates a self-deleting object if it is ever called again."""
        from app.api.v1 import schedules

        body: dict[str, Any] = {"metadata": {}}
        req = CreateScheduleRequest(
            display_name="x", vm_name="web-01", vm_namespace="somewhere-else",
            action="stop", schedule="0 22 * * *",
        )
        await schedules._own_the_schedule(MagicMock(), body, req, "opdev-dev")
        assert "ownerReferences" not in body["metadata"]

    async def test_a_schedule_is_still_created_when_the_machine_cannot_be_read(
        self,
    ) -> None:
        """Best effort: an untied schedule is the old behaviour, not a failure."""
        body = await _create_schedule(vm_exists=False)
        assert "ownerReferences" not in body["metadata"]
        assert body["kind"] == "CronJob"
