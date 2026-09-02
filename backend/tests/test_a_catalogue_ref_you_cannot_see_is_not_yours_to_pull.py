"""Two identities meet here, and nothing used to join them.

The catalogue is READ as the caller — their own dex token, forwarded to
Harbor, which answers per user — and the image is PULLED as the namespace's
robot, whose read covers the whole registry. Materialise accepted any
`catalog_ref` that matched the pattern, so the second identity could fetch
what the first was never shown:

    a user whose token cannot list project `finance` — so it never appears in
    their `GET /images` — posts `catalog_ref: "finance/db-golden:1"`, and the
    private disk lands in their namespace. Nothing about the request is
    malformed; the regex was the only thing standing there.

So the caller's own token asks Harbor for the artifact before anything is
written. Harbor decides; this code does no filtering of its own.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.images_catalog import assert_catalogue_ref_visible
from app.core.harbor_client import HarborNotFound, HarborUnauthorized, HarborUnavailable


def _harbor(artifacts: list[dict[str, Any]] | Exception) -> MagicMock:
    harbor = MagicMock()
    if isinstance(artifacts, Exception):
        harbor.list_artifacts = AsyncMock(side_effect=artifacts)
    else:
        harbor.list_artifacts = AsyncMock(return_value=artifacts)
    return harbor


VISIBLE = [{"tags": [{"name": "1.2.3"}, {"name": "latest"}]}]


@pytest.mark.asyncio
class TestTheCallerMustBeAbleToSeeIt:
    async def test_a_visible_tag_passes(self) -> None:
        harbor = _harbor(VISIBLE)
        await assert_catalogue_ref_visible(harbor, "caller-token", "team/img:1.2.3")

    async def test_it_asks_with_the_callers_token_not_the_robots(self) -> None:
        """The whole point: the robot can see more than the caller, so asking
        as the robot would answer the wrong question."""
        harbor = _harbor(VISIBLE)
        await assert_catalogue_ref_visible(harbor, "caller-token", "team/img:1.2.3")
        token, project, repository = harbor.list_artifacts.call_args.args
        assert token == "caller-token"
        assert (project, repository) == ("team", "img")

    async def test_a_tag_that_is_not_there_is_refused(self) -> None:
        with pytest.raises(HarborNotFound):
            await assert_catalogue_ref_visible(_harbor(VISIBLE), "t", "team/img:9.9.9")

    async def test_a_project_the_caller_cannot_see_is_refused(self) -> None:
        """Harbor answers 404 for a project outside the caller's view, and that
        arrives here as HarborNotFound — which must not be read as success."""
        harbor = _harbor(HarborNotFound("404"))
        with pytest.raises(HarborNotFound):
            await assert_catalogue_ref_visible(harbor, "t", "finance/db-golden:1")

    async def test_a_rejected_identity_is_not_a_pass(self) -> None:
        harbor = _harbor(HarborUnauthorized("401"))
        with pytest.raises(HarborUnauthorized):
            await assert_catalogue_ref_visible(harbor, "t", "team/img:1.2.3")

    async def test_harbor_being_down_is_not_a_pass_either(self) -> None:
        """Never a pull on a guess. An unreachable Harbor cannot say yes, and
        the caller gets a 503 rather than someone else's image."""
        harbor = _harbor(HarborUnavailable("boom"))
        with pytest.raises(HarborUnavailable):
            await assert_catalogue_ref_visible(harbor, "t", "team/img:1.2.3")

    async def test_an_empty_catalogue_is_refused(self) -> None:
        with pytest.raises(HarborNotFound):
            await assert_catalogue_ref_visible(_harbor([]), "t", "team/img:1.2.3")

    async def test_a_repository_with_a_slash_keeps_its_path(self) -> None:
        """Harbor repositories nest: `project/team/sub/img:tag`. Splitting on
        the wrong separator asks about a repository that does not exist and
        refuses something the caller can genuinely see."""
        harbor = _harbor(VISIBLE)
        await assert_catalogue_ref_visible(harbor, "t", "team/sub/img:1.2.3")
        _, project, repository = harbor.list_artifacts.call_args.args
        assert (project, repository) == ("team", "sub/img")

    async def test_an_artifact_with_no_tags_does_not_crash(self) -> None:
        """Harbor returns `tags: null` for an untagged artifact."""
        with pytest.raises(HarborNotFound):
            await assert_catalogue_ref_visible(
                _harbor([{"tags": None}]), "t", "team/img:1.2.3",
            )
