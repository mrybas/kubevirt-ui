"""Metrics answer only for the caller's namespaces.

`/metrics/query` and `/metrics/query_range` passed any PromQL to the metrics
backend, so every logged-in user could read every series it holds: other
tenants' VMs, nodes, and everything else scraped into it. Raw PromQL is
admin-only now; the UI asks for named metrics and the backend adds the
namespace selector.
"""

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import metrics
from app.core.auth import User

MINE = "tenant-mine"
RANGE = "start=0&end=3600"


@pytest.fixture
def fake_user() -> User:
    return User(id="u", email="u@example.test", username="u", groups=["team-mine"])


@pytest.fixture
def queries() -> Iterator[list[dict[str, str]]]:
    """Every query the backend sends, with the caller bound in MINE only."""
    sent: list[dict[str, str]] = []

    async def _query(_k8s: Any, _path: str, params: dict[str, str]) -> dict:
        sent.append(params)
        return {"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"name": "vm1", metrics.NAMESPACE_LABEL: MINE}, "value": [0, "1"]},
        ]}}

    metrics._cache.clear()
    with (
        patch.object(metrics, "_proxy_query", side_effect=_query),
        patch.object(metrics, "get_user_namespaces", AsyncMock(return_value=[MINE])),
        patch("app.api.v1.storage.get_user_namespaces", AsyncMock(return_value=[MINE])),
    ):
        yield sent


class TestRawPromQL:
    @pytest.mark.parametrize("path", [
        "/api/v1/metrics/query?query=up",
        f"/api/v1/metrics/query_range?query=up&{RANGE}",
        f"/api/v1/metrics/cluster?metric=cpu&{RANGE}",
    ])
    def test_is_for_admins_only(self, client: TestClient, queries: list, path: str) -> None:
        assert client.get(path).status_code == 403
        assert queries == []

    def test_an_admin_still_has_it(self, client: TestClient, queries: list, fake_user: User) -> None:
        fake_user.groups = ["kubevirt-ui-admins"]
        assert client.get("/api/v1/metrics/query?query=up").status_code == 200
        assert queries == [{"query": "up"}]

    def test_status_says_whether_cluster_metrics_are_yours(self, client: TestClient) -> None:
        with patch.object(metrics, "_discover_metrics_backend", AsyncMock(
            return_value=metrics.MetricsBackend(),
        )):
            assert client.get("/api/v1/metrics/status").json()["cluster"] is False


class TestOneVM:
    def test_your_vm_is_queried_with_its_namespace(self, client: TestClient, queries: list) -> None:
        r = client.get(f"/api/v1/metrics/vms/{MINE}/vm1?metric=cpu&{RANGE}")
        assert r.status_code == 200
        assert f'name="vm1", {metrics.NAMESPACE_LABEL}="{MINE}"' in queries[0]["query"]

    def test_a_foreign_namespace_is_refused(self, client: TestClient, queries: list) -> None:
        r = client.get(f"/api/v1/metrics/vms/tenant-theirs/vm1?metric=cpu&{RANGE}")
        assert r.status_code == 404
        assert queries == []

    @pytest.mark.parametrize("name", ['vm1"} or up{x="', "VM1", "vm1}"])
    def test_a_name_cannot_carry_promql(self, client: TestClient, queries: list, name: str) -> None:
        r = client.get(f"/api/v1/metrics/vms/{MINE}/{name}?metric=cpu&{RANGE}")
        assert r.status_code in (404, 422)
        assert queries == []

    def test_only_known_metrics(self, client: TestClient, queries: list) -> None:
        r = client.get(f"/api/v1/metrics/vms/{MINE}/vm1?metric=up&{RANGE}")
        assert r.status_code == 422
        assert queries == []


class TestTopVMs:
    def test_only_your_namespaces_are_ranked(self, client: TestClient, queries: list) -> None:
        r = client.get("/api/v1/metrics/top?metric=cpu")
        assert r.status_code == 200
        assert f'{metrics.NAMESPACE_LABEL}=~"{MINE}"' in queries[0]["query"]

    def test_series_carry_the_namespace_as_namespace(self, client: TestClient, queries: list) -> None:
        r = client.get("/api/v1/metrics/top?metric=memory")
        assert r.json()["data"]["result"][0]["metric"]["namespace"] == MINE

    def test_no_namespaces_means_no_query(self, client: TestClient, queries: list) -> None:
        with patch.object(metrics, "get_user_namespaces", AsyncMock(return_value=[])):
            r = client.get("/api/v1/metrics/top?metric=cpu")
        assert r.json()["data"]["result"] == []
        assert queries == []
