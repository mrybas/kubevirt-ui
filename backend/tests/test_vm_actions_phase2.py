"""Phase 2 — VM actions cross-env authorisation.

Covers `clone_vm`: when `target_namespace` differs from the source,
the user must also be `env_member` of the *target*.  This prevents an
env-member of folder A from cloning a VM into a namespace they don't
own (folder B).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1 import folders as folders_mod


# A non-admin user; the test wires per-call group membership via fake_user.groups.
def _set_clone_test_state(mock_k8s_client, source_folder_access, target_folder_access):
    """Wire up the k8s mock so:
    - source namespace `team-a-prod` belongs to folder `team-a`
    - target namespace `team-b-prod` belongs to folder `team-b`
    - both folders have the supplied access blocks
    """

    async def _read_ns(name=None, **kw):
        ns = MagicMock()
        ns.metadata.name = name
        if name == "team-a-prod":
            ns.metadata.labels = {
                folders_mod.ENV_FOLDER_LABEL: "team-a",
                folders_mod.ENV_ENVIRONMENT_LABEL: "prod",
            }
        elif name == "team-b-prod":
            ns.metadata.labels = {
                folders_mod.ENV_FOLDER_LABEL: "team-b",
                folders_mod.ENV_ENVIRONMENT_LABEL: "prod",
            }
        else:
            ns.metadata.labels = {}
        return ns

    cm = MagicMock()
    cm.data = {
        "team-a": json.dumps({"display_name": "team-a", "access": source_folder_access}),
        "team-b": json.dumps({"display_name": "team-b", "access": target_folder_access}),
    }
    cm.metadata = MagicMock()

    mock_k8s_client.core_api = MagicMock()
    mock_k8s_client.core_api.read_namespace = AsyncMock(side_effect=_read_ns)
    mock_k8s_client.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)
    mock_k8s_client._api_client = MagicMock()


def test_clone_into_same_namespace_passes(client, mock_k8s_client):
    """Same-namespace clone: only the source dep gate is involved.

    The conftest override on `require_env_member()` short-circuits to
    fake_user; the k8s state below is irrelevant for the source check
    (handled by the override).  We assert we don't hit a 403 from the
    *target* check (which only runs on cross-namespace clones).
    """
    _set_clone_test_state(
        mock_k8s_client,
        source_folder_access={"members": ["team-a-devs"]},
        target_folder_access={"members": ["team-a-devs"]},
    )
    # Patch out the actual kubevirt operations — we only care about the auth path.
    mock_k8s_client.create_virtual_machine = AsyncMock()
    # CustomObjectsApi calls go through k8s_client._api_client — give them a stub.
    # Stub the kubevirt fetch with an ApiException so the clone handler's
    # `except ApiException` converts it to a 500 — that's the "we got past
    # the cross-env auth check" signal we want to assert on.
    from kubernetes_asyncio.client.rest import ApiException
    custom_api = MagicMock()
    custom_api.get_namespaced_custom_object = AsyncMock(
        side_effect=ApiException(status=500, reason="stub: source VM lookup"),
    )

    from app.api.v1 import vm_actions
    # The clone endpoint enters the body of the function only when target_ns ==
    # source_ns.  We patch CustomObjectsApi at the module level (since the call
    # happens via `client.CustomObjectsApi(...)`).
    import kubernetes_asyncio.client as kc
    orig = kc.CustomObjectsApi
    kc.CustomObjectsApi = MagicMock(return_value=custom_api)
    try:
        resp = client.post(
            "/api/v1/namespaces/team-a-prod/vms/myvm/clone",
            params={"namespace": "team-a-prod"},
            json={"display_name": "myvm-clone", "start": False},
        )
        # Same-namespace → the target authz block is skipped; we expect to fail
        # *somewhere later* in the clone logic (500), not 403.
        assert resp.status_code != 403
    finally:
        kc.CustomObjectsApi = orig


def test_clone_into_different_namespace_rejected_when_not_target_member(
    client, mock_k8s_client, fake_user,
):
    """User is env_member of source (via override) but not of target → 403."""
    fake_user.groups = ["team-a-devs"]  # source folder member, NOT target
    _set_clone_test_state(
        mock_k8s_client,
        source_folder_access={"members": ["team-a-devs"]},
        target_folder_access={"admins": ["team-b-admins"], "members": ["team-b-devs"]},
    )

    resp = client.post(
        "/api/v1/namespaces/team-a-prod/vms/myvm/clone",
        params={"namespace": "team-a-prod"},
        json={
            "display_name": "myvm-clone",
            "start": False,
            "target_namespace": "team-b-prod",
        },
    )
    assert resp.status_code == 403, resp.text
    assert "target environment" in resp.json()["detail"]


def test_clone_into_different_namespace_allowed_when_target_member(
    client, mock_k8s_client, fake_user,
):
    """User is env_member of BOTH source and target — clone proceeds past authz."""
    fake_user.groups = ["team-a-devs", "team-b-devs"]
    _set_clone_test_state(
        mock_k8s_client,
        source_folder_access={"members": ["team-a-devs"]},
        target_folder_access={"members": ["team-b-devs"]},
    )

    # Stub out the kubevirt clone body so we don't dive into the real logic.
    # Stub the kubevirt fetch with an ApiException so the clone handler's
    # `except ApiException` converts it to a 500 — that's the "we got past
    # the cross-env auth check" signal we want to assert on.
    from kubernetes_asyncio.client.rest import ApiException
    custom_api = MagicMock()
    custom_api.get_namespaced_custom_object = AsyncMock(
        side_effect=ApiException(status=500, reason="stub: source VM lookup"),
    )
    import kubernetes_asyncio.client as kc
    orig = kc.CustomObjectsApi
    kc.CustomObjectsApi = MagicMock(return_value=custom_api)
    try:
        resp = client.post(
            "/api/v1/namespaces/team-a-prod/vms/myvm/clone",
            params={"namespace": "team-a-prod"},
            json={
                "display_name": "myvm-clone",
                "start": False,
                "target_namespace": "team-b-prod",
            },
        )
        # Past the cross-env check; expect failure later (500), not 403.
        assert resp.status_code != 403, resp.text
    finally:
        kc.CustomObjectsApi = orig


def test_clone_into_unmanaged_namespace_rejected(client, mock_k8s_client, fake_user):
    """If target namespace has no folder label → 404 → translated to 403-class denial."""
    fake_user.groups = ["team-a-devs"]

    async def _read_ns(name=None, **kw):
        ns = MagicMock()
        ns.metadata.name = name
        ns.metadata.labels = {}  # unmanaged
        return ns

    cm = MagicMock()
    cm.data = {"team-a": json.dumps({"access": {"members": ["team-a-devs"]}})}
    cm.metadata = MagicMock()
    mock_k8s_client.core_api = MagicMock()
    mock_k8s_client.core_api.read_namespace = AsyncMock(side_effect=_read_ns)
    mock_k8s_client.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)
    mock_k8s_client._api_client = MagicMock()

    resp = client.post(
        "/api/v1/namespaces/team-a-prod/vms/myvm/clone",
        params={"namespace": "team-a-prod"},
        json={
            "display_name": "x",
            "start": False,
            "target_namespace": "kube-system",
        },
    )
    # resolve_env raises HTTPException 404 for unmanaged ns — bubbles up unchanged.
    assert resp.status_code == 404
