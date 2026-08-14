"""Only one place may tear a VPC down.

Two modules grew their own teardown. `vpcs.delete_vpc` learned to wait for
the subnets and NAT objects before removing the router they finalize against
— after `snat-gamma-net`, `eip-gamma-net` and `con3-default` were stranded
permanently on the lab cluster — while `network.delete_vpc` kept the old
delete-subnets-then-delete-vpc copy and stranded them exactly as before. The
UI calls the fixed route, so nothing on screen could reveal the divergence.

A second copy is the failure mode, not the particular lines in it: whatever
the next teardown has to learn, it gets taught in one file and missed in the
other. So this asserts the shape.

What is banned is deleting a VPC *and* its subnets in one function without
waiting — rolling back a create whose subnet never came up is not that, and
stays allowed.
"""

import ast
from pathlib import Path

API = Path("app/api/v1")

# The gateway's own VPC, not a tenant's, and its teardown has the same wait —
# see test_egress_gateway_delete.py.
ALLOWED_MODULES = {"vpcs.py", "egress_gateway.py"}


def _deleted_plurals(node: ast.AST) -> set[str]:
    """Plurals passed to any delete_*_custom_object call inside `node`."""
    found: set[str] = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        fn = call.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if not name.startswith("delete_") or "custom_object" not in name:
            continue
        for kw in call.keywords:
            if kw.arg == "plural" and isinstance(kw.value, ast.Constant):
                found.add(kw.value.value)
    return found


def _teardown_functions(source: str) -> list[str]:
    """Functions that delete a VPC together with its subnets."""
    tree = ast.parse(source)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        plurals = _deleted_plurals(node)
        if "vpcs" in plurals and "subnets" in plurals:
            out.append(node.name)
    return out


def test_no_second_teardown() -> None:
    offenders = {
        f.name: _teardown_functions(f.read_text())
        for f in sorted(API.glob("*.py"))
        if f.name not in ALLOWED_MODULES
    }
    offenders = {k: v for k, v in offenders.items() if v}

    assert offenders == {}, (
        f"{offenders} tear a VPC down themselves. Removing the router means "
        "waiting for everything kube-ovn finalizes against it first — call "
        "vpcs.delete_vpc instead of copying the teardown."
    )


def test_the_owner_still_owns_it() -> None:
    """Guards the guard: were the teardown to move out of vpcs.py, the check
    above would pass by being vacuous."""
    assert "delete_vpc" in _teardown_functions((API / "vpcs.py").read_text())


def test_the_owner_waits_before_removing_the_router() -> None:
    src = (API / "vpcs.py").read_text()
    body = src[src.index("async def delete_vpc("):src.index("\n@router.", src.index("async def delete_vpc("))]

    assert body.index("_await_dependents_gone") < body.index('plural="vpcs"'), \
        "the wait has to come first, or it is decoration"
