"""Every API route must require authentication, save an explicit few.

This is the guard the codebase did not have. Auth was a per-route convention,
and 18 routes never got one — anonymous callers could

    DELETE /api/v1/projects/{name}          delete a project and cascade its namespaces
    POST   /api/v1/projects/{name}/access   create real RoleBindings
    DELETE /api/v1/storage/datavolumes/...  delete any DataVolume in any namespace
    POST   /api/v1/metrics/query            run arbitrary PromQL

`create_project` even read `request.state.user`, which nothing in the app
assigns, so the route *looked* guarded while being wide open. A convention
cannot catch that; a test over the actual route table can.

The allowlist below is deliberately tiny and every entry carries a reason.
Adding to it should feel like a decision, not a formality.
"""

import inspect

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute

from app.api.v1.router import router

# (path, why it is reachable without a session)
PUBLIC_ROUTES: dict[str, str] = {
    "/api/v1/auth/config": "the login page reads it before any session exists",
    "/api/v1/auth/refresh": "carries its own refresh token",
    "/api/v1/auth/logout": "must work with an expired session",
    "/api/v1/auth/me": "reports the identity, including the anonymous one",
    "/api/v1/auth/kubeconfig": "exchanges the caller's own tokens",
    "/api/v1/auth/token": "issues the session",
    "/api/v1/features": "feature flags gate the routes the SPA mounts at boot",
}


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


def _routes() -> list[APIRoute]:
    return [r for r in _app().routes if isinstance(r, APIRoute)]


def _dependency_names(route: APIRoute) -> set[str]:
    return {
        d.call.__name__
        for d in route.dependant.dependencies
        if getattr(d, "call", None) is not None
    } | {
        getattr(route.dependant.call, "__name__", ""),
    }


def _guarded(route: APIRoute) -> bool:
    names = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        call = getattr(dep, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", ""))
        stack.extend(getattr(dep, "dependencies", []))
    return bool(names & {"require_auth", "require_admin", "get_current_user"})


def test_the_route_table_is_actually_populated() -> None:
    # Guards the guard: an empty table would make every assertion below vacuous.
    assert len(_routes()) > 100


@pytest.mark.parametrize(
    "route", _routes(), ids=lambda r: f"{sorted(r.methods)[0]} {r.path}",
)
def test_route_requires_authentication(route: APIRoute) -> None:
    if route.path in PUBLIC_ROUTES:
        return

    assert _guarded(route), (
        f"{sorted(route.methods)} {route.path} has no auth dependency. Mount it "
        f"under the `protected` router in app/api/v1/router.py, or add it to "
        f"PUBLIC_ROUTES with a reason if it genuinely must be reachable "
        f"without a session."
    )


def test_public_routes_all_exist() -> None:
    # A stale allowlist entry hides the next hole behind a name nobody serves.
    paths = {r.path for r in _routes()}
    stale = set(PUBLIC_ROUTES) - paths
    assert not stale, f"PUBLIC_ROUTES lists routes that do not exist: {stale}"


# ---------------------------------------------------------------------------
# WebSockets
# ---------------------------------------------------------------------------

def _ws_routes() -> list[APIWebSocketRoute]:
    return [r for r in _app().routes if isinstance(r, APIWebSocketRoute)]


def test_there_are_websocket_routes_to_check() -> None:
    # The consoles are the only ones today; a zero here would make the rest of
    # this section pass by describing nothing.
    assert _ws_routes(), "no WebSocket routes found — has the console moved?"


@pytest.mark.parametrize(
    "route", _ws_routes(), ids=lambda r: f"WS {r.path}",
)
def test_websocket_routes_carry_no_http_dependency(route: APIWebSocketRoute) -> None:
    """A WebSocket cannot resolve `HTTPBearer`, and mounting it under the
    authenticated router does not fail loudly — it fails at connect time:

        TypeError: HTTPBearer.__call__() missing 1 required positional argument

    The socket closes with 1006 and the console spins forever. That is what
    happened when the consoles were moved under `protected`, and no test saw
    it because this file only walked `APIRoute`.
    """
    names = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        call = getattr(dep, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", ""))
        stack.extend(getattr(dep, "dependencies", []))

    forbidden = names & {"require_auth", "require_admin", "get_current_user"}
    assert not forbidden, (
        f"WS {route.path} depends on {sorted(forbidden)}, which resolves "
        "through HTTPBearer and cannot be given a WebSocket. Authenticate "
        "inside the handler instead (see vm_console._ws_authenticate)."
    )


@pytest.mark.parametrize(
    "route", _ws_routes(), ids=lambda r: f"WS {r.path}",
)
def test_websocket_routes_authenticate_themselves(route: APIWebSocketRoute) -> None:
    """Exempt from the dependency is not exempt from authentication.

    Read from the handler's own source rather than from a decorator, because
    the thing that must not be lost is the call itself.
    """
    source = inspect.getsource(route.endpoint)
    assert "_ws_authenticate" in source, (
        f"WS {route.path} never calls _ws_authenticate — it is reachable by "
        "anyone who knows the URL."
    )
    # The call has to gate the connection, not merely happen.
    assert "if not await _ws_authenticate" in source, (
        f"WS {route.path} calls _ws_authenticate but does not act on the "
        "result."
    )
