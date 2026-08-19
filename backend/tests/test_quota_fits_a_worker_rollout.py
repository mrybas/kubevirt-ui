"""The surge slot has to hold a *pod*, not a VM.

The quota already reserved one extra worker so a replacement can overlap with
the machine it replaces. It reserved the guest's declared memory — and
KubeVirt never schedules a VM on that number. virt-launcher asks for the
guest plus its own overhead.

Measured in UAT 2026-08-19, on a two-worker Talos tenant:

    hard requests.memory  8Gi          (3 slots x 2Gi + 2 CP x 1Gi)
    used                  5.79Gi
    replacement pod       2.273Gi      against 2.21Gi free

Short by 0.06Gi. The rollout never started, so it never finished: the VMI sat
Pending behind `exceeded quota`, the events were buried in `describe`, and
the tenant page went on reporting the cluster as healthy. Any worker-template
change — including the vCPU/memory edit the UI offers behind an `Apply`
button — deadlocked the same way.

The overhead is a formula, not a constant. Read straight off this stand:

    2Gi  / 2 vCPU  ->  2.273Gi   overhead 280Mi
    8Gi  / 4 vCPU  ->  8.301Gi   overhead 308Mi
   32Gi  / 8 vCPU  -> 32.379Gi   overhead 388Mi

A flat reserve chosen from the 2Gi row looks generous and still starves a
large worker, which is the trap these tests exist to hold shut.
"""

import pytest

from app.api.v1.tenants_crud import (
    _CP_MEMORY,
    _tenant_quota,
    _vmi_memory_overhead,
)
from app.models.tenant import TenantCreateRequest

Gi = 1024 ** 3
Mi = 1024 ** 2

# (guest memory, vCPU, what virt-launcher actually requested)
MEASURED = [
    (2 * Gi, 2, 2.273 * Gi),
    (8 * Gi, 4, 8.301 * Gi),
    (32 * Gi, 8, 32.379 * Gi),
]


def _req(**kw) -> TenantCreateRequest:
    base = dict(
        name="q1", display_name="q1", folder="f", environment="e",
        worker_os="talos", kubernetes_version="v1.32.1",
        worker_count=2, worker_vcpu=2, worker_memory="2Gi",
        control_plane_replicas=2,
    )
    base.update(kw)
    return TenantCreateRequest(**base)


@pytest.mark.parametrize("memory,vcpu,pod_request", MEASURED)
def test_the_reserve_covers_what_virt_launcher_really_asks_for(
    memory: int, vcpu: int, pod_request: float,
) -> None:
    observed = pod_request - memory
    assert _vmi_memory_overhead(memory, vcpu) >= observed, (
        f"a {memory // Gi}Gi/{vcpu}-vCPU worker's pod asked for "
        f"{observed / Mi:.0f}Mi above the guest and the quota reserves less "
        f"— the surge slot cannot be occupied and the rollout deadlocks"
    )


def test_the_reserve_grows_with_the_machine() -> None:
    """The trap a flat constant walks into.

    Overhead is dominated by page tables at large sizes (memory/512), so a
    reserve picked from a 2Gi worker is roughly a third of what a 256Gi one
    needs. Anything that stops scaling here brings the deadlock back for big
    tenants only — the ones least likely to be tested.
    """
    small = _vmi_memory_overhead(2 * Gi, 2)
    huge = _vmi_memory_overhead(256 * Gi, 32)

    assert huge > 2 * small
    assert huge >= 256 * Gi // 512, "page tables alone exceed the reserve"


def test_the_surge_slot_holds_a_launcher_not_a_guest() -> None:
    quota = int(_tenant_quota(_req())["memory"])

    per_worker = 2 * Gi + _vmi_memory_overhead(2 * Gi, 2)
    assert quota >= 2 * per_worker + 2 * _CP_MEMORY + per_worker, (
        "no room for one replacement pod while the workers it replaces are "
        "still running — the rollout will sit in `exceeded quota` forever "
        "and the tenant will still look healthy"
    )


def test_the_uat_deadlock_would_not_happen_again() -> None:
    # The exact shape that deadlocked, with the numbers it deadlocked on.
    quota = int(_tenant_quota(_req())["memory"])
    used_by_two_workers_and_cp = 6218849664      # measured, 5.79Gi
    replacement_pod = int(2.273 * Gi)            # measured on this stand

    assert quota - used_by_two_workers_and_cp >= replacement_pod, (
        f"only {(quota - used_by_two_workers_and_cp) / Gi:.2f}Gi free for a "
        f"{replacement_pod / Gi:.2f}Gi replacement pod — this is the UAT "
        f"deadlock, unchanged"
    )


@pytest.mark.parametrize("worker_memory,vcpu", [("2Gi", 2), ("8Gi", 4), ("32Gi", 8)])
def test_every_slot_carries_its_own_overhead(worker_memory: str, vcpu: int) -> None:
    # Not just the surge one: each running worker needs it too, or the
    # shortfall simply moves to the last worker to be scheduled.
    req = _req(worker_memory=worker_memory, worker_vcpu=vcpu)
    quota = int(_tenant_quota(req)["memory"])
    declared = int(worker_memory[:-2]) * Gi
    slots = req.worker_count + 1

    assert quota >= slots * (
        declared + _vmi_memory_overhead(declared, vcpu)
    ) + 2 * _CP_MEMORY


def test_cpu_is_left_alone() -> None:
    # Measured: two running workers and the control plane requested 930m
    # against a 7-core quota. CPU never binds, and inflating it would make
    # the folder ceiling refuse tenants that fit perfectly well.
    assert _tenant_quota(_req())["cpu"] == "7"


def test_worker_vms_stay_within_the_reserved_shape() -> None:
    """The reserve is only right for the VM the product actually builds.

    KubeVirt's overhead grows a term for each of these, and the quota
    accounts for none of them: hugepages pin the whole guest, a dedicated CPU
    placement changes the launcher's request, VFIO/GPU passthrough adds a
    fixed 1Gi per device, downward metrics add a page. Worker VMs set only
    cores, guest memory, interfaces and disks — so the reserve holds.

    If a feature below ever lands on the worker template, this fails on the
    same commit rather than as a rollout that hangs on a large tenant months
    later.
    """
    from app.api.v1.tenants_capi import _build_kubevirt_machine_template_cr

    cr = _build_kubevirt_machine_template_cr(_req())
    domain = (
        cr["spec"]["template"]["spec"]["virtualMachineTemplate"]["spec"]
        ["template"]["spec"]["domain"]
    )

    unaccounted = {
        "memory.hugepages": (domain.get("memory") or {}).get("hugepages"),
        "cpu.dedicatedCpuPlacement": (domain.get("cpu") or {}).get(
            "dedicatedCpuPlacement"),
        "cpu.isolateEmulatorThread": (domain.get("cpu") or {}).get(
            "isolateEmulatorThread"),
        "devices.gpus": (domain.get("devices") or {}).get("gpus"),
        "devices.hostDevices": (domain.get("devices") or {}).get("hostDevices"),
        "devices.downwardMetrics": (domain.get("devices") or {}).get(
            "downwardMetrics"),
        "launchSecurity": domain.get("launchSecurity"),
    }
    present = {k: v for k, v in unaccounted.items() if v}

    assert not present, (
        f"the worker template now sets {sorted(present)}, and virt-launcher "
        f"asks for more memory because of it — extend _vmi_memory_overhead "
        f"with the matching term before shipping this, or the surge slot "
        f"goes back to being too small"
    )
