"""While the controller is working on a machine, the page can see it.

The endpoints that ask for an operation answer as soon as the resource is
written — a rollback returns before the clone is built, before the machine is
stopped, before anything has been rolled back. The UI reported that as the
result: "Rolled back to X. VM is restarting." with none of it true yet, and
then showed an idle machine for the minutes that followed, because a rollback
does not move `status` until it is over.

So the machine says what is being done to it, for as long as it is being done.
Finished operations are left out: history that looks like a current state is
the thing being fixed.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.vms import _operation_in_flight


def _op(name: str, vm: str, action: str, phase: str, *, created: str,
        message: str = "") -> dict:
    return {
        "metadata": {"name": name, "creationTimestamp": created},
        "spec": {"vmName": vm, "action": action},
        "status": {"phase": phase, "message": message},
    }


def _k8s(items: list[dict]):
    k8s = MagicMock()
    k8s.custom_api.list_namespaced_custom_object = AsyncMock(
        return_value={"items": items})
    return k8s


@pytest.mark.asyncio
class TestWhatTheMachineIsUnder:
    async def test_a_running_rollback_is_reported_with_its_message(self) -> None:
        info = await _operation_in_flight(_k8s([
            _op("rb-1", "web-01", "RollbackDisk", "Running",
                created="2026-08-22T15:07:00Z",
                message="waiting for the machine to stop before swapping its root disk"),
        ]), "opdev-dev", "web-01")
        assert info is not None
        assert info.action == "RollbackDisk"
        assert info.phase == "Running"
        assert "waiting for the machine to stop" in info.message

    async def test_a_finished_one_is_not_a_current_state(self) -> None:
        for phase in ("Succeeded", "Failed"):
            info = await _operation_in_flight(_k8s([
                _op("rb-1", "web-01", "RollbackDisk", phase,
                    created="2026-08-22T15:07:00Z"),
            ]), "opdev-dev", "web-01")
            assert info is None, phase

    async def test_another_machines_operation_is_not_this_ones(self) -> None:
        info = await _operation_in_flight(_k8s([
            _op("rb-1", "other-vm", "RollbackDisk", "Running",
                created="2026-08-22T15:07:00Z"),
        ]), "opdev-dev", "web-01")
        assert info is None

    async def test_the_newest_one_is_the_one_in_flight(self) -> None:
        info = await _operation_in_flight(_k8s([
            _op("old", "web-01", "Recreate", "Pending",
                created="2026-08-22T10:00:00Z"),
            _op("new", "web-01", "RollbackDisk", "Running",
                created="2026-08-22T15:07:00Z"),
        ]), "opdev-dev", "web-01")
        assert info is not None and info.name == "new"

    async def test_a_phase_the_controller_has_not_set_yet_still_counts(self) -> None:
        """The seconds between creating the resource and its first pass."""
        info = await _operation_in_flight(_k8s([
            {"metadata": {"name": "fresh", "creationTimestamp": "2026-08-22T15:07:00Z"},
             "spec": {"vmName": "web-01", "action": "RollbackDisk"}},
        ]), "opdev-dev", "web-01")
        assert info is not None and info.phase == "Pending"

    async def test_no_operator_installed_is_not_a_failure(self) -> None:
        k8s = MagicMock()
        k8s.custom_api.list_namespaced_custom_object = AsyncMock(
            side_effect=Exception("no such CRD"))
        assert await _operation_in_flight(k8s, "ns", "web-01") is None


def test_the_detail_response_carries_it() -> None:
    import inspect

    from app.api.v1.vms import get_vm

    assert "_operation_in_flight" in inspect.getsource(get_vm)
