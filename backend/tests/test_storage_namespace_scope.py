"""A namespace in the query string is not an authorisation.

Every storage route takes `namespace` from the caller and reads it with the
UI's own ServiceAccount, which can read all of them. Measured on the cluster,
signed in as a user with no groups and no folder membership:

    GET /api/v1/storage/datavolumes?namespace=e2e-lab-prod
    -> 200  {"items":[{"name":"almalinux-9-...","display_name":"AlmaLinux 9",...

while the VM list for the very same namespace correctly returned nothing —
the VM path scopes by `get_user_namespaces`, the storage path did not. One
query parameter stood between an unprivileged session and another team's
images and disks.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1 import storage as storage_mod
from app.api.v1.storage import require_namespace_access

MINE = "team-a-prod"
THEIRS = "team-b-prod"


def _request() -> MagicMock:
    request = MagicMock()
    request.app.state.k8s_client = MagicMock()
    return request


def _user() -> SimpleNamespace:
    return SimpleNamespace(email="nobody@lab.local", username="nobody", groups=[])


@pytest.fixture
def only_mine(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ns(_k8s, _user):
        return [MINE]

    monkeypatch.setattr(storage_mod, "get_user_namespaces", _ns)


@pytest.mark.asyncio
async def test_own_namespace_is_allowed(only_mine) -> None:
    await require_namespace_access(_request(), _user(), MINE)


@pytest.mark.asyncio
async def test_someone_elses_namespace_is_refused(only_mine) -> None:
    with pytest.raises(HTTPException) as exc:
        await require_namespace_access(_request(), _user(), THEIRS)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_the_refusal_does_not_confirm_the_namespace_exists(only_mine) -> None:
    # A 403 would answer "yes, that team exists" to anyone who guesses a name.
    with pytest.raises(HTTPException) as exc:
        await require_namespace_access(_request(), _user(), THEIRS)
    assert exc.value.status_code == 404
    assert "not found" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_no_namespaces_at_all_means_nothing_is_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _none(_k8s, _user):
        return []

    monkeypatch.setattr(storage_mod, "get_user_namespaces", _none)
    with pytest.raises(HTTPException):
        await require_namespace_access(_request(), _user(), MINE)


def test_every_namespaced_storage_route_calls_the_guard() -> None:
    """The guard only helps where it is called.

    Asserted over the source rather than per-route, because the way this
    breaks again is a new route, not a changed one.
    """
    import inspect

    src = inspect.getsource(storage_mod)
    handlers = [
        "list_datavolumes",
        "get_datavolume",
        "create_datavolume",
        "delete_datavolume",
        "list_pvcs",
    ]
    for name in handlers:
        fn = getattr(storage_mod, name)
        body = inspect.getsource(fn)
        assert "require_namespace_access" in body, (
            f"{name} takes a namespace from the caller and never checks it"
        )
        assert "user" in inspect.signature(fn).parameters, (
            f"{name} has no user to check the namespace against"
        )
