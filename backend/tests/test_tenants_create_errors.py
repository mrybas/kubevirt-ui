"""A failed tenant create must say what the API server said.

The UI showed "Failed to create tenant" and nothing else for an
anti-escalation refusal whose message named the exact missing verbs. The only
way to see it was `kubectl logs` on the backend.
"""

import json

import pytest
from fastapi import HTTPException
from kubernetes_asyncio.client import ApiException

from app.api.v1.tenants_crud import _api_reason


def _forbidden() -> ApiException:
    e = ApiException(status=403, reason="Forbidden")
    e.body = json.dumps({
        "kind": "Status",
        "status": "Failure",
        "message": (
            'roles.rbac.authorization.k8s.io "capk-infra" is forbidden: user '
            '"system:serviceaccount:kubevirt-ui-system:kubevirt-ui" is '
            "attempting to grant RBAC permissions not currently held:\n"
            '{APIGroups:[""], Resources:["events"], Verbs:["create" "patch"]}'
        ),
        "reason": "Forbidden",
        "code": 403,
    })
    return e


def test_the_reason_survives() -> None:
    reason = _api_reason(_forbidden())
    assert "capk-infra" in reason
    assert "not currently held" in reason
    assert 'Verbs:["create" "patch"]' in reason


def test_it_is_one_line() -> None:
    # Straight into a toast; a body with newlines renders as one run-on line
    # anyway, so collapse it deliberately rather than by accident.
    assert "\n" not in _api_reason(_forbidden())


def test_it_does_not_leak_the_envelope() -> None:
    reason = _api_reason(_forbidden())
    assert "HTTP response headers" not in reason
    assert "Audit-Id" not in reason


def test_a_bodyless_exception_still_says_something() -> None:
    e = ApiException(status=500, reason="Internal Server Error")
    e.body = None
    assert _api_reason(e) == "Internal Server Error"


def test_unparseable_body_falls_back_to_reason() -> None:
    e = ApiException(status=409, reason="Conflict")
    e.body = "<html>gateway</html>"
    assert _api_reason(e) == "Conflict"
