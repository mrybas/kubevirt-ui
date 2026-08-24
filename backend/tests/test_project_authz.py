"""Changing a project takes more than a session.

Authentication was fixed at the router — every route here sits behind
`require_auth` now. Authorization was not, and that is a different hole: any
logged-in user could

    POST   /api/v1/projects/{name}/access     grant themselves admin
    DELETE /api/v1/projects/{name}            delete the project and its namespaces
    POST   /api/v1/projects/{name}/environments

A viewer granting themselves an admin RoleBinding leaves nothing behind but the
binding, which reads exactly like one an admin made.

Folders answer this from an access block in their own record. Projects keep no
such block — their access *is* the set of managed RoleBindings — so that is what
gets read, and an environment-scoped binding does not carry project-wide rights.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.api.v1.projects import _is_project_admin, router
from app.core.auth import User

ADMIN_ROLE = "kubevirt-ui-admin"


def _user(name="viewer", groups=()):
    return User(id=name, email=f"{name}@lab", username=name, groups=list(groups))


def _binding(subject: str, *, role=ADMIN_ROLE, scope="project", kind="User"):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=f"b-{subject}",
            labels={"kubevirt-ui.io/access-scope": scope},
        ),
        role_ref=SimpleNamespace(name=role),
        subjects=[SimpleNamespace(name=subject, kind=kind)],
    )


def _k8s(bindings):
    k8s = MagicMock()
    k8s.core_api.list_namespace = AsyncMock(
        return_value=SimpleNamespace(
            items=[SimpleNamespace(metadata=SimpleNamespace(
                name="proj-dev", labels={}))],
        ),
    )
    rbac = MagicMock()
    rbac.list_namespaced_role_binding = AsyncMock(
        return_value=SimpleNamespace(items=bindings),
    )
    k8s.rbac_api = rbac
    return k8s, rbac


@pytest.fixture(autouse=True)
def _rbac(monkeypatch):
    import app.api.v1.projects as mod
    self = {}

    async def get_rbac(k8s):
        return self["rbac"]

    monkeypatch.setattr(mod, "_get_rbac_api", get_rbac)
    return self


@pytest.mark.asyncio
async def test_a_platform_admin_always_passes(_rbac) -> None:
    k8s, _rbac["rbac"] = _k8s([])
    assert await _is_project_admin(k8s, _user(groups=["kubevirt-ui-admins"]), "proj")


@pytest.mark.asyncio
async def test_a_session_alone_is_not_enough(_rbac) -> None:
    k8s, _rbac["rbac"] = _k8s([])
    assert not await _is_project_admin(k8s, _user(), "proj")


@pytest.mark.asyncio
async def test_the_project_admin_binding_is_what_counts(_rbac) -> None:
    k8s, _rbac["rbac"] = _k8s([_binding("alice")])
    assert await _is_project_admin(k8s, _user("alice"), "proj")


@pytest.mark.asyncio
async def test_a_group_binding_counts_too(_rbac) -> None:
    k8s, _rbac["rbac"] = _k8s([_binding("team-x", kind="Group")])
    assert await _is_project_admin(k8s, _user("bob", groups=["team-x"]), "proj")


@pytest.mark.asyncio
async def test_an_editor_binding_does_not(_rbac) -> None:
    k8s, _rbac["rbac"] = _k8s([_binding("alice", role="kubevirt-ui-editor")])
    assert not await _is_project_admin(k8s, _user("alice"), "proj")


@pytest.mark.asyncio
async def test_admin_of_one_environment_is_not_admin_of_the_project(_rbac) -> None:
    """Otherwise the smallest grant anyone can be given hands out every other."""
    k8s, _rbac["rbac"] = _k8s([_binding("alice", scope="environment")])
    assert not await _is_project_admin(k8s, _user("alice"), "proj")


# The routes that change something, and the guard each one must carry. Listed
# rather than discovered: a new mutating route with no entry here is the thing
# that went wrong last time, and a list is what makes it show up in review.
GUARDED = {
    ("POST", "/projects"): "require_admin",
    ("PATCH", "/projects/{name}"): "dep",
    ("DELETE", "/projects/{name}"): "dep",
    ("POST", "/projects/{name}/environments"): "dep",
    ("DELETE", "/projects/{name}/environments/{environment}"): "dep",
    ("POST", "/projects/{name}/access"): "dep",
    ("DELETE", "/projects/{name}/access/{binding_id}"): "dep",
}


def _mutating_routes() -> dict[tuple[str, str], APIRoute]:
    app = FastAPI()
    app.include_router(router, prefix="/projects")
    out = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods & {"POST", "PATCH", "PUT", "DELETE"}:
            out[(method, route.path)] = route
    return out


def test_every_mutating_project_route_is_authorised() -> None:
    routes = _mutating_routes()
    assert set(routes) == set(GUARDED), (
        "the mutating routes changed; each one needs an authorization decision "
        f"in this table. found={sorted(routes)}"
    )
    for key, expected in GUARDED.items():
        names = {
            getattr(d.call, "__name__", "")
            for d in routes[key].dependant.dependencies
        }
        # The dependency closures the factories return are all called `dep`.
        assert expected in names, (
            f"{key[0]} {key[1]} takes any session: dependencies are {names}"
        )
