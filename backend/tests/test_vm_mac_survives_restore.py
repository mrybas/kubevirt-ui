"""A restored VM has to come back with the network it was configured for.

Restoring `web1` from a Velero backup produced a VM that KubeVirt reported
Running with IP 10.100.0.6 — and a guest with no address at all:

    enp1s0  DOWN  b6:68:8e:58:ba:1d
    netplan: enp1s0: {match: {macaddress: "6a:aa:5d:81:45:49"}, dhcp4: true}

The MAC lived only on the VMI, which a backup does not restore, so every
restore handed the guest an interface its cloud-init-written netplan does not
match. Velero said Completed; the VM was unreachable.
"""

import re

from app.api.v1.vms import allocate_mac

MAC_RE = re.compile(r"^02(:[0-9a-f]{2}){5}$")


class TestAllocateMac:
    def test_is_a_locally_administered_unicast_address(self) -> None:
        mac = allocate_mac()
        assert MAC_RE.match(mac), mac
        first = int(mac.split(":")[0], 16)
        assert first & 0b10, "locally administered bit must be set"
        assert not first & 0b1, "must not be a multicast address"

    def test_does_not_repeat(self) -> None:
        macs = {allocate_mac() for _ in range(500)}
        assert len(macs) == 500


class TestEveryNicCarriesOne:
    """The point is that the MAC is in the VM object a backup captures."""

    def _interfaces(self, source: str) -> list[str]:
        """Each `iface_specs.append(...)` call, up to its closing paren.

        A regex on braces stops at the first `}` — the specs contain
        `"bridge": {}` — so this walks the parentheses instead.
        """
        out: list[str] = []
        for m in re.finditer(r"iface_specs\.append\(", source):
            i, depth = m.end() - 1, 0
            while i < len(source):
                if source[i] == "(":
                    depth += 1
                elif source[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            out.append(source[m.start():i + 1])
        return out

    def test_the_vpc_and_vlan_paths_both_pin_a_mac(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/vms.py").read_text()
        blocks = self._interfaces(src)
        assert blocks, "no interface specs found — did the builder move?"
        for block in blocks:
            assert "macAddress" in block, f"NIC built without a MAC:\n{block}"

    def test_the_fallback_nic_too(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/vms.py").read_text()
        fallback = re.search(
            r'vm_spec\["domain"\]\["devices"\]\["interfaces"\] = \[\s*\{[^]]*\]', src, re.S,
        )
        assert fallback and "macAddress" in fallback.group(0)


class TestTheOvnPortGetsTheSameMac:
    """Pinning the guest MAC alone leaves the VM unreachable.

    With bridge binding the guest's frames go on the wire, and kube-ovn binds
    the logical switch port to the MAC *it* allocated. Measured on the cluster
    after pinning the MAC without telling kube-ovn:

        guest    02:f3:2d:80:64:96   (interface UP, 10.100.0.8/24)
        OVN port 96:77:a8:8c:7b:3b
        a pod on the same subnet: tcp/22 on the VM unreachable, DNS rc=2

    So the same MAC has to be requested for the port via the
    `ovn.kubernetes.io/mac_address` annotation.
    """

    def test_the_annotation_is_stamped_from_the_primary_nic(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/vms.py").read_text()
        assert 'template_annotations["ovn.kubernetes.io/mac_address"] = primary_mac' in src
        # …and it is the MAC that actually went onto interface 0.
        assert "primary_mac = mac" in src

    def test_it_sits_next_to_the_logical_switch_annotation(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/vms.py").read_text()
        switch = src.index('template_annotations["ovn.kubernetes.io/logical_switch"]')
        mac = src.index('template_annotations["ovn.kubernetes.io/mac_address"] = primary_mac')
        assert 0 < mac - switch < 1500, "the port's MAC must be set on the same path"

    def test_the_fallback_nic_stamps_it_too(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/vms.py").read_text()
        assert 'template_annotations["ovn.kubernetes.io/mac_address"] = fallback_mac' in src
