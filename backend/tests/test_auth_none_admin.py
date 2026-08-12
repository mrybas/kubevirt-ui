"""Unit tests for admin resolution, including the auth-disabled case.

With AUTH_TYPE=none the backend invents an anonymous user in
"kubevirt-ui-admins". Checking that invented group against ADMIN_GROUPS meant
a deployment pointing ADMIN_GROUPS at its own group got 403 on every write
while believing auth was off — including the first folder, which blocks
tenant creation entirely.
"""

import pytest

from app.core import auth
from app.core.groups import is_admin


@pytest.fixture
def auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "AUTH_TYPE", "oidc")


@pytest.fixture
def auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "AUTH_TYPE", "none")


def _admin_groups(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Point settings.admin_groups_list at `names` (ADMIN_GROUPS env var)."""
    from app import config

    class _Settings:
        admin_groups_list = list(names)

    monkeypatch.setattr(config, "get_settings", lambda: _Settings())


class TestAuthDisabled:
    def test_anonymous_user_is_admin(
        self, auth_disabled: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The deployment repointed ADMIN_GROUPS at its LDAP group, which the
        # synthetic user is not in — this is the case that used to 403.
        _admin_groups(monkeypatch, "some-ldap-group")
        assert is_admin(["kubevirt-ui-admins"]) is True

    def test_holds_even_with_no_groups_at_all(
        self, auth_disabled: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _admin_groups(monkeypatch, "some-ldap-group")
        assert is_admin([]) is True


class TestAuthEnabled:
    def test_matching_group_is_admin(
        self, auth_enabled: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _admin_groups(monkeypatch, "kubevirt-ui-admins")
        assert is_admin(["kubevirt-ui-admins"]) is True

    def test_non_matching_group_is_not_admin(
        self, auth_enabled: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _admin_groups(monkeypatch, "kubevirt-ui-admins")
        assert is_admin(["developers"]) is False

    def test_no_groups_is_not_admin(
        self, auth_enabled: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _admin_groups(monkeypatch, "kubevirt-ui-admins")
        assert is_admin([]) is False

    def test_system_masters_is_admin(
        self, auth_enabled: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _admin_groups(monkeypatch, "kubevirt-ui-admins")
        assert is_admin(["system:masters"]) is True

    def test_custom_admin_group_is_honored(
        self, auth_enabled: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _admin_groups(monkeypatch, "some-ldap-group")
        assert is_admin(["some-ldap-group"]) is True
        assert is_admin(["kubevirt-ui-admins"]) is False
