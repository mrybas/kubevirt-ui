"""Every custom-object patch must name its content type.

`patch_namespaced_custom_object` / `patch_cluster_custom_object` default to
`application/json-patch+json`, which expects a *list of operations*. Every call
in this codebase passes a merge-style body (`{"spec": {...}}`), so a call that
omits `_content_type` is rejected by the API server:

    error decoding patch: json: cannot unmarshal object into
    Go value of type []handlers.jsonPatchOp

Nothing catches that at import or review time — it only shows up as a 400 on
the one code path that happens to run — so it is pinned statically here.
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

PATCH_CALL = re.compile(
    r"patch_(?:namespaced|cluster)_custom_object\((.*?)\n(\s*)\)", re.S,
)


def _call_sites_without_content_type() -> list[str]:
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        source = path.read_text()
        for match in PATCH_CALL.finditer(source):
            if "_content_type" in match.group(1):
                continue
            line = source[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(APP.parent)}:{line}")
    return offenders


def test_no_patch_call_relies_on_the_default_content_type() -> None:
    offenders = _call_sites_without_content_type()
    assert not offenders, (
        "these patch calls send a merge body as a JSON Patch and will 400 — "
        'add _content_type="application/merge-patch+json":\n  '
        + "\n  ".join(offenders)
    )


def test_the_scan_actually_matches_real_call_sites() -> None:
    # Guards the guard: a regex that silently stops matching would make the
    # test above pass forever.
    found = sum(
        len(PATCH_CALL.findall(path.read_text())) for path in APP.rglob("*.py")
    )
    assert found > 20, f"expected the codebase to patch custom objects, matched {found}"
