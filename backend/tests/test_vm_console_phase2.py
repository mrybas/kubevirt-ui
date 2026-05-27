"""Phase 2 — VM console WebSocket env-member enforcement.

Tests `_ws_authenticate` directly (unit-level — full WS proxy needs a
live VM, out of scope for unit tests).  Validates:

* Auth-disabled mode bypasses the check.
* Missing token closes with 1008.
* Invalid token closes with 1008.
* Authenticated non-member closes with 1008 before `accept`.
* Authenticated env-member returns True.
* Global admin bypasses the env-member check.
* Unmanaged namespace closes with 1008.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.v1 import folders as folders_mod
from app.api.v1 import vm_console
from app.core.auth import User


def _ns_obj(folder: str = "team-a", env: str = "prod") -> MagicMock:
    ns = MagicMock()
    ns.metadata.labels = {
        folders_mod.ENV_FOLDER_LABEL: folder,
        folders_mod.ENV_ENVIRONMENT_LABEL: env,
    }
    return ns


def _mock_websocket(token: str | None = "tok") -> MagicMock:
    ws = MagicMock()
    ws.query_params = {"token": token} if token else {}
    ws.close = AsyncMock()
    ws.app.state.k8s_client = MagicMock()
    ws.app.state.k8s_client.core_api = MagicMock()
    ws.app.state.k8s_client.core_api.read_namespace = AsyncMock(return_value=_ns_obj())
    cm = MagicMock()
    cm.data = {
        "team-a": json.dumps({
            "access": {"members": ["team-a-devs"]},
        }),
    }
    ws.app.state.k8s_client.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)
    return ws


@pytest.mark.asyncio
async def test_auth_none_bypasses_check(monkeypatch):
    monkeypatch.setattr(vm_console, "AUTH_TYPE", "none")
    ws = _mock_websocket()
    assert await vm_console._ws_authenticate(ws, namespace="team-a-prod") is True
    ws.close.assert_not_called()


@pytest.mark.asyncio
async def test_missing_token_closes_with_1008(monkeypatch):
    monkeypatch.setattr(vm_console, "AUTH_TYPE", "oidc")
    ws = _mock_websocket(token=None)
    assert await vm_console._ws_authenticate(ws, namespace="team-a-prod") is False
    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs.get("code") == 1008


@pytest.mark.asyncio
async def test_invalid_token_closes_with_1008(monkeypatch):
    monkeypatch.setattr(vm_console, "AUTH_TYPE", "oidc")
    monkeypatch.setattr(vm_console, "validate_oidc_token", AsyncMock(return_value=None))
    ws = _mock_websocket()
    assert await vm_console._ws_authenticate(ws, namespace="team-a-prod") is False
    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs.get("code") == 1008


@pytest.mark.asyncio
async def test_authenticated_non_member_rejected(monkeypatch):
    """User authenticated but not in any role → 1008 close, returns False."""
    monkeypatch.setattr(vm_console, "AUTH_TYPE", "oidc")
    user = User(id="u", email="u@x", username="u", groups=["random-group"])
    monkeypatch.setattr(vm_console, "validate_oidc_token", AsyncMock(return_value=user))
    ws = _mock_websocket()

    assert await vm_console._ws_authenticate(ws, namespace="team-a-prod") is False
    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs.get("code") == 1008
    assert "member" in (ws.close.await_args.kwargs.get("reason") or "").lower()


@pytest.mark.asyncio
async def test_env_member_accepted(monkeypatch):
    monkeypatch.setattr(vm_console, "AUTH_TYPE", "oidc")
    user = User(id="u", email="u@x", username="u", groups=["team-a-devs"])
    monkeypatch.setattr(vm_console, "validate_oidc_token", AsyncMock(return_value=user))
    ws = _mock_websocket()

    assert await vm_console._ws_authenticate(ws, namespace="team-a-prod") is True
    ws.close.assert_not_called()


@pytest.mark.asyncio
async def test_global_admin_bypasses_lookup(monkeypatch):
    """Global admin → fast path, no namespace lookup needed."""
    monkeypatch.setattr(vm_console, "AUTH_TYPE", "oidc")
    user = User(id="u", email="u@x", username="u", groups=["kubevirt-ui-admins"])
    monkeypatch.setattr(vm_console, "validate_oidc_token", AsyncMock(return_value=user))
    ws = _mock_websocket()

    assert await vm_console._ws_authenticate(ws, namespace="team-a-prod") is True
    # No need to read the namespace — admin short-circuits.
    ws.app.state.k8s_client.core_api.read_namespace.assert_not_called()


@pytest.mark.asyncio
async def test_unmanaged_namespace_rejected(monkeypatch):
    monkeypatch.setattr(vm_console, "AUTH_TYPE", "oidc")
    user = User(id="u", email="u@x", username="u", groups=["team-a-devs"])
    monkeypatch.setattr(vm_console, "validate_oidc_token", AsyncMock(return_value=user))
    ws = _mock_websocket()
    # Namespace exists but has no folder labels → resolve_env raises 404.
    unmanaged = MagicMock()
    unmanaged.metadata.labels = {}
    ws.app.state.k8s_client.core_api.read_namespace = AsyncMock(return_value=unmanaged)

    assert await vm_console._ws_authenticate(ws, namespace="kube-system") is False
    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs.get("code") == 1008
