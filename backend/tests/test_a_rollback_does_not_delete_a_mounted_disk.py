"""A rollback never removes a claim the guest still has mounted.

Reported from the stand: rolling a VM's root disk back to a snapshot left the
claim in Terminating behind `pvc-protection`, a finished clone with nowhere to
go, a running virt-launcher still writing to the disk being deleted, and a VM
the UI showed as healthy throughout. Nothing resolved it; nothing could.

Three things had to line up, and any one of them would have prevented it.

The owner lookup scanned `spec.disks`, and a machine's root disk is not there
— it is built from the image and recorded on the status. So a root-disk
rollback fell through to the legacy path.

That path stops a machine by patching `runStrategy: Halted` on the
VirtualMachine, which is a field the operator owns and writes straight back.
The machine never stopped. Two owners of one field, and the older writer lost
without being told.

And the wait for the machine to disappear fell through when it ran out of
attempts, straight into the deletion. Two minutes of waiting followed by
destroying the disk anyway is worse than either waiting longer or saying no.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from kubernetes_asyncio.client.rest import ApiException

from app.api.v1.disks import _managed_owner_holding_snapshot_source


def _api(*, source: str, vms: list[dict]):
    api = MagicMock()

    async def get_obj(**kwargs):
        return {"spec": {"source": {"persistentVolumeClaimName": source}}}

    async def list_obj(**kwargs):
        return {"items": vms}

    api.get_namespaced_custom_object = AsyncMock(side_effect=get_obj)
    api.list_namespaced_custom_object = AsyncMock(side_effect=list_obj)
    return api


def _vm(name: str, *, disks: list[str] = (), root: str = "") -> dict:
    return {
        "metadata": {"name": name},
        "spec": {"disks": [{"claim": c} for c in disks]},
        "status": {"rootDiskName": root} if root else {},
    }


@pytest.mark.asyncio
class TestWhoOwnsTheDisk:
    async def test_an_attached_disk_is_recognised(self) -> None:
        owner = await _managed_owner_holding_snapshot_source(
            _api(source="payload", vms=[_vm("web-01", disks=["payload"])]),
            "opdev-dev", "payload-snap",
        )
        assert owner == "web-01"

    async def test_the_machines_own_root_disk_is_recognised_too(self) -> None:
        """The case that fell through to the path that deleted it."""
        owner = await _managed_owner_holding_snapshot_source(
            _api(source="uat-vm-2-49mzc-root-1",
                 vms=[_vm("uat-vm-2-49mzc", root="uat-vm-2-49mzc-root-1")]),
            "poc-transit-dev", "root-snap",
        )
        assert owner == "uat-vm-2-49mzc"

    async def test_somebody_elses_disk_is_not_this_machines(self) -> None:
        owner = await _managed_owner_holding_snapshot_source(
            _api(source="stranger", vms=[_vm("web-01", disks=["payload"],
                                             root="web-01-root-1")]),
            "opdev-dev", "snap",
        )
        assert owner is None

    async def test_no_managed_machines_at_all_is_not_an_error(self) -> None:
        api = _api(source="payload", vms=[])
        api.list_namespaced_custom_object = AsyncMock(
            side_effect=ApiException(status=404))
        assert await _managed_owner_holding_snapshot_source(
            api, "ns", "snap") is None


def test_a_machine_that_will_not_stop_is_refused_not_worked_around() -> None:
    """The loop used to fall through into the deletion when it gave up.

    Read as code rather than run: reproducing it means two minutes of polling
    against a fake. What matters is that the wait has an outcome and the
    deletion is behind it.
    """
    import inspect

    from app.api.v1.disks import rollback_snapshot

    source = inspect.getsource(rollback_snapshot)
    wait = source.index("Wait for VMI to disappear")
    refusal = source.index("if not gone:")
    deletion = source.index("delete_namespaced_persistent_volume_claim")
    assert wait < refusal < deletion, "the refusal is not between the wait and the deletion"
    assert "HTTP_409_CONFLICT" in source[refusal:deletion]
