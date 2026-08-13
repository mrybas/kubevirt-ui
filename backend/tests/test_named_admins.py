"""Admins can be named individually, not only by group.

Groups are the right mechanism when the identity provider emits them. Not
every one does: Dex's local password database emits none at all, so under a
group-only rule a deployment using it cannot produce an admin — the entire
admin half of the UI (Network, Security, Cluster, Tenants) is unreachable for
everybody, with no configuration that fixes it.
"""

import pytest

from app.core.auth import User
from app.core.groups import is_admin


def _user(email: str = "", username: str = "", *groups: str) -> User:
    return User(id="u", email=email, username=username, groups=list(groups))


@pytest.fixture
def named(monkeypatch):
    def _set(value: str) -> None:
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("ADMIN_USERS", value)

    yield _set

    from app.config import get_settings

    get_settings.cache_clear()


class TestGroupsStillWork:
    def test_a_group_member_is_an_admin(self) -> None:
        assert is_admin(["kubevirt-ui-admins"])

    def test_an_outsider_is_not(self) -> None:
        assert not is_admin(["some-team"], _user(email="x@lab.local"))


class TestNamedAdmins:
    def test_by_email(self, named) -> None:
        named("e2e-admin@lab.local")

        assert is_admin([], _user(email="e2e-admin@lab.local"))

    def test_by_username(self, named) -> None:
        named("e2e-admin")

        assert is_admin([], _user(username="e2e-admin"))

    def test_case_is_ignored(self, named) -> None:
        named("E2E-Admin@Lab.Local")

        assert is_admin([], _user(email="e2e-admin@lab.local"))

    def test_someone_else_is_still_refused(self, named) -> None:
        named("e2e-admin@lab.local")

        assert not is_admin([], _user(email="e2e-user@lab.local"))

    def test_an_empty_setting_grants_nobody(self, named) -> None:
        named("")

        # The dangerous shape: empty string must not match an empty email.
        assert not is_admin([], _user(email="", username=""))

    def test_a_user_without_the_argument_falls_back_to_groups(self, named) -> None:
        named("e2e-admin@lab.local")

        assert not is_admin([])
