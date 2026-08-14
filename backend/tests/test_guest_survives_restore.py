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
        assert "resp.conditions = list(resp.conditions or [])" in SRC

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
