"""Phase 2 — folder access PATCH endpoint + RoleBindings reconciler.

Covers:

* `PATCH /api/v1/folders/{name}/access` partial-replace semantics
  (omit-keeps, []-clears, env_access full-replace).
* `reconcile_folder_rbac` materialises 3 RBs per env with the UNION of
  folder-level + per-env subjects.
* Empty subjects → RB delete-if-exists.
* Idempotency — re-running the reconcile with the same input is safe.
* RoleRef immutability handled by delete-and-create when changed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.v1 import folders as folders_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ns(name: str, env: str, folder: str = "team-dev") -> MagicMock:
    """Build a mock namespace object with the expected labels."""
    ns = MagicMock()
    ns.metadata.name = name
    ns.metadata.labels = {
        folders_mod.ENV_FOLDER_LABEL: folder,
        folders_mod.ENV_ENVIRONMENT_LABEL: env,
        folders_mod.ENV_MANAGED_LABEL: "true",
    }
    return ns


def _api_exception(status: int):
    """Create a fake ApiException-compatible object."""
    from kubernetes_asyncio.client.rest import ApiException
    return ApiException(status=status, reason="test")


def _stub_k8s_with_folder(folder_name: str, folder_data: dict, ns_items: list[MagicMock]):
    """Build a MagicMock k8s_client wired for `reconcile_folder_rbac`.

    Returns (k8s_client, rbac_api) — the rbac_api is also reachable via
    the module's `_get_rbac_api` after we patch it.
    """
    k8s = MagicMock()
    # core_api: list_namespace returns ns_items wrapped in .items
    ns_list_resp = MagicMock()
    ns_list_resp.items = ns_items
    k8s.core_api = MagicMock()
    k8s.core_api.list_namespace = AsyncMock(return_value=ns_list_resp)

    cm = MagicMock()
    cm.data = {folder_name: json.dumps(folder_data)}
    cm.metadata = MagicMock()
    k8s.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)
    k8s.core_api.patch_namespaced_config_map = AsyncMock()
    k8s.core_api.replace_namespaced_config_map = AsyncMock()

    rbac = MagicMock()
    rbac.create_namespaced_role_binding = AsyncMock()
    rbac.delete_namespaced_role_binding = AsyncMock()
    rbac.read_namespaced_role_binding = AsyncMock()
    rbac.replace_namespaced_role_binding = AsyncMock()

    return k8s, rbac


# ---------------------------------------------------------------------------
# _collect_subjects — union semantics
# ---------------------------------------------------------------------------

class TestCollectSubjects:
    def test_returns_sorted_dedup_union(self):
        folder = {
            "access": {
                "admins": ["b", "a"],
                "env_access": {"prod": {"admins": ["c", "a"]}},
            },
        }
        assert folders_mod._collect_subjects(folder, "prod", "admin") == ["a", "b", "c"]

    def test_returns_empty_when_no_lists(self):
        assert folders_mod._collect_subjects({}, "prod", "admin") == []

    def test_env_without_entry_falls_back_to_folder_only(self):
        folder = {"access": {"members": ["g1"]}}
        assert folders_mod._collect_subjects(folder, "dev", "member") == ["g1"]

    def test_filters_empty_strings(self):
        folder = {"access": {"viewers": ["", "g1", ""]}}
        assert folders_mod._collect_subjects(folder, "prod", "viewer") == ["g1"]


# ---------------------------------------------------------------------------
# reconcile_folder_rbac — basic flows
# ---------------------------------------------------------------------------

class TestReconcileFolderRbac:
    @pytest.mark.asyncio
    async def test_creates_three_rbs_per_env(self, monkeypatch):
        folder_meta = {
            "access": {
                "admins":  ["g-admin"],
                "members": ["g-member"],
                "viewers": ["g-viewer"],
                "env_access": {},
            },
        }
        ns_items = [_ns("team-dev-prod", "prod"), _ns("team-dev-dev", "dev")]
        k8s, rbac = _stub_k8s_with_folder("team-dev", folder_meta, ns_items)

        async def _fake_get_rbac(_):
            return rbac

        monkeypatch.setattr(folders_mod, "_get_rbac_api", _fake_get_rbac)

        await folders_mod.reconcile_folder_rbac(k8s, "team-dev", folder_meta)
        # 3 roles × 2 envs = 6 create calls
        assert rbac.create_namespaced_role_binding.await_count == 6

        # Verify subjects + labels on at least one call.
        first_call = rbac.create_namespaced_role_binding.await_args_list[0]
        body = first_call.kwargs.get("body") or first_call.args[1]
        labels = body["metadata"]["labels"]
        assert labels[folders_mod.ACCESS_SOURCE_LABEL] == folders_mod.ACCESS_SOURCE_PHASE2
        assert labels[folders_mod.ACCESS_FOLDER_LABEL] == "team-dev"
        assert all(s["kind"] == "Group" for s in body["subjects"])

    @pytest.mark.asyncio
    async def test_env_access_unions_with_folder_subjects(self, monkeypatch):
        folder_meta = {
            "access": {
                "admins": ["folder-admin"],
                "env_access": {
                    "prod": {"admins": ["prod-only-admin"]},
                },
            },
        }
        ns_items = [_ns("team-dev-prod", "prod"), _ns("team-dev-dev", "dev")]
        k8s, rbac = _stub_k8s_with_folder("team-dev", folder_meta, ns_items)

        async def _fake_get_rbac(_):
            return rbac

        monkeypatch.setattr(folders_mod, "_get_rbac_api", _fake_get_rbac)
        await folders_mod.reconcile_folder_rbac(k8s, "team-dev", folder_meta)

        # Find the prod admins RB call.
        prod_admin_call = None
        for call in rbac.create_namespaced_role_binding.await_args_list:
            body = call.kwargs.get("body") or call.args[1]
            if (
                body["metadata"]["name"] == folders_mod.RB_NAME_ADMIN
                and body["metadata"]["namespace"] == "team-dev-prod"
            ):
                prod_admin_call = body
                break

        assert prod_admin_call is not None
        names = sorted(s["name"] for s in prod_admin_call["subjects"])
        assert names == ["folder-admin", "prod-only-admin"]

        # Dev admins RB should only have folder-level admin (no env_access).
        dev_admin_call = None
        for call in rbac.create_namespaced_role_binding.await_args_list:
            body = call.kwargs.get("body") or call.args[1]
            if (
                body["metadata"]["name"] == folders_mod.RB_NAME_ADMIN
                and body["metadata"]["namespace"] == "team-dev-dev"
            ):
                dev_admin_call = body
                break

        assert dev_admin_call is not None
        assert [s["name"] for s in dev_admin_call["subjects"]] == ["folder-admin"]


# ---------------------------------------------------------------------------
# Empty subjects → delete-if-exists
# ---------------------------------------------------------------------------

class TestEmptySubjectsDelete:
    @pytest.mark.asyncio
    async def test_empty_subjects_delete_rb_when_present(self, monkeypatch):
        # No groups at all — every RB should be deleted (404s ignored).
        folder_meta = {"access": {}}
        ns_items = [_ns("team-dev-prod", "prod")]
        k8s, rbac = _stub_k8s_with_folder("team-dev", folder_meta, ns_items)

        async def _fake_get_rbac(_):
            return rbac

        monkeypatch.setattr(folders_mod, "_get_rbac_api", _fake_get_rbac)
        await folders_mod.reconcile_folder_rbac(k8s, "team-dev", folder_meta)

        # 3 roles × 1 env = 3 delete attempts, no creates.
        assert rbac.delete_namespaced_role_binding.await_count == 3
        assert rbac.create_namespaced_role_binding.await_count == 0

    @pytest.mark.asyncio
    async def test_empty_subjects_404_swallowed(self, monkeypatch):
        folder_meta = {"access": {}}
        ns_items = [_ns("team-dev-prod", "prod")]
        k8s, rbac = _stub_k8s_with_folder("team-dev", folder_meta, ns_items)
        # All deletes return 404 — should not raise.
        rbac.delete_namespaced_role_binding = AsyncMock(side_effect=_api_exception(404))

        async def _fake_get_rbac(_):
            return rbac

        monkeypatch.setattr(folders_mod, "_get_rbac_api", _fake_get_rbac)
        # Should not raise.
        await folders_mod.reconcile_folder_rbac(k8s, "team-dev", folder_meta)


# ---------------------------------------------------------------------------
# Idempotency — 409 on create triggers replace
# ---------------------------------------------------------------------------

class TestIdempotency:
    @pytest.mark.asyncio
    async def test_409_triggers_replace_when_roleref_matches(self, monkeypatch):
        folder_meta = {"access": {"admins": ["g1"]}}
        ns_items = [_ns("team-dev-prod", "prod")]
        k8s, rbac = _stub_k8s_with_folder("team-dev", folder_meta, ns_items)
        rbac.create_namespaced_role_binding = AsyncMock(side_effect=_api_exception(409))

        existing = MagicMock()
        existing.metadata.resource_version = "42"
        existing.role_ref.name = "kubevirt-ui-admin"  # matches
        rbac.read_namespaced_role_binding = AsyncMock(return_value=existing)
        rbac.replace_namespaced_role_binding = AsyncMock()

        async def _fake_get_rbac(_):
            return rbac

        monkeypatch.setattr(folders_mod, "_get_rbac_api", _fake_get_rbac)
        await folders_mod.reconcile_folder_rbac(k8s, "team-dev", folder_meta)

        # Replace was called for the admin RB.
        assert rbac.replace_namespaced_role_binding.await_count >= 1
        call = rbac.replace_namespaced_role_binding.await_args_list[0]
        body = call.kwargs.get("body") or call.args[2]
        # ResourceVersion preserved for optimistic concurrency.
        assert body["metadata"]["resourceVersion"] == "42"

    @pytest.mark.asyncio
    async def test_roleref_immutability_falls_back_to_delete_and_create(self, monkeypatch):
        """If the existing RB has a different roleRef, replace would fail;
        we must delete the RB and re-create it."""
        folder_meta = {"access": {"admins": ["g1"]}}
        ns_items = [_ns("team-dev-prod", "prod")]
        k8s, rbac = _stub_k8s_with_folder("team-dev", folder_meta, ns_items)

        # First create call → 409 (already exists).
        # Second create call (after delete) → succeeds.
        rbac.create_namespaced_role_binding = AsyncMock(
            side_effect=[_api_exception(409), None, None, None, None, None, None]
        )
        existing = MagicMock()
        existing.metadata.resource_version = "42"
        existing.role_ref.name = "old-role-name"  # MISMATCH — triggers delete+create
        rbac.read_namespaced_role_binding = AsyncMock(return_value=existing)
        rbac.delete_namespaced_role_binding = AsyncMock()

        async def _fake_get_rbac(_):
            return rbac

        monkeypatch.setattr(folders_mod, "_get_rbac_api", _fake_get_rbac)
        await folders_mod.reconcile_folder_rbac(k8s, "team-dev", folder_meta)

        # The conflicting RB was deleted (at least once) and a new create attempted.
        assert rbac.delete_namespaced_role_binding.await_count >= 1
        assert rbac.replace_namespaced_role_binding.await_count == 0


# ---------------------------------------------------------------------------
# PATCH /folders/{name}/access — partial-replace endpoint
# ---------------------------------------------------------------------------

class TestPatchFolderAccessEndpoint:
    """Endpoint-level tests using TestClient + conftest fixtures.

    The fixture `client` overrides `require_folder_admin()` to a no-op
    that returns `fake_user`, so we can hit PATCH directly.  The
    underlying k8s mocks live in `mock_k8s_client` from conftest.
    """

    def _set_up_folder(self, mock_k8s_client, folder_name="team-dev", access=None):
        meta = {"display_name": folder_name, "parent_id": None}
        if access is not None:
            meta["access"] = access
        cm = MagicMock()
        cm.data = {folder_name: json.dumps(meta)}
        cm.metadata = MagicMock()
        mock_k8s_client.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)
        mock_k8s_client.core_api.patch_namespaced_config_map = AsyncMock()
        # Reconciler call path — list_namespace returns no envs (no RB churn).
        ns_list_resp = MagicMock()
        ns_list_resp.items = []
        mock_k8s_client.core_api.list_namespace = AsyncMock(return_value=ns_list_resp)
        mock_k8s_client._api_client = MagicMock()

    def test_patch_replaces_only_listed_fields(self, client, mock_k8s_client):
        self._set_up_folder(
            mock_k8s_client,
            access={
                "admins":  ["a-existing"],
                "members": ["m-existing"],
                "viewers": ["v-existing"],
                "env_access": {"prod": {"admins": ["prod-existing"]}},
            },
        )

        # Patch ONLY admins → other fields must survive.
        resp = client.patch(
            "/api/v1/folders/team-dev/access",
            json={"admins": ["a-new"]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["admins"] == ["a-new"]
        assert body["members"] == ["m-existing"]
        assert body["viewers"] == ["v-existing"]
        assert body["env_access"]["prod"]["admins"] == ["prod-existing"]

    def test_patch_with_empty_list_clears_field(self, client, mock_k8s_client):
        self._set_up_folder(
            mock_k8s_client,
            access={
                "admins":  ["a"],
                "members": ["m"],
                "viewers": ["v"],
            },
        )
        resp = client.patch(
            "/api/v1/folders/team-dev/access",
            json={"members": []},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["members"] == []
        # Other fields untouched.
        assert body["admins"] == ["a"]

    def test_patch_env_access_full_replace(self, client, mock_k8s_client):
        self._set_up_folder(
            mock_k8s_client,
            access={
                "env_access": {
                    "prod": {"admins": ["old-prod"]},
                    "dev":  {"admins": ["old-dev"]},
                },
            },
        )
        # Sending env_access replaces the whole map.
        resp = client.patch(
            "/api/v1/folders/team-dev/access",
            json={"env_access": {"prod": {"admins": ["new-prod"], "members": [], "viewers": []}}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert list(body["env_access"].keys()) == ["prod"]
        assert body["env_access"]["prod"]["admins"] == ["new-prod"]

    def test_patch_404_when_folder_missing(self, client, mock_k8s_client):
        cm = MagicMock()
        cm.data = {}
        mock_k8s_client.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)
        resp = client.patch("/api/v1/folders/no-such/access", json={})
        assert resp.status_code == 404

    def test_patch_first_time_sets_access_on_legacy_folder(self, client, mock_k8s_client):
        """Legacy folder (no `access` block) — PATCH should initialise it."""
        self._set_up_folder(mock_k8s_client, access=None)
        resp = client.patch(
            "/api/v1/folders/team-dev/access",
            json={"admins": ["new-admin"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["admins"] == ["new-admin"]
        assert body["members"] == []  # initialised empty
        assert body["env_access"] == {}
