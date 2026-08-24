"""429 from the API server reaches the client as 429.

UAT run 4, in passing:

    app.api.v1.disks - ERROR - Failed to list snapshots: (429)
    GET …/disks/…/snapshots → 500 Internal Server Error

and the next request succeeded. Flattened into a 500 it reads as "this is
broken": a client has no reason to retry a 500 and every reason to retry a
429. The disks module was raising hand-rolled 500s from every ApiException
rather than going through the mapper, so it could not tell the difference.
"""

import pytest
from kubernetes_asyncio.client.rest import ApiException

from app.core.errors import k8s_error_to_http


@pytest.mark.parametrize(
    "status_code,expected",
    [
        (429, 429),   # slow down, ask again
        (503, 503),   # the control plane is briefly away, ask again
        (404, 404),
        (403, 403),
        (409, 409),
        (500, 500),   # theirs is genuinely ours to report as ours
        (418, 500),   # anything unmapped stays an internal error
    ],
)
def test_the_status_survives(status_code: int, expected: int) -> None:
    assert k8s_error_to_http(
        ApiException(status=status_code), "listing snapshots",
    ).status_code == expected


def test_a_retriable_answer_says_to_retry() -> None:
    detail = k8s_error_to_http(ApiException(status=429), "listing").detail
    assert "try again" in detail


def test_the_disks_module_goes_through_the_mapper() -> None:
    """It had ten hand-rolled 500s, which is ten places that cannot tell a
    busy cluster from a broken one."""
    from pathlib import Path

    source = Path("app/api/v1/disks.py").read_text()
    assert "HTTP_500_INTERNAL_SERVER_ERROR" not in source
    assert source.count("k8s_error_to_http") >= 10
