"""The Harbor client must carry the caller's identity, never its own.

Browsing is authorised per user by Harbor. If the client could hold a service
credential, every caller would see everything and kubevirt-ui would have to
filter — a second, weaker copy of a decision Harbor already makes correctly.
"""

import httpx
import pytest

from app.core.harbor_client import (
    HarborClient,
    HarborUnauthorized,
    HarborUnavailable,
)


def _client(handler) -> HarborClient:
    c = HarborClient(base_url="https://harbor.example")
    c._transport = httpx.MockTransport(handler)
    return c


async def test_the_caller_token_is_what_reaches_harbor():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=[{"name": "vm-images-public"}])

    await _client(handler).list_projects("user-token-abc")

    assert seen["auth"] == "Bearer user-token-abc"


async def test_a_rejected_token_is_not_reported_as_an_outage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errors": [{"code": "UNAUTHORIZED"}]})

    with pytest.raises(HarborUnauthorized):
        await _client(handler).list_projects("expired-token")


async def test_an_unreachable_harbor_is_not_reported_as_a_rejection():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(HarborUnavailable):
        await _client(handler).list_projects("fine-token")


async def test_repositories_are_read_from_the_project_scoped_path():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=[{"name": "vm-images-public/ubuntu-2204"}])

    await _client(handler).list_repositories("t", "vm-images-public")

    assert seen["path"] == "/api/v2.0/projects/vm-images-public/repositories"


async def test_artifacts_are_read_per_repository_not_per_project():
    """The project-wide artifact listing returns 401 for scoped identities.

    Browsing goes project -> repositories -> artifacts-of-one-repository, so
    the client must not reach for /projects/{p}/artifacts.
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=[{"tags": [{"name": "20260901"}]}])

    await _client(handler).list_artifacts("t", "vm-images-public", "ubuntu-2204")

    assert seen["path"] == (
        "/api/v2.0/projects/vm-images-public/repositories/ubuntu-2204/artifacts"
    )
