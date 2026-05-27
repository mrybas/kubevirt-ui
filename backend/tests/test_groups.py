"""Phase 2 — role helper short-circuit matrix.

Covers `is_folder_admin/member/viewer` and `is_env_admin/member/viewer`
in `app.core.groups`.  Validates:

* Global admins bypass all checks.
* Folder roles flow downward (admin → member → viewer).
* Env-level access is the *union* of folder-level + per-env entries.
* Env-only access is scoped to the specific env (not cross-env).
* Legacy folders (no `access` block) only allow global admins.
* `None`/missing role lists are treated as empty (no crash).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.core.groups import (
    is_env_admin,
    is_env_member,
    is_env_viewer,
    is_folder_admin,
    is_folder_member,
    is_folder_viewer,
)


@dataclass
class _StubUser:
    groups: list[str]


# A fully populated folder used in most cases.
FOLDER_FULL = {
    "_name": "team-dev",
    "access": {
        "admins":  ["platform-team"],
        "members": ["team-dev-devs"],
        "viewers": ["audit-readers"],
        "env_access": {
            "prod": {
                "admins":  ["team-prod-leads"],
                "members": ["team-prod-deployers"],
                "viewers": [],
            },
        },
    },
}

# Legacy folder — no `access` block at all (Phase 1 behaviour).
FOLDER_LEGACY: dict = {"_name": "legacy", "display_name": "Legacy"}

# Malformed: explicit None for access (defensive — load from broken cm).
FOLDER_NULL_ACCESS = {"_name": "nullacc", "access": None}

# Partial: only `admins` set, env_access missing.
FOLDER_PARTIAL = {"_name": "partial", "access": {"admins": ["g1"]}}


# ---------------------------------------------------------------------------
# Global admin override
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("folder", [FOLDER_FULL, FOLDER_LEGACY, FOLDER_NULL_ACCESS, FOLDER_PARTIAL])
def test_global_admin_passes_every_check(folder):
    u = _StubUser(groups=["kubevirt-ui-admins"])
    assert is_folder_admin(u, folder)
    assert is_folder_member(u, folder)
    assert is_folder_viewer(u, folder)
    assert is_env_admin(u, folder, "prod")
    assert is_env_member(u, folder, "prod")
    assert is_env_viewer(u, folder, "prod")
    # Even on an env that doesn't exist in env_access, admin still passes.
    assert is_env_admin(u, folder, "nonexistent")


# ---------------------------------------------------------------------------
# Folder-level role hierarchy
# ---------------------------------------------------------------------------

def test_folder_admin_is_also_member_and_viewer():
    u = _StubUser(groups=["platform-team"])
    assert is_folder_admin(u, FOLDER_FULL)
    assert is_folder_member(u, FOLDER_FULL)
    assert is_folder_viewer(u, FOLDER_FULL)


def test_folder_member_is_not_admin_but_is_viewer():
    u = _StubUser(groups=["team-dev-devs"])
    assert not is_folder_admin(u, FOLDER_FULL)
    assert is_folder_member(u, FOLDER_FULL)
    assert is_folder_viewer(u, FOLDER_FULL)


def test_folder_viewer_is_not_admin_or_member():
    u = _StubUser(groups=["audit-readers"])
    assert not is_folder_admin(u, FOLDER_FULL)
    assert not is_folder_member(u, FOLDER_FULL)
    assert is_folder_viewer(u, FOLDER_FULL)


def test_outsider_has_no_folder_access():
    u = _StubUser(groups=["random-group"])
    assert not is_folder_admin(u, FOLDER_FULL)
    assert not is_folder_member(u, FOLDER_FULL)
    assert not is_folder_viewer(u, FOLDER_FULL)


# ---------------------------------------------------------------------------
# Env-level union semantics
# ---------------------------------------------------------------------------

def test_env_admin_via_folder_role():
    u = _StubUser(groups=["platform-team"])  # folder admin
    assert is_env_admin(u, FOLDER_FULL, "prod")
    assert is_env_admin(u, FOLDER_FULL, "dev")  # folder admin → any env


def test_env_admin_via_env_specific_access():
    u = _StubUser(groups=["team-prod-leads"])
    assert is_env_admin(u, FOLDER_FULL, "prod")
    # Cross-env: not admin on dev (no entry for dev in env_access).
    assert not is_env_admin(u, FOLDER_FULL, "dev")


def test_env_member_union_of_folder_and_env():
    # folder member
    u_fm = _StubUser(groups=["team-dev-devs"])
    assert is_env_member(u_fm, FOLDER_FULL, "prod")
    assert is_env_member(u_fm, FOLDER_FULL, "dev")

    # env-only member
    u_em = _StubUser(groups=["team-prod-deployers"])
    assert is_env_member(u_em, FOLDER_FULL, "prod")
    assert not is_env_member(u_em, FOLDER_FULL, "dev")

    # env-only admin → also env_member (admin → member)
    u_ea = _StubUser(groups=["team-prod-leads"])
    assert is_env_member(u_ea, FOLDER_FULL, "prod")


def test_env_viewer_union_of_folder_and_env():
    u_fv = _StubUser(groups=["audit-readers"])
    assert is_env_viewer(u_fv, FOLDER_FULL, "prod")
    assert is_env_viewer(u_fv, FOLDER_FULL, "dev")

    # env-only member also satisfies env_viewer (member → viewer)
    u_em = _StubUser(groups=["team-prod-deployers"])
    assert is_env_viewer(u_em, FOLDER_FULL, "prod")
    assert not is_env_viewer(u_em, FOLDER_FULL, "dev")


def test_env_only_admin_is_not_viewer_on_other_env():
    """An env admin on prod should NOT inherit anything on dev."""
    u = _StubUser(groups=["team-prod-leads"])
    assert not is_env_viewer(u, FOLDER_FULL, "dev")


# ---------------------------------------------------------------------------
# Legacy / malformed folders
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("folder", [FOLDER_LEGACY, FOLDER_NULL_ACCESS])
def test_legacy_folder_blocks_everyone_but_global_admin(folder):
    u = _StubUser(groups=["platform-team", "team-dev-devs", "audit-readers"])
    assert not is_folder_admin(u, folder)
    assert not is_folder_member(u, folder)
    assert not is_folder_viewer(u, folder)
    assert not is_env_admin(u, folder, "prod")
    assert not is_env_member(u, folder, "prod")
    assert not is_env_viewer(u, folder, "prod")


def test_partial_access_only_admin_set():
    u = _StubUser(groups=["g1"])
    assert is_folder_admin(u, FOLDER_PARTIAL)
    # Admin implies member + viewer.
    assert is_folder_member(u, FOLDER_PARTIAL)
    assert is_folder_viewer(u, FOLDER_PARTIAL)
    # Env without env_access entry — admin still passes (folder-level).
    assert is_env_admin(u, FOLDER_PARTIAL, "anyenv")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_user_with_no_groups_denied_everywhere():
    u = _StubUser(groups=[])
    assert not is_folder_admin(u, FOLDER_FULL)
    assert not is_env_member(u, FOLDER_FULL, "prod")


def test_user_with_multiple_groups_takes_highest():
    """A user in both folder-viewer and env-admin groups gets env-admin."""
    u = _StubUser(groups=["audit-readers", "team-prod-leads"])
    assert is_env_admin(u, FOLDER_FULL, "prod")
    # On a different env, only the folder-viewer carries through.
    assert not is_env_admin(u, FOLDER_FULL, "dev")
    assert not is_env_member(u, FOLDER_FULL, "dev")
    assert is_env_viewer(u, FOLDER_FULL, "dev")


def test_env_access_with_missing_keys_treated_as_empty():
    """env_access[env] is a dict with no admins/members/viewers keys."""
    folder = {
        "_name": "f",
        "access": {
            "admins": [],
            "env_access": {"prod": {}},  # empty per-env block
        },
    }
    u = _StubUser(groups=["any"])
    assert not is_env_admin(u, folder, "prod")
    assert not is_env_member(u, folder, "prod")
    assert not is_env_viewer(u, folder, "prod")
