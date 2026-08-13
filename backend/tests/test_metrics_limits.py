"""The PromQL proxy is a cost surface, not a read-only view.

Authentication keeps anonymous callers out; these bounds keep an authenticated
one from taking VictoriaMetrics — and with it the platform's monitoring — down
with a single query spanning the whole retention window.
"""

import pytest
from fastapi import HTTPException

from app.api.v1.metrics import (
    MAX_QUERY_CHARS,
    MAX_RANGE_SECONDS,
    _reject_oversized_query,
    _reject_oversized_range,
)


class TestQuerySize:
    def test_a_normal_dashboard_query_passes(self) -> None:
        _reject_oversized_query('sum(rate(container_cpu_usage_seconds_total[5m])) by (pod)')

    def test_an_absurd_query_is_refused(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _reject_oversized_query("x" * (MAX_QUERY_CHARS + 1))

        assert exc.value.status_code == 413

    def test_the_boundary_itself_is_allowed(self) -> None:
        _reject_oversized_query("x" * MAX_QUERY_CHARS)


class TestRangeWidth:
    def test_a_day_is_fine(self) -> None:
        _reject_oversized_range(0, 24 * 3600)

    def test_a_year_is_refused(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _reject_oversized_range(0, 365 * 24 * 3600)

        assert exc.value.status_code == 413

    def test_a_backwards_range_is_refused_before_it_reaches_the_backend(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _reject_oversized_range(1000, 500)

        assert exc.value.status_code == 422

    def test_the_boundary_itself_is_allowed(self) -> None:
        _reject_oversized_range(0, MAX_RANGE_SECONDS)


def test_both_query_endpoints_apply_the_guards() -> None:
    import inspect

    from app.api.v1 import metrics

    instant = inspect.getsource(metrics.metrics_query)
    ranged = inspect.getsource(metrics.metrics_query_range)

    assert "_reject_oversized_query(query)" in instant
    assert "_reject_oversized_query(query)" in ranged
    assert "_reject_oversized_range(start, end)" in ranged
