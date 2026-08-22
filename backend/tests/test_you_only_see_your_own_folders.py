"""`GET /folders` returns folders you have something to do with.

It filtered nothing. Every authenticated user got every folder — the name,
the display name, who is in it, how many VMs it holds, how much storage — and
the UI hid the ones that were not theirs by convention. UAT run 4 saw the
other folder from two different roles and recorded it twice before working
out that it was the endpoint and not the role.

Same root as the Create Folder button that answers 403 to the user it is
shown to: the page was drawing what it could see rather than what it could
use, because what it could see was everything.

Three ways in, matching how access is granted everywhere else — the folder's
access block, one of its environments' access blocks, or RBAC in one of its
namespaces, which is the only way a folder made before access blocks existed
can be reached at all.
"""

from types import SimpleNamespace

import pytest

from app.api.v1.folders import _folders_you_may_see

FOLDERS = {
    "poc-transit": {
        "display_name": "PoC Transit",
        "access": {
            "admins": ["kv-poc-transit-admins"],
            "members": [],
            "viewers": ["kv-poc-transit-viewers"],
            "env_access": {"dev": {"admins": ["kv-poc-transit-dev-admins"]}},
        },
    },
    "other": {"display_name": "Other", "access": {"admins": ["kv-other-admins"]}},
    "legacy": {"display_name": "Legacy"},  # no access block at all
}


def _ns(name: str, folder: str, env: str):
    return SimpleNamespace(metadata=SimpleNamespace(
        name=name, labels={
            "kubevirt-ui.io/folder": folder,
            "kubevirt-ui.io/environment": env,
        },
    ))


NS_BY_FOLDER = {
    "poc-transit": [_ns("poc-transit-dev", "poc-transit", "dev"),
                    _ns("poc-transit-prod", "poc-transit", "prod")],
    "other": [_ns("other-dev", "other", "dev")],
    "legacy": [_ns("legacy-dev", "legacy", "dev")],
}


def _user(groups: list[str]):
    return SimpleNamespace(
        email="someone@ipa.test", username="someone", groups=groups, is_admin=False,
    )


def _seen(groups: list[str], user_ns: set[str] = frozenset()) -> set[str]:
    return set(_folders_you_may_see(
        _user(groups), FOLDERS, set(user_ns), NS_BY_FOLDER,
    ))


class TestWhatYouCanSee:
    def test_a_folder_viewer_sees_their_folder_and_no_other(self) -> None:
        assert _seen(["kv-poc-transit-viewers"]) == {"poc-transit"}

    def test_an_env_admin_sees_the_folder_that_env_is_in(self) -> None:
        """The role the run used, which saw both folders."""
        assert _seen(["kv-poc-transit-dev-admins"]) == {"poc-transit"}

    def test_a_stranger_sees_nothing(self) -> None:
        assert _seen(["some-other-team"]) == set()

    def test_namespace_rbac_still_reaches_a_folder_with_no_access_block(self) -> None:
        """The legacy path: no access block, a RoleBinding in its namespace."""
        assert _seen(["nobody"], {"legacy-dev"}) == {"legacy"}

    def test_two_folders_when_two_are_yours(self) -> None:
        assert _seen(["kv-poc-transit-viewers", "kv-other-admins"]) == {
            "poc-transit", "other",
        }


class TestTheTreeStillHangsTogether:
    def test_an_ancestor_comes_along_so_the_child_can_be_drawn(self) -> None:
        folders = {
            "root": {"display_name": "Root"},
            "kid": {"display_name": "Kid", "parent_id": "root",
                    "access": {"viewers": ["kv-kid-viewers"]}},
        }
        seen = _folders_you_may_see(
            _user(["kv-kid-viewers"]), folders, set(),
            {"kid": [_ns("kid-dev", "kid", "dev")]},
        )
        assert set(seen) == {"kid", "root"}

    def test_a_cycle_in_parent_ids_does_not_hang(self) -> None:
        """Nothing stops two folders naming each other in a ConfigMap."""
        folders = {
            "a": {"parent_id": "b", "access": {"viewers": ["kv-a"]}},
            "b": {"parent_id": "a"},
        }
        seen = _folders_you_may_see(_user(["kv-a"]), folders, set(), {})
        assert set(seen) == {"a", "b"}
