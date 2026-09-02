"""The Harbor client must carry the caller's identity, never its own.

Browsing is authorised per user by Harbor. If the client could hold a service
credential, every caller would see everything and kubevirt-ui would have to
filter — a second, weaker copy of a decision Harbor already makes correctly.
"""

import httpx
import pytest

from app.core.harbor_client import (
    HARBOR_PAGE_SIZE,
    HarborClient,
    HarborNotFound,
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


async def test_one_repositorys_artifacts_are_read_from_the_repository_path():
    """The path `assert_tag_is_free` reads one repository's tags from.

    This test used to carry the claim that the project-wide artifact listing
    "returns 401 for scoped identities", and that claim was wrong. The 401 it
    rested on was measured with a ROBOT account, and robots are refused the
    ENTIRE management API at every level regardless of permissions — so it
    said nothing about that endpoint. Catalogue enumeration now uses the
    project-wide listing (see `list_project_artifacts`); this per-repository
    path remains for the tag check, which asks about exactly one repository.
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=[{"tags": [{"name": "20260901"}]}])

    await _client(handler).list_artifacts("t", "vm-images-public", "ubuntu-2204")

    assert seen["path"] == (
        "/api/v2.0/projects/vm-images-public/repositories/ubuntu-2204/artifacts"
    )


async def test_a_not_found_response_is_still_one_of_the_designed_exceptions():
    """404 (and any other undesigned 4xx) must not escape as httpx.HTTPStatusError.

    HarborUnauthorized/HarborUnavailable are meant to be exhaustive: a caller
    catching only those two must never see a bare httpx exception leak through.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"code": "NOT_FOUND"}]})

    with pytest.raises((HarborUnauthorized, HarborUnavailable)):
        await _client(handler).list_projects("fine-token")


async def test_verify_identity_hits_the_auth_gated_probe():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"username": "someone"})

    await _client(handler).verify_identity("user-token-abc")

    assert seen["path"] == "/api/v2.0/users/current"
    assert seen["auth"] == "Bearer user-token-abc"


async def test_verify_identity_accepts_a_valid_user():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"username": "someone"})

    # Must not raise.
    await _client(handler).verify_identity("fine-token")


async def test_verify_identity_accepts_a_recognised_robot_account():
    """412 from /users/current: a real identity, just not a user account.

    Robots do not browse (see the module docstring), but recognising one here
    is still not the same thing as rejecting an unknown bearer — 412 must not
    be folded into the 401/403 branch.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, json={"errors": [{"code": "PRECONDITION"}]})

    await _client(handler).verify_identity("robot-token")


async def test_verify_identity_rejects_an_anonymous_or_invalid_bearer():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errors": [{"code": "UNAUTHORIZED"}]})

    with pytest.raises(HarborUnauthorized):
        await _client(handler).verify_identity("not-a-real-token")


async def test_verify_identity_reports_a_403_the_same_as_a_401():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"errors": [{"code": "FORBIDDEN"}]})

    with pytest.raises(HarborUnauthorized):
        await _client(handler).verify_identity("forbidden-token")


async def test_verify_identity_reports_a_5xx_as_an_outage_not_a_rejection():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(HarborUnavailable):
        await _client(handler).verify_identity("fine-token")


async def test_verify_identity_reports_a_transport_failure_as_an_outage():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(HarborUnavailable):
        await _client(handler).verify_identity("fine-token")


async def test_a_repository_name_with_a_slash_is_a_single_path_segment():
    """Harbor repository names are frequently multi-segment (team/subimage).

    An unencoded slash would silently address a different, possibly valid,
    resource. It must reach Harbor as one encoded path segment instead.
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # request.url.path returns the *unquoted* path (httpx decodes %2F back
        # to "/" there), which would hide exactly the bug this test guards
        # against. raw_path preserves the wire encoding, so it is the only
        # place the fix is actually observable.
        seen["raw_path"] = request.url.raw_path
        return httpx.Response(200, json=[{"tags": [{"name": "20260901"}]}])

    await _client(handler).list_artifacts("t", "vm-images-public", "team/subimage")

    assert seen["raw_path"] == (
        b"/api/v2.0/projects/vm-images-public/repositories/team%2Fsubimage/artifacts"
        b"?page=1&page_size=100"
    )


# ---------------------------------------------------------------------------
# Pagination.
#
# Harbor returns one page unless asked otherwise, and reading only the first
# is a correctness bug, not a performance one: `assert_tag_is_free` walks
# this list to decide whether a tag is taken, so a tag on page two is
# reported free and gets published over — producing an image nobody can boot,
# which is the exact failure the immutable-tag rule exists to prevent.
# ---------------------------------------------------------------------------


def _page(n: int, size: int = HARBOR_PAGE_SIZE) -> list[dict]:
    return [{"name": f"repo-{n}-{i}"} for i in range(size)]


async def test_every_page_is_followed_when_harbor_advertises_a_next_link():
    pages = {
        1: (_page(1), '</api/v2.0/projects?page=2&page_size=100>; rel="next"'),
        2: (_page(2), '</api/v2.0/projects?page=3&page_size=100>; rel="next"'),
        3: ([{"name": "last-one"}], ""),
    }
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        seen.append(request.url.params.get("page"))
        body, link = pages[page]
        headers = {"Link": link} if link else {}
        return httpx.Response(200, json=body, headers=headers)

    rows = await _client(handler).list_projects("fine-token")

    assert seen == ["1", "2", "3"]
    assert len(rows) == 2 * HARBOR_PAGE_SIZE + 1
    assert rows[-1] == {"name": "last-one"}


async def test_a_link_header_without_a_next_relation_ends_the_walk():
    """Harbor sends Link on the last page too, with only rel="prev"."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("page"))
        return httpx.Response(
            200,
            json=_page(1),
            headers={"Link": '</api/v2.0/projects?page=1>; rel="prev"'},
        )

    rows = await _client(handler).list_projects("fine-token")

    assert seen == ["1"]
    assert len(rows) == HARBOR_PAGE_SIZE


async def test_x_total_count_carries_the_walk_when_no_link_header_is_sent():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        seen.append(request.url.params.get("page"))
        body = _page(page) if page == 1 else [{"name": "tail"}]
        return httpx.Response(
            200, json=body, headers={"X-Total-Count": str(HARBOR_PAGE_SIZE + 1)}
        )

    rows = await _client(handler).list_projects("fine-token")

    assert seen == ["1", "2"]
    assert len(rows) == HARBOR_PAGE_SIZE + 1


async def test_a_short_page_with_no_headers_at_all_ends_the_walk():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("page"))
        return httpx.Response(200, json=[{"name": "only-one"}])

    rows = await _client(handler).list_projects("fine-token")

    assert seen == ["1"]
    assert rows == [{"name": "only-one"}]


async def test_artifacts_are_paginated_too_so_an_occupied_tag_cannot_hide():
    """The list assert_tag_is_free reads is the one that must be complete."""
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(
                200,
                json=[{"tags": [{"name": f"t{i}"}]} for i in range(HARBOR_PAGE_SIZE)],
                headers={"Link": '</next>; rel="next"'},
            )
        return httpx.Response(200, json=[{"tags": [{"name": "20260902"}]}])

    artifacts = await _client(handler).list_artifacts("t", "p", "r")

    tags = [tag["name"] for a in artifacts for tag in a["tags"]]
    assert "20260902" in tags


async def test_a_repository_that_does_not_exist_yet_is_its_own_exception():
    """404 is how Harbor answers "nothing has ever been pushed here".

    Still a HarborUnavailable subclass, so every caller that degrades on the
    two designed exceptions is unaffected — but distinguishable, so the first
    publish to a brand-new repository is not read as an outage.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"code": "NOT_FOUND"}]})

    with pytest.raises(HarborNotFound):
        await _client(handler).list_artifacts("t", "p", "brand-new")

    assert issubclass(HarborNotFound, HarborUnavailable)


async def test_the_project_wide_listing_never_sends_latest_in_repository():
    """Harbor's documentation offers `latest_in_repository=true` as the way to
    get one current artifact per repository. On this Harbor build it does not
    work at all, measured against real pushed artifacts in the lab:

        ?latest_in_repository=true      -> HTTP 400, "either 'media_type' or
          'artifact_type' must be specified, but not both, when querying with
          latest_in_repository"
        + the companion filter via `q=` -> HTTP 500 for the brace and fuzzy
          forms; 200 with ZERO results for the bare form, on artifacts that
          unambiguously match the values queried

    No syntax was found that returns a correct non-empty result. Sent anyway,
    EVERY catalogue read is a 400 — which this client turns into
    HarborUnavailable, so the Images page shows an empty catalogue with
    `catalog_available: false`. A hard production failure, not a missing
    optimisation.

    This test asserts the absence deliberately, so the parameter cannot be
    re-added from the documentation without something failing. Dropping it
    costs no requests: the `1 + P` saving comes from using the project-wide
    endpoint instead of the per-repository walk, and this parameter only ever
    reduced rows.
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json=[{"repository_name": "p/ubuntu", "tags": [{"name": "1"}]}],
        )

    await _client(handler).list_project_artifacts("t", "vm-images-public")

    assert seen["path"] == "/api/v2.0/projects/vm-images-public/artifacts"
    assert "latest_in_repository" not in seen["params"]
    # Nor smuggled in through the `q=` companion filter the 400 asks for —
    # that form returns 500 or an empty result set.
    assert "q" not in seen["params"]
    # Pagination is unchanged and still the point of this call.
    assert seen["params"]["page"] == "1"
    assert seen["params"]["page_size"] == "100"
