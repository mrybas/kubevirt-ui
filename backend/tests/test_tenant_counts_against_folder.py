"""A tenant spends its folder's budget like anything else.

Two tenants — 1 worker of 2 vCPU / 2Gi each, plus their control planes — ran
inside folder `acme` while the folder ceiling saw only the standalone VM:

    acme-dev quota used: requests.cpu 2065m, requests.memory 4.8Gi
    tenant-tci   labels: kubevirt-ui.io/folder=acme   (no ResourceQuota)
    tenant-ttalos labels: kubevirt-ui.io/folder=acme  (no ResourceQuota)

The namespaces already carry the folder label, so a quota on them is counted
by `_own_env_quota` automatically — there simply was none.
"""

from unittest.mock import MagicMock

import pytest

from app.api.v1.tenants_crud import _tenant_quota


def _req(**kw):
    req = MagicMock()
    req.worker_count = 1
    req.worker_vcpu = 2
    req.worker_memory = "2Gi"
    req.worker_disk = "20Gi"
    req.control_plane_replicas = 1
    for k, v in kw.items():
        setattr(req, k, v)
    return req


class TestTenantQuota:
    def test_counts_the_workers_plus_one_for_replacement(self) -> None:
        # Replacing a worker overlaps with it: sized to exactly the worker
        # count, the quota refused every replacement and a tenant whose
        # worker died could never get a new one.
        q = _tenant_quota(_req(worker_count=3, control_plane_replicas=0))
        assert q["cpu"] == "8"                              # (3+1) x 2 vCPU
        assert int(q["memory"]) == 4 * 2 * 1024 ** 3
        assert int(q["storage"]) == 4 * 20 * 1024 ** 3

    def test_a_single_worker_tenant_can_replace_its_only_worker(self) -> None:
        q = _tenant_quota(_req(worker_count=1, worker_vcpu=2, control_plane_replicas=0))
        assert float(q["cpu"]) >= 4

    def test_adds_an_allowance_per_control_plane_replica(self) -> None:
        one = _tenant_quota(_req(control_plane_replicas=1))
        three = _tenant_quota(_req(control_plane_replicas=3))
        assert float(three["cpu"]) > float(one["cpu"])
        assert int(three["memory"]) > int(one["memory"])

    def test_the_control_plane_needs_no_storage(self) -> None:
        q = _tenant_quota(_req(worker_count=1, control_plane_replicas=3))
        assert int(q["storage"]) == 2 * 20 * 1024 ** 3   # worker + its replacement

    def test_a_bigger_worker_costs_more(self) -> None:
        small = _tenant_quota(_req(worker_vcpu=2, worker_memory="2Gi"))
        big = _tenant_quota(_req(worker_vcpu=8, worker_memory="16Gi"))
        assert float(big["cpu"]) > float(small["cpu"])
        assert int(big["memory"]) > int(small["memory"])


class TestItIsCheckedAndWritten:
    def test_the_creation_path_checks_the_ceiling_before_creating(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/tenants_crud.py").read_text()
        check = src.index("assert_within_folder_quota(")
        create = src.index("await _create_namespace(")
        assert check < create, "the ceiling must be checked before anything exists"

    def test_and_writes_the_quota_onto_the_tenant_namespace(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/tenants_crud.py").read_text()
        assert "await _write_tenant_quota(k8s, ns, tenant_quota)" in src

    @pytest.mark.asyncio
    async def test_writing_it_twice_replaces_rather_than_failing(self) -> None:
        # Scaling calls this again with new numbers; the second write has to
        # land, not be swallowed.
        from unittest.mock import AsyncMock

        from kubernetes_asyncio.client.rest import ApiException

        from app.api.v1.tenants_crud import _write_tenant_quota

        k8s = MagicMock()
        k8s.core_api.create_namespaced_resource_quota = AsyncMock(
            side_effect=ApiException(status=409),
        )
        k8s.core_api.replace_namespaced_resource_quota = AsyncMock()
        await _write_tenant_quota(k8s, "tenant-tci", {
            "cpu": "2", "memory": "1", "storage": "1",
        })
        k8s.core_api.replace_namespaced_resource_quota.assert_awaited_once()


@pytest.mark.asyncio
class TestScalingResizesTheQuota:
    """The quota written at creation made every scale-up a half-failure.

    The MachineDeployment scaled, and each new worker's pod was then refused
    for exceeding a quota sized for the tenant as it used to be — visible only
    in events, with the tenant reporting the new worker count it never got.
    """

    async def test_the_scale_path_resizes_it(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/tenants_crud.py").read_text()
        scale = src[src.index("async def scale_tenant"):src.index("@router.get(\"/{name}/storage/status\"")]
        assert "_write_tenant_quota(k8s, ns, planned_quota)" in scale

    async def test_it_asks_the_ceiling_excluding_the_tenant_itself(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/tenants_crud.py").read_text()
        block = src[src.index("async def _plan_tenant_quota"):src.index("async def _current_worker_shape")]
        assert "assert_within_folder_quota" in block
        assert "exclude_namespace=ns" in block

    async def test_the_ceiling_is_asked_before_anything_is_patched(self) -> None:
        # A refusal must leave the tenant exactly as it was; checking after
        # the patch left the MachineDeployment already scaled.
        from pathlib import Path

        src = Path("app/api/v1/tenants_crud.py").read_text()
        start = src.index("async def scale_tenant")
        scale = src[start:src.index("@router.", start + 10)]
        assert scale.index("_plan_tenant_quota(k8s, name, ns, scale)") < scale.index(
            "patch_namespaced_custom_object",
        )

    async def test_the_shape_comes_from_the_objects_not_from_guesswork(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/tenants_crud.py").read_text()
        block = src[src.index("async def _current_worker_shape"):]
        block = block[:block.index("\n\n\n")]
        assert "kubevirtmachinetemplates" in block
        assert "kamajicontrolplanes" in block
        assert "dataVolumeTemplates" in block

    async def test_writing_the_quota_replaces_an_existing_one(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from kubernetes_asyncio.client.rest import ApiException

        from app.api.v1.tenants_crud import _write_tenant_quota

        k8s = MagicMock()
        k8s.core_api.create_namespaced_resource_quota = AsyncMock(
            side_effect=ApiException(status=409),
        )
        k8s.core_api.replace_namespaced_resource_quota = AsyncMock()

        await _write_tenant_quota(k8s, "tenant-tci", {
            "cpu": "4", "memory": "8", "storage": "40",
        })
        k8s.core_api.replace_namespaced_resource_quota.assert_awaited_once()
