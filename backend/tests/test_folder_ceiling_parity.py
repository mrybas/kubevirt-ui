"""One statement of the folder ceiling, asserted by both implementations.

The backend refuses a tenant that does not fit its folder before writing
anything. The operator writes the same quota from the same description and,
until this table existed, never asked — so `spec.workers.count` edited on the
CR grew the charge with nothing in the way. Both ask now, which is only an
improvement while both answer the same: a tenant refused by the API and
admitted by its reconciler leaves the CR and the cluster disagreeing about
something neither of them will report.

Neither side generates the file. If either arithmetic moves, its own suite
goes red here, before a cluster sees it.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1.folders import assert_within_folder_quota

_MOUNTED = Path("/test/parity/folder-ceiling.json")
_IN_REPO = Path(__file__).resolve().parents[2] / "test" / "parity" / "folder-ceiling.json"
TABLE = _MOUNTED if _MOUNTED.exists() else _IN_REPO


def _cases():
    doc = json.loads(TABLE.read_text())
    assert doc["cases"], "the parity table is empty, so this test proves nothing"
    return [pytest.param(c, id=c["name"]) for c in doc["cases"]]


def _k8s(namespaces: list[dict]):
    """A cluster holding exactly what the table says it holds.

    The label selector is honoured rather than ignored: `_own_env_quota` asks
    for one folder's namespaces at a time, and a fake that answers with all of
    them would make every sub-folder case pass for the wrong reason.
    """
    k8s = MagicMock()

    async def list_namespaces(label_selector=None):
        folder = (label_selector or "").split("=", 1)[-1]
        return [
            {"name": ns["namespace"]}
            for ns in namespaces
            if ns["folder"] == folder
        ]

    async def list_quotas(namespace):
        items = []
        for ns in namespaces:
            if ns["namespace"] != namespace:
                continue
            hard = {}
            for key, field in (
                ("requests.cpu", "cpu"),
                ("requests.memory", "memory"),
                ("requests.storage", "storage"),
            ):
                if ns.get(field):
                    hard[key] = ns[field]
            items.append(SimpleNamespace(
                metadata=SimpleNamespace(name=f"{namespace}-quota"),
                spec=SimpleNamespace(hard=hard),
            ))
        return SimpleNamespace(items=items)

    k8s.list_namespaces = AsyncMock(side_effect=list_namespaces)
    k8s.core_api.list_namespaced_resource_quota = AsyncMock(side_effect=list_quotas)
    return k8s


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _cases())
async def test_the_ceiling_agrees_with_the_operator(case) -> None:
    folders = {
        name: {"parent_id": f.get("parent"), "quota": f.get("quota") or None}
        for name, f in case["folders"].items()
    }
    want = case["want"]

    async def ask():
        await assert_within_folder_quota(
            _k8s(case["namespaces"]), folders, case["folder"],
            want.get("cpu"), want.get("memory"), want.get("storage"),
            exclude_namespace=case["asking"], asking="this tenant",
        )

    if case["expected"]["refused"]:
        with pytest.raises(HTTPException) as e:
            await ask()
        assert e.value.status_code == 409
        assert case["expected"]["dimension"] in e.value.detail
        assert "is free and" in e.value.detail
    else:
        await ask()
