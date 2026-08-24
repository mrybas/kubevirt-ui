"""The HelmReleases, asserted by both implementations.

The table was taken once from this module — it is the reference — with the
catalogue the stand carries, and is read by the operator's suite too. The
acceptance for the addon slice is exactly this: an addon enabled through the
operator produces the release an addon enabled through the old UI does, byte
for byte.
"""

import json
from pathlib import Path

import pytest

_MOUNTED = Path("/test/parity/addon-releases.json")
_IN_REPO = Path(__file__).resolve().parents[2] / "test" / "parity" / "addon-releases.json"
TABLE = _MOUNTED if _MOUNTED.exists() else _IN_REPO


def _table():
    return json.loads(TABLE.read_text())


def _catalog(raw):
    from app.models.tenant import AddonCatalog, AddonComponent

    return AddonCatalog(
        git_repository_ref=raw.get("gitRepositoryRef", {}),
        base_path=raw.get("basePath", "base"),
        components=[AddonComponent(**c) for c in raw.get("components", [])],
    )


@pytest.mark.parametrize("case", [
    pytest.param(r, id=r["metadata"]["name"]) for r in _table()["releases"]
])
def test_the_release_is_what_the_table_says(case) -> None:
    from app.api.v1.tenants_addons import _build_flux_helmrelease_cr, _build_helm_values

    table = _table()
    catalog = _catalog(table["catalog"])
    tenant = table["tenant"]

    addon_id = case["metadata"]["labels"]["kubevirt-ui.io/addon"]
    asked = next(r["parameters"] for r in table["requested"] if r["id"] == addon_id)
    component = catalog.get_component(addon_id)
    params = {p.id: asked.get(p.id, p.default) for p in component.parameters}

    values = _build_helm_values(tenant, component, params)
    if component.id == "namespaces":
        values = {"namespaces": [
            {"name": catalog.get_component(r["id"]).namespace}
            for r in table["requested"]
            if catalog.get_component(r["id"]) and catalog.get_component(r["id"]).namespace
        ]}

    if component.id == "namespaces":
        depends_on = None
    elif component.category == "networking" and component.required:
        depends_on = [f"{tenant}-namespaces"]
    else:
        depends_on = [f"{tenant}-calico"]

    built = _build_flux_helmrelease_cr(
        tenant_name=tenant, addon_id=addon_id, component=component,
        catalog=catalog, helm_values=values, depends_on=depends_on,
    )
    assert built["spec"] == case["spec"]
    assert built["metadata"]["labels"] == case["metadata"]["labels"]
