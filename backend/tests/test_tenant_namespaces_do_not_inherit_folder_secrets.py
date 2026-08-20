"""A folder role must not read another cluster's credentials.

A tenant namespace is a folder namespace: it carries the folder and environment
labels so the tenant takes part in folder authorisation, and that is exactly
what put the folder roles into it. Measured on a live stand:

    kubectl auth can-i get secret/uat-t1-admin-kubeconfig -n tenant-uat-t1 \\
      --as=someone --as-group=kv-poc-transit-viewers
    yes

A folder *viewer* — the lowest role there is — could read that tenant's admin
kubeconfig and its cluster CA. That is cluster-admin on the tenant, handed to
anyone with read access to the folder. A folder member, whose role grants `*` on
secrets, could rewrite them, and could delete the worker VMs that are the
tenant's nodes.

What lives in a tenant namespace is not a user's workloads. It is another
cluster's certificates, datastore configuration and CAPI graph. Every product
operation on them runs as the backend behind `require_tenant_access`, so nothing
legitimate reads those secrets as the user — the kubeconfig download does not.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from app.api.v1.folders import (
    TENANT_CLUSTERROLES,
    TENANT_NS_LABEL,
    _role_for_namespace,
)
from app.models.project import ROLE_TO_CLUSTERROLE


def _chart_roles() -> dict[str, list[dict]]:
    """Every ClusterRole the chart ships, by name."""
    here = Path(__file__).resolve()
    for root in (here.parents[2], Path("/")):
        candidate = root / "helm" / "kubevirt-ui" / "templates" / "clusterroles.yaml"
        if candidate.is_file():
            break
    else:
        pytest.skip("the chart is not reachable")

    lines: list[str] = []
    for line in candidate.read_text().splitlines():
        replaced = re.sub(r"\{\{-?.*?-?\}\}", "\x00", line)
        if replaced.strip() == "\x00":
            continue
        lines.append(replaced.replace("\x00", "placeholder"))

    out: dict[str, list[dict]] = {}
    for doc in yaml.safe_load_all("\n".join(lines)):
        if isinstance(doc, dict) and doc.get("kind") == "ClusterRole":
            out[doc["metadata"]["name"]] = doc.get("rules") or []
    return out


def _grants(rules: list[dict], group: str, resource: str) -> set[str]:
    verbs: set[str] = set()
    for rule in rules:
        groups = rule.get("apiGroups") or []
        resources = rule.get("resources") or []
        if group not in groups and "*" not in groups:
            continue
        if resource not in resources and "*" not in resources:
            continue
        verbs |= set(rule.get("verbs") or [])
    return verbs


def test_the_folder_roles_really_do_grant_secrets():
    """The premise, checked. A test asserting the fix without confirming the
    problem would keep passing if the problem moved."""
    roles = _chart_roles()
    for name in ROLE_TO_CLUSTERROLE.values():
        assert name in roles, f"{name} is not in the chart"
        verbs = _grants(roles[name], "", "secrets")
        assert verbs, f"{name} no longer grants secrets — this test is stale"


@pytest.mark.parametrize("tenant_role", sorted(set(TENANT_CLUSTERROLES.values())))
def test_a_tenant_role_cannot_read_secrets(tenant_role: str):
    roles = _chart_roles()
    assert tenant_role in roles, f"{tenant_role} is not in the chart"
    verbs = _grants(roles[tenant_role], "", "secrets")
    assert not verbs, (
        f"{tenant_role} grants {sorted(verbs)} on secrets — that is the tenant's "
        f"admin kubeconfig and its cluster CA"
    )


@pytest.mark.parametrize("tenant_role", sorted(set(TENANT_CLUSTERROLES.values())))
def test_a_tenant_role_cannot_delete_the_clusters_own_machinery(tenant_role: str):
    """Deleting a Machine or a worker VM by hand is not an operation; it is an
    outage. Everything the product does goes through the backend."""
    roles = _chart_roles()
    for group, resource in (
        ("kubevirt.io", "virtualmachines"),
        ("cluster.x-k8s.io", "machines"),
        ("cluster.x-k8s.io", "machinedeployments"),
    ):
        verbs = _grants(roles[tenant_role], group, resource)
        forbidden = verbs & {"delete", "deletecollection", "create", "update", "patch", "*"}
        assert not forbidden, (
            f"{tenant_role} grants {sorted(forbidden)} on {group}/{resource}"
        )


def test_a_tenant_namespace_gets_the_tenant_role():
    labels = {TENANT_NS_LABEL: "uat-t1", "kubevirt-ui.io/folder": "poc"}
    for folder_role, tenant_role in TENANT_CLUSTERROLES.items():
        assert _role_for_namespace(folder_role, labels) == tenant_role


def test_an_ordinary_namespace_keeps_the_folder_role():
    """Projects are the case the folder roles were written for, and reading a
    secret there is a normal thing to do."""
    labels = {"kubevirt-ui.io/folder": "poc", "kubevirt-ui.io/environment": "dev"}
    for folder_role in TENANT_CLUSTERROLES:
        assert _role_for_namespace(folder_role, labels) == folder_role
    assert _role_for_namespace("kubevirt-ui-viewer", None) == "kubevirt-ui-viewer"


@pytest.mark.asyncio
async def test_the_binding_written_to_a_tenant_namespace_names_the_tenant_role():
    """End to end through the function that writes them, because the mapping
    being right matters less than the call site using it."""
    from app.api.v1 import folders

    written: list[dict] = []

    rbac = MagicMock()
    rbac.create_namespaced_role_binding = AsyncMock(
        side_effect=lambda **kw: written.append(kw["body"]) or MagicMock())

    k8s = MagicMock()
    k8s.core_api.read_namespace = AsyncMock(return_value=MagicMock(
        metadata=MagicMock(labels={TENANT_NS_LABEL: "uat-t1"})))

    original = folders._get_rbac_api
    folders._get_rbac_api = AsyncMock(return_value=rbac)
    try:
        await folders.reconcile_namespace_rbac(
            k8s, "tenant-uat-t1", "poc", "dev",
            {"access": {"viewers": ["kv-poc-viewers"], "members": ["kv-poc-members"],
                        "admins": ["kv-poc-admins"]}},
        )
    finally:
        folders._get_rbac_api = original

    assert written, "no bindings were written"
    for body in written:
        name = body["roleRef"]["name"]
        assert name in TENANT_CLUSTERROLES.values(), (
            f"a tenant namespace was bound to {name}"
        )
