"""The K8S VERSION column was empty for every tenant, always.

It was read from `Cluster.status.version`, which CAPI leaves unset on this
path — verified on the lab, where `kubectl get cluster -n tenant-t1 t1
-o jsonpath='{.status.version}'` returns nothing while the control plane has
been running v1.30.1 for hours. The version lives on
`KamajiControlPlane.spec.version`.

Deliberately not the Machines' kubelet version: during an upgrade that is a
different number, and the column is about the control plane.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from app.api.v1.tenants_crud import _enrich_with_control_plane
from app.models.tenant import TenantResponse


def _tenant(version: str = "") -> TenantResponse:
    return TenantResponse(
        name="t1", display_name="Tenant One", namespace="tenant-t1",
        kubernetes_version=version, status="Ready",
    )


def _k8s(kcp: dict | None) -> MagicMock:
    async def get_obj(**kw):
        if kcp is None:
            raise ApiException(status=404)
        return kcp

    k8s = MagicMock()
    k8s.custom_api.get_namespaced_custom_object = AsyncMock(side_effect=get_obj)
    return k8s


@pytest.mark.asyncio
async def test_the_version_comes_from_the_control_plane() -> None:
    k8s = _k8s({"spec": {"version": "v1.30.1", "replicas": 2}, "status": {"readyReplicas": 2}})

    out = await _enrich_with_control_plane(k8s, _tenant())

    assert out.kubernetes_version == "v1.30.1"


@pytest.mark.asyncio
async def test_a_version_the_cluster_did_report_is_not_overwritten() -> None:
    """If CAPI ever fills status.version in, it is the more authoritative one."""
    k8s = _k8s({"spec": {"version": "v1.30.1"}, "status": {}})

    out = await _enrich_with_control_plane(k8s, _tenant("v1.31.0"))

    assert out.kubernetes_version == "v1.31.0"


@pytest.mark.asyncio
async def test_a_missing_control_plane_leaves_it_empty() -> None:
    out = await _enrich_with_control_plane(_k8s(None), _tenant())

    assert out.kubernetes_version == ""
