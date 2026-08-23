"""Backups are authorised against what they reach.

Every route in this module took a bare `require_auth`. Any authenticated user
could back up, **restore**, or delete any namespace in the cluster, and manage
the BackupStorageLocations every backup is written to. A restore is the worst of
them: it writes other people's namespaces from a snapshot they never agreed to.

The rule is one sentence — you may act on a backup only if you are a member of
every namespace it covers — and it is checked against the resolved spec rather
than the request, because the request has three ways of producing the same spec
and only the spec is what Velero acts on.

The empty case is the dangerous one. Velero reads "no includedNamespaces" as
*every* namespace, so a request naming no folder, no environment and no explicit
list is a whole-cluster backup. That is precisely what an unprivileged caller
would send by accident.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1.velero_backups import _authorise_scope, _covered_namespaces
from app.core.auth import User

FOLDERS = {
    "poc-transit": {
        "access": {
            "env_access": {
                "dev": {"members": ["kv-poc-transit-members"]},
            },
        },
    },
    "finance": {"access": {"env_access": {"prod": {"members": ["fin-members"]}}}},
}

NAMESPACES = {
    "poc-transit-dev": ("poc-transit", "dev"),
    "finance-prod": ("finance", "prod"),
}


@pytest.fixture
def k8s(monkeypatch):
    import app.core.groups as groups

    async def resolve_env(_client, namespace):
        if namespace not in NAMESPACES:
            raise HTTPException(status_code=404, detail="unmanaged")
        return NAMESPACES[namespace]

    async def load_folder(_client, folder):
        return FOLDERS[folder]

    monkeypatch.setattr(groups, "resolve_env", resolve_env)
    monkeypatch.setattr(groups, "load_folder", load_folder)
    return MagicMock()


def _user(*groups_):
    return User(id="u", email="u@lab", username="u", groups=list(groups_))


MEMBER = "kv-poc-transit-members"


@pytest.mark.asyncio
async def test_a_member_may_back_up_their_own_environment(k8s) -> None:
    await _authorise_scope(k8s, _user(MEMBER), ["poc-transit-dev"], "back up")


@pytest.mark.asyncio
async def test_a_member_may_not_back_up_somebody_elses(k8s) -> None:
    with pytest.raises(HTTPException) as e:
        await _authorise_scope(k8s, _user(MEMBER), ["finance-prod"], "back up")
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_one_foreign_namespace_in_the_set_is_enough_to_refuse(k8s) -> None:
    with pytest.raises(HTTPException) as e:
        await _authorise_scope(
            k8s, _user(MEMBER), ["poc-transit-dev", "finance-prod"], "back up",
        )
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_the_empty_set_is_the_whole_cluster_and_takes_an_admin(k8s) -> None:
    """Velero's own semantics, and the shape an accidental request has."""
    with pytest.raises(HTTPException) as e:
        await _authorise_scope(k8s, _user(MEMBER), [], "back up")
    assert e.value.status_code == 403
    assert "every namespace in the cluster" in e.value.detail

    await _authorise_scope(k8s, _user("kubevirt-ui-admins"), [], "back up")


@pytest.mark.asyncio
async def test_an_unmanaged_namespace_is_refused_not_reported(k8s) -> None:
    """404 would answer "does this namespace exist?" for anyone who asks."""
    with pytest.raises(HTTPException) as e:
        await _authorise_scope(k8s, _user(MEMBER), ["kube-system"], "back up")
    assert e.value.status_code == 403


def test_the_covered_set_is_read_from_the_spec() -> None:
    assert _covered_namespaces({"includedNamespaces": ["a", "b"]}) == ["a", "b"]
    assert _covered_namespaces({}) == []


# Each route, and the decision made for it. A new route with no entry fails.
EXPECTED = {
    # The listings are `require_auth` at the door and filtered inside: a
    # backup names the namespaces it covers, and reading every backup in the
    # cluster was the folder-listing leak in another set of pages.
    ("GET", "/velero/backups"): "require_auth",
    ("GET", "/velero/restores"): "require_auth",
    ("POST", "/velero/backups"): "require_auth+scope",
    ("DELETE", "/velero/backups/{name}"): "require_auth+scope",
    ("POST", "/velero/backups/{backup_name}/restore"): "require_auth+scope",
    ("GET", "/velero/schedules"): "require_auth",
    ("POST", "/velero/schedules"): "require_auth+scope",
    ("DELETE", "/velero/schedules/{name}"): "require_auth+scope",
    ("PATCH", "/velero/schedules/{name}"): "require_auth+scope",
    ("GET", "/velero/storage"): "require_auth",
    ("POST", "/velero/storage"): "require_admin",
    ("PUT", "/velero/storage/{name}"): "require_admin",
    ("DELETE", "/velero/storage/{name}"): "require_admin",
}


def test_every_velero_route_has_a_decision() -> None:
    import inspect

    from fastapi import FastAPI
    from fastapi.routing import APIRoute

    from app.api.v1.velero_backups import router

    app = FastAPI()
    app.include_router(router, prefix="/velero")

    found = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        source = inspect.getsource(route.endpoint)
        guards = {
            getattr(d.call, "__name__", "")
            for d in route.dependant.dependencies
        }
        if "require_admin" in guards:
            decision = "require_admin"
        elif "_authorise_scope" in source:
            decision = "require_auth+scope"
        else:
            decision = "require_auth"
        for method in route.methods & {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            found[(method, route.path)] = decision

    assert found == EXPECTED, (
        "a velero route changed its authorization, or a new one arrived with "
        f"none: {sorted(set(found.items()) ^ set(EXPECTED.items()))}"
    )
