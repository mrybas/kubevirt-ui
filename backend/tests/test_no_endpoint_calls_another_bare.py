"""Calling an endpoint function directly must pass the user along.

`scale_tenant` ended with `return await get_tenant(request, name)`. FastAPI
resolves `user: User = Depends(require_auth)` only for requests it routes, so
the direct call handed the *Depends object* to the authorisation check:

    File "app/api/v1/tenants_common.py", line 647, in require_tenant_access
      if is_admin(user.groups, user):
    AttributeError: 'Depends' object has no attribute 'groups'

Every tenant scale answered 500 — after having scaled the MachineDeployment.
"""

import re
from pathlib import Path

SRC_DIR = Path("app/api/v1")

# Handlers that take an authenticated user; calling them without one is the bug.
CALL = re.compile(r"await (get_tenant|get_vm|get_folder|get_vpc)\((request|req)[^)]*\)")


def _calls_without_user(source: str) -> list[str]:
    out = []
    for m in CALL.finditer(source):
        call = m.group(0)
        if "user=" not in call:
            out.append(call)
    return out


def test_no_handler_calls_another_without_passing_the_user() -> None:
    offenders = {}
    for path in SRC_DIR.glob("*.py"):
        bad = _calls_without_user(path.read_text())
        if bad:
            offenders[path.name] = bad
    assert not offenders, (
        "these calls would receive a Depends object as the user: " + repr(offenders)
    )


def test_the_scale_path_passes_it() -> None:
    src = (SRC_DIR / "tenants_crud.py").read_text()
    assert "get_tenant(request, name, user=user)" in src
