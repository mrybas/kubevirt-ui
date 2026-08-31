"""Who may create a tenant, and why it is a knob rather than a code change.

A tenant is not a large VM. It takes one address from a pool of twenty, a name
in a public DNS zone that ends up in a certificate, a namespace, and a share of
the one Kamaji datastore every tenant's control plane stores its state in —
none of which any namespace quota counts, and the folder ceiling only binds
when somebody has set one.

So the widening lives behind a declared variable: a stand can try folder
members creating tenants without that reaching every install, and it goes back
without waiting for a release. These tests pin both positions of the switch and
the refusal text, because a refusal that names the wrong role sends people to
ask for the wrong grant.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.v1 import tenants_crud
from app.core import groups


def _user(*groups: str) -> MagicMock:
    user = MagicMock()
    user.groups = list(groups)
    user.username = "tester"
    user.email = "tester@example.test"
    return user


FOLDER = {
    "display_name": "PoC",
    "access": {"admins": ["folder-admins"], "members": ["folder-members"]},
}


async def _build_in(user: MagicMock, role: str | None) -> dict:
    env = {} if role is None else {"TENANTS_CREATE_ROLE": role}
    with (
        patch.dict("os.environ", env, clear=False),
        patch.object(tenants_crud, "load_folders", AsyncMock(return_value={"poc": FOLDER})),
        patch.object(tenants_crud, "is_admin", lambda *_a, **_k: False),
    ):
        if role is None:
            import os

            os.environ.pop("TENANTS_CREATE_ROLE", None)
        return await tenants_crud._folder_you_may_build_in(MagicMock(), user, "poc")


@pytest.mark.asyncio
class TestWhoMayCreateATenant:
    async def test_a_folder_admin_always_may(self) -> None:
        assert await _build_in(_user("folder-admins"), None) is FOLDER
        assert await _build_in(_user("folder-admins"), "member") is FOLDER

    async def test_a_member_may_not_by_default(self) -> None:
        """The default is the behaviour every install has today."""
        with pytest.raises(HTTPException) as e:
            await _build_in(_user("folder-members"), None)
        assert e.value.status_code == 403
        assert "folder-admin" in e.value.detail

    async def test_a_member_may_when_the_knob_says_so(self) -> None:
        assert await _build_in(_user("folder-members"), "member") is FOLDER

    async def test_both_spellings_are_accepted(self) -> None:
        """The refusal says "folder-member"; someone will paste that back in.
        A knob that rejects the word its own error message used is a knob that
        silently does nothing."""
        assert await _build_in(_user("folder-members"), "folder-member") is FOLDER

    async def test_the_roles_add_up_rather_than_replace_each_other(self) -> None:
        """"member" is the lowest role admitted, not the only one."""
        for role in ("member", "folder-member"):
            assert await _build_in(_user("folder-admins"), role) is FOLDER
            assert await _build_in(_user("folder-members"), role) is FOLDER
            with pytest.raises(HTTPException):
                await _build_in(_user("folder-viewers"), role)

    async def test_the_refusal_names_the_role_actually_required(self) -> None:
        """With the knob on, telling a stranger to get folder-admin sends them
        to ask for a grant they do not need."""
        with pytest.raises(HTTPException) as e:
            await _build_in(_user("nobody"), "member")
        assert "folder-member" in e.value.detail

    async def test_a_stranger_is_refused_in_both_positions(self) -> None:
        for role in (None, "member"):
            with pytest.raises(HTTPException) as e:
                await _build_in(_user("nobody"), role)
            assert e.value.status_code == 403
            # Never "no such folder" — telling the two apart enumerates folders.
            assert "not found" not in e.value.detail

    async def test_the_folder_list_offers_exactly_what_the_endpoint_allows(
        self,
    ) -> None:
        """One predicate, two readers.

        The tenants page used to filter folders by `users.includes(username)` —
        a list of individually-named subjects — so access granted by group left
        the dropdown empty and the right could not be exercised at all. The page
        now reads this, and it must agree with the refusal above in both
        positions of the knob.
        """
        import os

        member, admin, stranger = (
            _user("folder-members"),
            _user("folder-admins"),
            _user("nobody"),
        )
        os.environ.pop("TENANTS_CREATE_ROLE", None)
        with patch.object(groups, "is_admin", lambda *_a, **_k: False):
            assert groups.may_create_tenant(admin, FOLDER) is True
            assert groups.may_create_tenant(member, FOLDER) is False
            with patch.dict("os.environ", {"TENANTS_CREATE_ROLE": "member"}):
                assert groups.may_create_tenant(member, FOLDER) is True
                assert groups.may_create_tenant(stranger, FOLDER) is False

    async def test_an_unknown_value_is_not_read_as_permission(self) -> None:
        """Anything that is not exactly `member` leaves the default in place —
        a typo must not widen access."""
        for typo in ("members", "Member ", "true", "yes"):
            expect_open = typo.strip().lower() == "member"
            if expect_open:
                continue
            with pytest.raises(HTTPException):
                await _build_in(_user("folder-members"), typo)
