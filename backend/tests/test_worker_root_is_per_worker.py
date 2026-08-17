"""Every Talos worker pointed at the same DataVolume, by name.

Not a latent tidiness problem — measured on the lab:

  * `tal1-talos-golden` was the only PVC in the namespace and the worker
    mounted it as root, so with one worker the golden image is no longer
    golden: the node writes into it and the next tenant clones a used disk;
  * with two workers it is two writers on one raw block device, RWX, and
    nothing complains;
  * the DV takes the first VM as its owner with `blockOwnerDeletion`, so
    deleting worker A deletes the DV and re-imports it (new UID,
    ImportScheduled). A rolling update, an MHC replacement or a scale-down
    wipes worker B's root disk. Silently, through a routine operation.

The fix is the template, because CAPK does the per-machine part itself:
proved by experiment, it rewrites `dataVolumeTemplates[].metadata.name` to
`<vm-name>-<template-name>` and fixes `volumes[].dataVolume.name` to match,
giving two workers two DVs each with its own controller owner. The earlier
"CAPK copies the spec verbatim, so the names collide" reading was wrong.
"""

import pytest

from app.api.v1.tenants_capi import (
    WORKER_ROOT_TEMPLATE,
    _build_kubevirt_machine_template_cr,
    _build_worker_data_volume_templates,
    _build_worker_root_volume,
    _larger_size,
    talos_golden_pvc_name,
)
from app.models.tenant import TenantCreateRequest


def _req(**kw) -> TenantCreateRequest:
    base = dict(name="t9", display_name="t9", folder="f", environment="e",
                worker_os="talos")
    base.update(kw)
    return TenantCreateRequest(**base)


class TestTheRootDiskIsNoLongerShared:
    def test_the_worker_does_not_mount_the_golden_pvc_itself(self) -> None:
        req = _req()

        vol = _build_worker_root_volume(req)

        assert vol["dataVolume"]["name"] != talos_golden_pvc_name(req)
        assert vol["dataVolume"]["name"] == WORKER_ROOT_TEMPLATE

    def test_the_template_clones_the_golden_image(self) -> None:
        [tpl] = _build_worker_data_volume_templates(_req())

        assert tpl["spec"]["source"] == {
            "pvc": {"name": "t9-talos-golden", "namespace": "tenant-t9"},
        }

    def test_the_volume_reference_matches_the_template_name(self) -> None:
        """CAPK renames both together; if they disagree here they disagree
        after renaming too, and the VM never starts."""
        req = _req()
        [tpl] = _build_worker_data_volume_templates(req)

        assert _build_worker_root_volume(req)["dataVolume"]["name"] == \
            tpl["metadata"]["name"]

    def test_it_is_in_the_machine_template_the_cluster_gets(self) -> None:
        """A builder nothing calls is the way this fix would quietly not ship."""
        cr = _build_kubevirt_machine_template_cr(_req())
        vm_spec = cr["spec"]["template"]["spec"]["virtualMachineTemplate"]["spec"]

        assert vm_spec["dataVolumeTemplates"], "no per-worker root disk in the CR"
        names = {v["dataVolume"]["name"] for v in vm_spec["template"]["spec"]["volumes"]
                 if "dataVolume" in v}
        assert names == {vm_spec["dataVolumeTemplates"][0]["metadata"]["name"]}


class TestCloudInitWorkersAreUntouched:
    def test_they_still_boot_a_container_disk(self, monkeypatch) -> None:
        monkeypatch.setenv("TENANTS_DEFAULT_WORKER_IMAGE", "quay.io/x/ubuntu:22.04")
        req = _req(worker_os="cloud-init")

        assert "containerDisk" in _build_worker_root_volume(req)

    def test_and_get_no_data_volume_templates(self, monkeypatch) -> None:
        monkeypatch.setenv("TENANTS_DEFAULT_WORKER_IMAGE", "quay.io/x/ubuntu:22.04")
        """An empty `dataVolumeTemplates` on a containerDisk VM is noise at
        best; KubeVirt validates the list against the volumes."""
        cr = _build_kubevirt_machine_template_cr(_req(worker_os="cloud-init"))
        vm_spec = cr["spec"]["template"]["spec"]["virtualMachineTemplate"]["spec"]

        assert "dataVolumeTemplates" not in vm_spec


class TestTheCloneIsNeverSmallerThanItsSource:
    def test_the_golden_size_is_the_floor(self) -> None:
        """CDI refuses a clone into a smaller target at admission, and the
        failure reads as every worker stuck for no visible reason."""
        [tpl] = _build_worker_data_volume_templates(_req(worker_disk="5Gi"))

        assert tpl["spec"]["storage"]["resources"]["requests"]["storage"] == "20Gi"

    def test_a_larger_request_is_honoured(self) -> None:
        [tpl] = _build_worker_data_volume_templates(_req(worker_disk="60Gi"))

        assert tpl["spec"]["storage"]["resources"]["requests"]["storage"] == "60Gi"

    def test_an_unparseable_size_falls_back_to_the_golden_size(self) -> None:
        assert _larger_size("twenty gigs", "20Gi") == "20Gi"

    @pytest.mark.parametrize("a,b,expected", [
        ("1Ti", "20Gi", "1Ti"),
        ("500Mi", "20Gi", "20Gi"),
        ("20Gi", "20Gi", "20Gi"),
        ("21474836480", "20Gi", "21474836480"),
    ])
    def test_units_are_compared_not_strings(self, a, b, expected) -> None:
        """String comparison would make "5Gi" larger than "20Gi"."""
        assert _larger_size(a, b) == expected


class TestTheSizeIsOneFactNotTwo:
    def test_the_import_and_the_clone_read_the_same_constant(self) -> None:
        import inspect

        from app.api.v1.tenants_talos import (
            DEFAULT_TALOS_GOLDEN_SIZE, ensure_talos_golden_image,
        )

        default = inspect.signature(ensure_talos_golden_image).parameters["size"].default
        assert default == DEFAULT_TALOS_GOLDEN_SIZE

        [tpl] = _build_worker_data_volume_templates(_req(worker_disk="1Gi"))
        assert tpl["spec"]["storage"]["resources"]["requests"]["storage"] == \
            DEFAULT_TALOS_GOLDEN_SIZE


class TestStorageClass:
    def test_it_follows_the_tenant(self) -> None:
        [tpl] = _build_worker_data_volume_templates(_req(storage_class="ceph-block"))

        assert tpl["spec"]["storage"]["storageClassName"] == "ceph-block"

    def test_omitted_when_unset_so_the_default_class_applies(self) -> None:
        """An empty string is not "the default" — it is a class named "", and
        the PVC stays Pending forever."""
        [tpl] = _build_worker_data_volume_templates(_req(storage_class=None))

        assert "storageClassName" not in tpl["spec"]["storage"]


class TestTheQuotaCountsTheDisksTheTenantProvisionsForItself:
    """The fix ran out of quota on its own acceptance run.

    `_tenant_quota` sized storage from `worker_disk` alone, which was right
    while every Talos worker shared one golden PVC. With a root clone per
    worker the namespace needs golden + N roots before a single tenant
    workload exists, and the two-worker tenant stopped halfway:

        ErrExceededQuota ... requested: requests.storage=21474836480,
        used: 53687091200, limited: 64424509440

    A quota that cannot fit the cluster it is provisioning is not a limit, it
    is a broken build — and it presents as a storage problem.
    """

    def test_talos_storage_covers_golden_and_the_roots(self) -> None:
        from app.api.v1.tenants_crud import _tenant_quota

        q = _tenant_quota(_req(worker_count=2, worker_disk="20Gi"))

        # data 3x20 + golden 20 + roots 3x20  (surge = workers + 1)
        assert int(q["storage"]) == (3 * 20 + 20 + 3 * 20) * 2**30

    def test_cloud_init_is_unchanged(self) -> None:
        """They boot a containerDisk — no DataVolume, nothing to count."""
        from app.api.v1.tenants_crud import _tenant_quota

        q = _tenant_quota(_req(worker_os="cloud-init", worker_count=2,
                               worker_disk="20Gi"))

        assert int(q["storage"]) == 3 * 20 * 2**30

    def test_the_surge_covers_a_replacement_clone(self) -> None:
        """Sized to exactly `worker_count`, the quota would refuse every
        worker replacement: the new clone exists while the old disk is still
        there, and CDI stages through a scratch PVC of the target size."""
        from app.api.v1.tenants_crud import _tenant_quota

        one = int(_tenant_quota(_req(worker_count=1))["storage"])
        two = int(_tenant_quota(_req(worker_count=2))["storage"])

        # One more worker costs one data disk and one root, not just one disk.
        assert two - one == (20 + 20) * 2**30

    def test_a_bigger_worker_disk_raises_both_halves(self) -> None:
        from app.api.v1.tenants_crud import _tenant_quota

        q = _tenant_quota(_req(worker_count=1, worker_disk="40Gi"))

        assert int(q["storage"]) == (2 * 40 + 20 + 2 * 40) * 2**30
