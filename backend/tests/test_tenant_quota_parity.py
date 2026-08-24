"""One table, asserted by both implementations of the tenant reservation.

Neither side generates it. While this backend still computes tenant quotas —
and it does, for every tenant the product creates — two arithmetics decide the
same number, and the interesting failure is not either being wrong but the two
disagreeing: a tenant adopted from one and reconciled by the other would have
its quota rewritten on the first pass, silently, in a direction nobody chose.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Mounted at /test in the test container; the repo path is the fallback for
# anyone running pytest outside it.
_MOUNTED = Path("/test/parity/tenant-quota.json")
_IN_REPO = Path(__file__).resolve().parents[2] / "test" / "parity" / "tenant-quota.json"
TABLE = _MOUNTED if _MOUNTED.exists() else _IN_REPO


def _cases():
    doc = json.loads(TABLE.read_text())
    assert doc["cases"], "the parity table is empty, so this test proves nothing"
    return [pytest.param(c, id=c["name"]) for c in doc["cases"]]


@pytest.mark.parametrize("case", _cases())
def test_the_reservation_agrees_with_the_operator(case) -> None:
    from app.api.v1.tenants_crud import _tenant_quota

    req = MagicMock()
    req.worker_count = case["workers"]
    req.worker_vcpu = case["vcpu"]
    req.worker_memory = f"{case['memoryGi']}Gi"
    req.worker_disk = f"{case['diskGi']}Gi"
    req.control_plane_replicas = case["controlPlaneReplicas"]
    req.worker_os = "talos" if case["talos"] else "cloud-init"
    req.talos_version = None
    req.kubernetes_version = "v1.33.1"
    req.storage_class = None

    quota = _tenant_quota(req)
    expected = case["expected"]

    assert int(float(quota["cpu"]) * 1000) == expected["cpuMilli"]
    assert int(quota["memory"]) == expected["memoryBytes"]
    assert int(quota["storage"]) == expected["storageBytes"]
