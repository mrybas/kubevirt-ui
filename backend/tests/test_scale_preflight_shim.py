"""Scaling any tenant returned 500, and no test saw it.

`_tenant_quota` is called two ways: with a real `TenantCreateRequest` at
create time, and with a hand-built `SimpleNamespace` in the scale preflight.
Every unit test used the first. T3 taught the function to read `worker_os` —
and the shim did not have it, so every scale became

    AttributeError: 'SimpleNamespace' object has no attribute 'worker_os'

which the API returned as 500 and the page swallowed without a word. Found by
scaling a tenant through the UI during the T22 acceptance, not by the suite.

The general guard is the first test here: it builds the shim the way the
endpoint does and asserts it satisfies whatever the quota function reads
*today*, so the next field added is caught by the same test rather than by
another afternoon.
"""

from types import SimpleNamespace

import pytest

from app.api.v1.tenants_crud import _tenant_quota


class TestTheShimSatisfiesTheFunctionItFeeds:
    def test_every_attribute_the_quota_reads_is_present(self) -> None:
        """Derived from the source rather than from a list kept in step by
        hand — a list is exactly the second record this keeps failing on."""
        import inspect
        import re

        src = inspect.getsource(_tenant_quota)
        wanted = set(re.findall(r"\breq\.(\w+)", src))

        shim = SimpleNamespace(
            worker_count=2, worker_vcpu=2, worker_memory="2Gi",
            worker_disk="20Gi", control_plane_replicas=1, worker_os="talos",
        )

        missing = [a for a in wanted if not hasattr(shim, a)]
        assert not missing, f"the scale preflight shim is missing {missing}"

    def test_the_shim_actually_computes(self) -> None:
        q = _tenant_quota(SimpleNamespace(
            worker_count=2, worker_vcpu=2, worker_memory="2Gi",
            worker_disk="20Gi", control_plane_replicas=1, worker_os="talos",
        ))

        assert int(q["storage"]) > 0 and q["cpu"]

    def test_the_endpoint_passes_worker_os(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/tenants_crud.py").read_text()
        block = src[src.index("quota = _tenant_quota(SimpleNamespace("):]
        block = block[:block.index("))")]

        assert "worker_os=" in block


class TestTheShapeIsReadFromTheDatapath:
    @pytest.mark.asyncio
    async def test_a_data_volume_root_means_talos(self) -> None:
        """Not an annotation somebody has to keep current: the root volume is
        what the tenant actually boots."""
        shape = await _shape_for(root={"name": "root", "dataVolume": {"name": "root"}})

        assert shape["worker_os"] == "talos"

    @pytest.mark.asyncio
    async def test_a_container_disk_root_means_cloud_init(self) -> None:
        shape = await _shape_for(
            root={"name": "root", "containerDisk": {"image": "quay.io/x:1"}})

        assert shape["worker_os"] == "cloud-init"

    @pytest.mark.asyncio
    async def test_worker_disk_is_the_data_disk_not_the_root_clone(self) -> None:
        """It used to be read from the DataVolume template, which is the root.
        With the quota now counting the root separately that charged it
        twice — and quietly, as a number nobody recomputes by hand."""
        shape = await _shape_for(
            root={"name": "root", "dataVolume": {"name": "root"}},
            data_capacity="5Gi", dv_size="100Gi",
        )

        assert shape["disk"] == "5Gi"


async def _shape_for(*, root: dict, data_capacity: str = "20Gi",
                     dv_size: str = "20Gi") -> dict:
    from unittest.mock import AsyncMock, MagicMock

    from app.api.v1.tenants_crud import _current_worker_shape

    md = {"spec": {"template": {"spec": {"infrastructureRef": {"name": "w"}}}}}
    vm_spec = {
        "dataVolumeTemplates": [
            {"spec": {"storage": {"resources": {"requests": {"storage": dv_size}}}}},
        ],
        "template": {"spec": {
            "domain": {"cpu": {"cores": 4}, "memory": {"guest": "8Gi"}},
            "volumes": [
                root,
                {"name": "data", "emptyDisk": {"capacity": data_capacity}},
            ],
        }},
    }
    tpl = {"spec": {"template": {"spec": {"virtualMachineTemplate": {
        "spec": vm_spec,
    }}}}}

    async def get_obj(**kw):
        if kw["plural"] == "machinedeployments":
            return md
        if kw["plural"] == "kubevirtmachinetemplates":
            return tpl
        return {"spec": {"replicas": 1}}

    k8s = MagicMock()
    k8s.custom_api.get_namespaced_custom_object = AsyncMock(side_effect=get_obj)
    return await _current_worker_shape(k8s, "tenant-t", "t-workers")
