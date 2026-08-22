"""`POST /tenants` decides who you are before it answers anything else.

UAT run 4, A-1. Under a viewer's token:

    POST /vms     → 403   (guard at the door)
    POST /vpcs    → 403   (guard at the door)
    POST /tenants → 404 "Folder 'no-such-folder-xyz' not found"
    POST /tenants → 400 "Environment 'no-such-env-xyz' does not exist in
                         folder 'poc-transit'. Known envs: ['dev', 'prod']"

The tenant endpoint took `require_auth` at the door and checked folder-admin
three lookups later, and those lookups answer questions: which folders exist,
and the full list of environments inside one. A viewer read the contents of a
folder they may not write to out of the error text of a call they are not
allowed to make.

The tester was careful to say what they had not proven — that a viewer could
actually create a tenant — and they were right not to: the check existed. It
was simply last, and one check that is last is one refactor away from being
absent.

So authorisation is the first thing the handler does, and "not yours" and
"does not exist" are one answer, because telling them apart is how a list of
folder names is read out one guess at a time.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

FOLDERS = {
    "poc-transit": {"access": {"admins": ["kv-poc-transit-admins"]}},
    "other": {"access": {"admins": ["kv-other-admins"]}},
}


def _user(groups: list[str], admin: bool = False):
    return SimpleNamespace(
        email="kv-viewer@ipa.test", username="kv-viewer",
        groups=groups + (["kubevirt-ui-admins"] if admin else []),
        is_admin=admin,
    )


async def _ask(user, folder: str):
    from app.api.v1 import tenants_crud

    with patch.object(tenants_crud, "load_folders", AsyncMock(return_value=FOLDERS)):
        return await tenants_crud._folder_you_may_build_in(MagicMock(), user, folder)


@pytest.mark.asyncio
class TestTheDoor:
    async def test_a_viewer_is_refused(self) -> None:
        with pytest.raises(HTTPException) as e:
            await _ask(_user(["kv-poc-transit-viewers"]), "poc-transit")
        assert e.value.status_code == 403

    async def test_a_folder_admin_gets_in(self) -> None:
        meta = await _ask(_user(["kv-poc-transit-admins"]), "poc-transit")
        assert meta is FOLDERS["poc-transit"]

    async def test_a_folder_admin_of_another_folder_is_refused(self) -> None:
        with pytest.raises(HTTPException) as e:
            await _ask(_user(["kv-other-admins"]), "poc-transit")
        assert e.value.status_code == 403

    async def test_a_platform_admin_gets_in(self) -> None:
        meta = await _ask(_user([], admin=True), "poc-transit")
        assert meta is FOLDERS["poc-transit"]


@pytest.mark.asyncio
class TestWhatTheRefusalGivesAway:
    async def test_a_folder_that_does_not_exist_answers_like_one_that_is_not_yours(
        self,
    ) -> None:
        """Otherwise the pair of answers is a directory of folder names."""
        user = _user(["kv-poc-transit-viewers"])
        with pytest.raises(HTTPException) as missing:
            await _ask(user, "no-such-folder-xyz")
        with pytest.raises(HTTPException) as forbidden:
            await _ask(user, "poc-transit")
        assert missing.value.status_code == forbidden.value.status_code == 403
        assert missing.value.detail.replace("no-such-folder-xyz", "poc-transit") == (
            forbidden.value.detail
        )

    async def test_the_refusal_says_what_it_would_take(self) -> None:
        with pytest.raises(HTTPException) as e:
            await _ask(_user(["kv-poc-transit-viewers"]), "poc-transit")
        assert "folder-admin" in e.value.detail

    async def test_an_admin_still_gets_a_plain_404_for_a_typo(self) -> None:
        """Nothing to hide from someone who can list every folder anyway."""
        with pytest.raises(HTTPException) as e:
            await _ask(_user([], admin=True), "no-such-folder-xyz")
        assert e.value.status_code == 404


def test_no_environment_is_named_before_the_caller_is_checked() -> None:
    """The leak was the order, so the order is what this holds.

    `Known envs: [...]` is a useful message for someone allowed to create a
    tenant. It is a disclosure for anyone else, and the only thing that makes
    it one is arriving before the authorisation.
    """
    import inspect

    from app.api.v1.tenants_crud import create_tenant

    # Code only: the comment above the guard quotes the leaked message, and a
    # first version of this test found the quote and called it the defect.
    source = "\n".join(
        line for line in inspect.getsource(create_tenant).splitlines()
        if not line.lstrip().startswith("#")
    )
    assert source.index("_folder_you_may_build_in") < source.index("Known envs")
    assert source.index("_folder_you_may_build_in") < source.index("list_namespace")
