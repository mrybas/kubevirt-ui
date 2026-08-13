"""Worker readiness has to follow the node, not the Machine.

Measured on the cluster: a worker VM deleted out of band was recreated by CAPK
from its original bootstrap secret, whose join token had expired, so the new
VM never rejoined. For ten minutes CAPI reported

    Ready=True  BootstrapReady=True  InfrastructureReady=True
    NodeHealthy=Unknown  "Node condition Ready is Unknown"

and `KubevirtMachine ready=true, VMProvisioned=True`, while the tenant's own
`kubectl get nodes` showed:

    t-cloudinit-workers-knrkw-p2rkm   NotReady   10.16.0.168   (stale address)

`MachineDeployment.status.readyReplicas` counts the Ready condition, so the UI
would have said 2/2 for a cluster with one dead worker.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client import ApiException

from app.api.v1.tenants_crud import _healthy_worker_count


def _machine(node_healthy: str | None, ready: str = "True") -> dict:
    conds = [{"type": "Ready", "status": ready}]
    if node_healthy is not None:
        conds.append({"type": "NodeHealthy", "status": node_healthy})
    return {"status": {"conditions": conds}}


def _k8s(machines: list[dict] | None, fail: bool = False) -> MagicMock:
    k8s = MagicMock()
    if fail:
        k8s.custom_api.list_namespaced_custom_object = AsyncMock(
            side_effect=ApiException(status=403, reason="Forbidden"),
        )
    else:
        k8s.custom_api.list_namespaced_custom_object = AsyncMock(
            return_value={"items": machines or []},
        )
    return k8s


@pytest.mark.asyncio
async def test_a_ready_machine_with_an_unhealthy_node_does_not_count() -> None:
    # The exact shape observed: Ready=True, NodeHealthy=Unknown.
    k8s = _k8s([_machine("True"), _machine("Unknown")])
    assert await _healthy_worker_count(k8s, "tenant-x", fallback=2) == 1


@pytest.mark.asyncio
async def test_all_healthy_counts_all() -> None:
    k8s = _k8s([_machine("True"), _machine("True")])
    assert await _healthy_worker_count(k8s, "tenant-x", fallback=2) == 2


@pytest.mark.asyncio
async def test_an_explicitly_false_node_does_not_count() -> None:
    k8s = _k8s([_machine("True"), _machine("False")])
    assert await _healthy_worker_count(k8s, "tenant-x", fallback=2) == 1


@pytest.mark.asyncio
async def test_a_machine_without_the_condition_does_not_count() -> None:
    # Before the node is registered at all there is no NodeHealthy yet, and
    # "not yet joined" is not "ready".
    k8s = _k8s([_machine(None)])
    assert await _healthy_worker_count(k8s, "tenant-x", fallback=1) == 0


@pytest.mark.asyncio
async def test_unreadable_machines_fall_back_rather_than_report_an_outage() -> None:
    # A zero here would render as "0/2 workers" on a healthy cluster.
    k8s = _k8s(None, fail=True)
    assert await _healthy_worker_count(k8s, "tenant-x", fallback=2) == 2


@pytest.mark.asyncio
async def test_no_machines_at_all_falls_back_too() -> None:
    k8s = _k8s([])
    assert await _healthy_worker_count(k8s, "tenant-x", fallback=3) == 3


@pytest.mark.asyncio
async def test_the_tenant_response_uses_the_healthy_count_not_readyreplicas() -> None:
    """The helper only helps if `_enrich_with_workers` calls it.

    This is the assertion that fails on the shipped behaviour: a
    MachineDeployment reporting readyReplicas=2 over one healthy and one
    unhealthy node must surface as 1, not 2.
    """
    from app.api.v1.tenants_crud import _enrich_with_workers
    from app.models.tenant import TenantResponse

    k8s = MagicMock()

    async def _list(**kw):
        if kw.get("plural") == "machinedeployments":
            return {"items": [{
                "spec": {"replicas": 2, "template": {"spec": {"infrastructureRef": {}}}},
                "status": {"readyReplicas": 2},
            }]}
        if kw.get("plural") == "machines":
            return {"items": [_machine("True"), _machine("Unknown")]}
        return {"items": []}

    k8s.custom_api.list_namespaced_custom_object = AsyncMock(side_effect=_list)

    tenant = TenantResponse(
        name="t", display_name="t", namespace="tenant-t",
        kubernetes_version="v1.32.1", status="Ready",
    )

    out = await _enrich_with_workers(k8s, tenant)

    assert out.worker_count == 2
    assert out.workers_ready == 1, (
        "workers_ready followed MachineDeployment.readyReplicas, which stays "
        "at 2 while one node is NotReady"
    )
