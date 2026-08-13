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
    def test_counts_the_workers(self) -> None:
        q = _tenant_quota(_req(worker_count=3, control_plane_replicas=0))
        assert q["cpu"] == "6"
        assert int(q["memory"]) == 3 * 2 * 1024 ** 3
        assert int(q["storage"]) == 3 * 20 * 1024 ** 3

    def test_adds_an_allowance_per_control_plane_replica(self) -> None:
        one = _tenant_quota(_req(control_plane_replicas=1))
        three = _tenant_quota(_req(control_plane_replicas=3))
        assert float(three["cpu"]) > float(one["cpu"])
        assert int(three["memory"]) > int(one["memory"])

    def test_the_control_plane_needs_no_storage(self) -> None:
        q = _tenant_quota(_req(worker_count=1, control_plane_replicas=3))
        assert int(q["storage"]) == 20 * 1024 ** 3

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
    async def test_writing_it_twice_is_not_an_error(self) -> None:
        from unittest.mock import AsyncMock

        from kubernetes_asyncio.client.rest import ApiException

        from app.api.v1.tenants_crud import _write_tenant_quota

        k8s = MagicMock()
        k8s.core_api.create_namespaced_resource_quota = AsyncMock(
            side_effect=ApiException(status=409),
        )
        await _write_tenant_quota(k8s, "tenant-tci", {
            "cpu": "2", "memory": "1", "storage": "1",
        })
