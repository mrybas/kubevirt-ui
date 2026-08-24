"""A VM restored from a backup has to come back with a working network.

Cloud-init on Ubuntu writes netplan pinned to the MAC it saw on first boot:

    enp1s0: {match: {macaddress: "6a:aa:5d:81:45:49"}, dhcp4: true}

KubeVirt generates that MAC when the VMI starts and keeps it only on the VMI,
which a backup does not carry, so the restored guest had an interface it did
not recognise:

    enp1s0  DOWN  b6:68:8e:58:ba:1d
    getent hosts example.com  -> rc=2

while Velero reported Completed and the VM page showed an IP the guest never
used.

Pinning the MAC into the VM spec fixed that and bought two new problems — a
clone copies the spec, and a restore made alongside the original is a second
VM with the same address on the same subnet. The guest is told to match its
NIC by name instead, so it stops caring what the MAC is.
"""

import re

import pytest
from pathlib import Path

import yaml

SRC = Path("app/api/v1/vms.py").read_text()


class TestNothingPinsAMac:
    def test_no_interface_carries_a_macaddress(self) -> None:
        assert "macAddress" not in SRC, "a pinned MAC duplicates itself on clone"

    def test_the_ovn_port_is_not_pinned_either(self) -> None:
        assert "ovn.kubernetes.io/mac_address" not in SRC

    def test_and_nothing_generates_one(self) -> None:
        assert "allocate_mac" not in SRC


class TestTheGuestMatchesByName:
    def _network_data(self) -> dict:
        m = re.search(r'GUEST_NETWORK_DATA = """(.*?)"""', SRC, re.S)
        assert m, "GUEST_NETWORK_DATA not found"
        return yaml.safe_load(m.group(1))

    def test_it_is_valid_netplan_v2(self) -> None:
        data = self._network_data()
        assert data["version"] == 2
        assert "ethernets" in data

    def test_it_matches_on_the_interface_name_and_never_a_mac(self) -> None:
        eth = next(iter(self._network_data()["ethernets"].values()))
        assert "name" in eth["match"]
        assert "macaddress" not in eth["match"]

    def test_it_asks_for_dhcp(self) -> None:
        eth = next(iter(self._network_data()["ethernets"].values()))
        assert eth["dhcp4"] is True

    def test_a_nic_without_carrier_does_not_hold_the_boot(self) -> None:
        eth = next(iter(self._network_data()["ethernets"].values()))
        assert eth.get("optional") is True


class TestItIsAlwaysDelivered:
    def test_the_cloud_init_disk_is_attached_even_with_no_user_data(self) -> None:
        # Without a datasource cloud-init writes its own fallback config —
        # matched on the MAC again.
        block = SRC[SRC.index('ci: dict[str, str]'):SRC.index('# Handle network configuration')]
        assert '"networkData": GUEST_NETWORK_DATA' in block
        assert 'if cloud_init_data:' in block
        assert block.index('ci: dict[str, str]') < block.index('if cloud_init_data:')


class TestTheDiskFailureReachesTheUI:
    """A clone refused for storage quota said only "VMI does not exist".

    The real reason lives on the DataVolume — and a CSI clone needs a
    temporary PVC on top of the disk it creates, so a quota that fits the disk
    exactly still refuses the clone:

        persistentvolumeclaims "tmp-pvc-591d600d…" is forbidden: exceeded
        quota: acme-dev-quota, requested: requests.storage=10737418240,
        used: requests.storage=85899345920
    """

    def test_the_detail_handler_appends_datavolume_conditions(self) -> None:
        assert "_datavolume_blockers" in SRC
        # the handler extends whatever conditions the serializer produced
        assert "resp.conditions" in SRC
        assert "await _datavolume_blockers(k8s_client, namespace, vm)" in SRC

    def test_it_looks_at_both_volumes_and_templates(self) -> None:
        block = SRC[SRC.index("async def _datavolume_blockers"):SRC.index("@router.get(\"/{name}\"")]
        assert 'v.get("dataVolume")' in block
        assert '"dataVolumeTemplates"' in block

    def test_a_ready_disk_contributes_nothing(self) -> None:
        block = SRC[SRC.index("async def _datavolume_blockers"):SRC.index("@router.get(\"/{name}\"")]
        assert 'if status.get("phase") == "Succeeded":' in block
        assert "continue" in block

    def test_it_prefers_the_condition_that_says_why(self) -> None:
        # A stuck disk reports several unhappy conditions at once; only the
        # one with reason=Error carries "forbidden: exceeded quota…", the
        # rest just repeat that it is Pending.
        block = SRC[SRC.index("async def _datavolume_blockers"):SRC.index("@router.get(\"/{name}\"")]
        assert 'c.get("reason") == "Error"' in block


class TestAStuckMigrationIsReported:
    """A live migration runs a second launcher beside the first, so the VM
    counts twice against the environment quota for its duration. With none to
    spare it just waits:

        migrate-nomac-88jjm-2mw42  Pending
          migrationRejectedByResourceQuota=True

    and nothing said so — the VM kept running on its old node and the request
    looked accepted.

    These used to grep the source for strings the handler contains, which
    transcribes the code rather than measuring it. They call it now, which is
    also how the second answer — is anything still in flight — is covered at
    all. That answer exists because a page waiting on a fixed schedule stopped
    asking after twelve seconds and a measured migration took forty-five.
    """

    @staticmethod
    async def _state(items: list[dict], vm: str = "web-01"):
        from unittest.mock import AsyncMock, MagicMock

        from app.api.v1.vms import _migration_state

        k8s = MagicMock()
        k8s.custom_api.list_namespaced_custom_object = AsyncMock(
            return_value={"items": items})
        return await _migration_state(k8s, "opdev-dev", vm)

    @staticmethod
    def _migration(vm: str, phase: str | None, *, rejected: bool = False) -> dict:
        return {
            "metadata": {"name": f"migrate-{vm}-abcde"},
            "spec": {"vmiName": vm},
            "status": {
                "phase": phase,
                "conditions": [{
                    "type": "migrationRejectedByResourceQuota", "status": "True",
                }] if rejected else [],
            },
        }

    @pytest.mark.asyncio
    async def test_a_finished_migration_says_nothing(self) -> None:
        for phase in ("Succeeded", "Failed", None):
            blockers, in_flight = await self._state(
                [self._migration("web-01", phase)])
            assert blockers == []
            assert in_flight is None, phase

    @pytest.mark.asyncio
    async def test_one_in_flight_is_reported_and_kept_track_of(self) -> None:
        blockers, in_flight = await self._state(
            [self._migration("web-01", "Running")])
        assert in_flight == "Running"
        assert len(blockers) == 1
        assert "Running" in blockers[0]["message"]

    @pytest.mark.asyncio
    async def test_the_quota_is_named_when_that_is_the_cause(self) -> None:
        blockers, _ = await self._state(
            [self._migration("web-01", "Pending", rejected=True)])
        assert "second copy of the VM" in blockers[0]["message"]
        assert "ResourceQuota" in blockers[0]["message"]

    @pytest.mark.asyncio
    async def test_another_vms_migration_is_not_this_one(self) -> None:
        blockers, in_flight = await self._state(
            [self._migration("other-vm", "Running")])
        assert blockers == [] and in_flight is None

    @pytest.mark.asyncio
    async def test_a_cluster_that_cannot_be_read_blocks_nothing(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from app.api.v1.vms import _migration_state

        k8s = MagicMock()
        k8s.custom_api.list_namespaced_custom_object = AsyncMock(
            side_effect=Exception("nope"))
        assert await _migration_state(k8s, "ns", "web-01") == ([], None)

    def test_the_phase_reaches_the_response(self) -> None:
        """The field is what a client waits on; unset, the wait is a guess."""
        import inspect

        from app.api.v1.vms import get_vm

        assert "migration_phase" in inspect.getsource(get_vm)
