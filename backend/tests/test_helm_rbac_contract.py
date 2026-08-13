"""Contract test: the chart must grant what the backend actually calls.

This class of bug is invisible to every other test here. The Python is
correct, the unit tests pass, the object is built exactly right — and the
call returns 403 on any cluster with RBAC enabled, which is all of them. It
only ever shows up on a live cluster, usually as a feature that silently does
nothing.

Three shipped features were broken this way at once: the VpcDns route needed
`patch` on deployments, BGP peering needed `bgp-confs` (absent from the chart
entirely), and the control-plane restart needed `delete` on pods.

Each entry below is a permission some code path depends on, with the caller
named. Add a row when you add a call; the failure message tells the next
person exactly which rule to extend.
"""

import re
from pathlib import Path

import pytest
import yaml

_CHART_RBAC = Path("kubevirt-ui") / "templates" / "rbac.yaml"
# In the test container only ./backend is mounted at /app, so the chart comes
# in separately at /helm (see docker-compose.yml). Outside it, walk up.
_CANDIDATES = [
    Path("/helm") / _CHART_RBAC,
    Path(__file__).resolve().parents[2] / "helm" / _CHART_RBAC,
]
RBAC_TEMPLATE = next((p for p in _CANDIDATES if p.is_file()), None)

pytestmark = pytest.mark.skipif(
    RBAC_TEMPLATE is None,
    reason=(
        "Helm chart not reachable; mount it at /helm to run the RBAC contract "
        f"test (looked in: {', '.join(str(p) for p in _CANDIDATES)})"
    ),
)

# (api group, resource, verb, who needs it)
REQUIRED: list[tuple[str, str, str, str]] = [
    # --- VpcDns: route the service network so the ClusterIP is reachable ---
    ("apps", "deployments", "patch", "vpcs._ensure_vpc_dns_service_route"),
    ("apps", "deployments", "get", "network._find_kubeovn_namespace"),
    # --- Talos workers: provider install + per-machine config template ---
    # Both 403'd on a live cluster while every unit test passed; the tenant
    # got as far as writing its Talos secrets and PKI before dying.
    ("operator.cluster.x-k8s.io", "bootstrapproviders", "create",
     "tenants_talos._ensure_talos_bootstrap_provider"),
    ("operator.cluster.x-k8s.io", "bootstrapproviders", "get",
     "tenants_talos._ensure_talos_bootstrap_provider"),
    ("bootstrap.cluster.x-k8s.io", "talosconfigtemplates", "create",
     "tenants_capi (Talos worker path)"),
    ("bootstrap.cluster.x-k8s.io", "talosconfigtemplates", "patch",
     "tenants_capi (Talos worker path)"),
    # --- BGP peering for egress gateways ---
    ("kubeovn.io", "bgp-confs", "get", "bgp.get_bgp_conf"),
    ("kubeovn.io", "bgp-confs", "list", "bgp.list_bgp_confs"),
    ("kubeovn.io", "bgp-confs", "create", "bgp.upsert_bgp_conf"),
    ("kubeovn.io", "bgp-confs", "update", "bgp.upsert_bgp_conf"),
    ("kubeovn.io", "bgp-confs", "delete", "bgp.delete_bgp_conf"),
    # --- Kamaji control-plane restart (delete pods, never rollout) ---
    ("", "pods", "delete", "tenants_common.restart_control_plane_pods"),
    ("", "pods", "list", "tenants_common.restart_control_plane_pods"),
    # --- VPC isolation ACLs and peering both patch kube-ovn objects ---
    ("kubeovn.io", "subnets", "patch", "subnet_acls / vpcs isolation ACLs"),
    ("kubeovn.io", "vpcs", "patch", "vpcs.create_vpc_peering"),
    ("kubeovn.io", "vpc-egress-gateways", "create", "egress_gateway.create"),
    ("kubeovn.io", "vpc-dnses", "create", "vpcs._ensure_vpc_dns"),
    # --- Tenant storage: the capk-infra / kubevirt-csi identities ---
    ("", "serviceaccounts", "create", "tenants_storage._ensure_service_account"),
    ("rbac.authorization.k8s.io", "roles", "create", "tenants_storage._ensure_role"),
    ("", "secrets", "create", "tenants_storage._ensure_kubeconfig_secret"),
    # --- Ceph storage discovery + clone targeting ---
    ("storage.k8s.io", "storageclasses", "list", "tenants_crud._discover_ceph"),
    # --- Kyverno DNS-injection policy per VPC ---
    ("kyverno.io", "clusterpolicies", "create", "vpcs._ensure_vpc_dns_policy"),
    # --- VPC underlay fabric ---
    ("kubeovn.io", "provider-networks", "create", "vpc_underlay.ensure"),
    ("kubeovn.io", "vlans", "create", "vpc_underlay.ensure"),
    ("kubeovn.io", "subnets", "create", "vpc_underlay.ensure"),
    ("k8s.cni.cncf.io", "network-attachment-definitions", "create", "vpc_underlay.ensure"),
    ("apps", "daemonsets", "create", "vpc_underlay._ensure_daemonset"),
    ("apps", "daemonsets", "patch", "vpc_underlay._ensure_daemonset (reconcile)"),
    ("", "nodes", "patch", "vpc_underlay._label_gateway_nodes"),
    ("apps", "daemonsets", "get", "vpc_underlay._kubeovn_cni_image"),
    ("apps", "daemonsets", "list", "vpc_underlay.get (workaround DaemonSets)"),
    # --- Talos tenants: PKI via cert-manager for the CSR signer ---
    ("cert-manager.io", "issuers", "create", "tenants_talos.build_talos_pki"),
    ("cert-manager.io", "certificates", "create", "tenants_talos.build_talos_pki"),
]


def _cluster_role_rules() -> list[dict]:
    """Rules of the cluster-wide ClusterRole, with Helm directives stripped.

    The template is plain YAML apart from a handful of `{{ }}` lines, none of
    which sit inside the rules themselves — so dropping them and parsing the
    first document is enough, and avoids needing helm in the test image.
    """
    raw = RBAC_TEMPLATE.read_text()
    # Drop whole-line Helm directives; keep indentation of everything else.
    cleaned = "\n".join(
        line for line in raw.splitlines()
        if not re.match(r"^\s*\{\{-?\s*(if|else|end|range|with)\b", line)
    )
    # Substitute the remaining inline templating with a placeholder so the
    # YAML stays parseable (names/labels are not what this test checks).
    cleaned = re.sub(r"\{\{-?.*?-?\}\}", "placeholder", cleaned)

    for doc in yaml.safe_load_all(cleaned):
        if isinstance(doc, dict) and doc.get("kind") == "ClusterRole":
            return doc.get("rules") or []
    raise AssertionError("No ClusterRole found in rbac.yaml")


@pytest.fixture(scope="module")
def rules() -> list[dict]:
    return _cluster_role_rules()


def _granted(rules: list[dict], group: str, resource: str, verb: str) -> bool:
    for rule in rules:
        groups = rule.get("apiGroups") or []
        resources = rule.get("resources") or []
        verbs = rule.get("verbs") or []
        if (group in groups or "*" in groups) \
                and (resource in resources or "*" in resources) \
                and (verb in verbs or "*" in verbs):
            return True
    return False


@pytest.mark.parametrize(
    "group,resource,verb,caller",
    REQUIRED,
    ids=[f"{g or 'core'}/{r}:{v}" for g, r, v, _ in REQUIRED],
)
def test_chart_grants_permission(
    rules: list[dict], group: str, resource: str, verb: str, caller: str,
) -> None:
    assert _granted(rules, group, resource, verb), (
        f"{caller} calls {verb} on {group or 'core'}/{resource}, but the chart's "
        f"ClusterRole does not grant it — the call will 403 on a live cluster "
        f"while every unit test passes. Add the verb in "
        f"helm/kubevirt-ui/templates/rbac.yaml."
    )


def test_the_template_is_parseable(rules: list[dict]) -> None:
    # Guards the stripping above: if the template grows a construct this
    # cannot handle, fail loudly here rather than silently checking nothing.
    assert len(rules) > 10


# ---------------------------------------------------------------------------
# Anti-escalation: we can only grant what we already hold
# ---------------------------------------------------------------------------

def _rules_we_grant() -> list[tuple[str, str, str, str]]:
    """Every (group, resource, verb) the code puts into a Role it creates.

    Derived from the builders themselves rather than restated here, because a
    hand-kept list is exactly what drifted: the write verbs were added to
    `_capk_role_rules` and the chart's ClusterRole was never widened to match.
    """
    from app.api.v1.tenants_storage import (
        _capk_role_rules,
        _csi_role_rules,
    )

    out: list[tuple[str, str, str, str]] = []
    for source, builder in (
        ("capk-infra", _capk_role_rules),
        ("kubevirt-csi", _csi_role_rules),
    ):
        for rule in builder():
            for group in rule.get("apiGroups", [""]):
                for resource in rule.get("resources", []):
                    for verb in rule.get("verbs", []):
                        out.append((group, resource, verb, source))
    return out


@pytest.mark.parametrize(
    "group,resource,verb,source",
    _rules_we_grant(),
    ids=lambda v: str(v),
)
def test_we_hold_every_permission_we_grant(
    rules: list[dict], group: str, resource: str, verb: str, source: str,
) -> None:
    """Kubernetes refuses to let a subject grant rights it does not hold.

    Creating a tenant with storage builds the namespaced `capk-infra` Role in
    `tenant-<name>`, and the API server checks each rule against what the UI's
    ServiceAccount holds *in that namespace* — which means the cluster-wide
    ClusterRole, since the UI has no Role there. Verbs held only in the UI's
    own namespaced Role do not count.

    On the cluster that failure reads:

        roles.rbac.authorization.k8s.io "capk-infra" is forbidden: user
        "system:serviceaccount:kubevirt-ui-system:kubevirt-ui" is attempting
        to grant RBAC permissions not currently held:
        {APIGroups:[""], Resources:["events"], Verbs:["create" "patch"]}
        {APIGroups:[""], Resources:["pods"], Verbs:["create" "update" "patch"]}
        {APIGroups:[""], Resources:["services"], Verbs:["update" "patch" "delete"]}

    and the UI reports only "Failed to create tenant". Nothing before this
    point notices: the builders are unit-tested, the role bodies are correct,
    and the chart is valid YAML.
    """
    assert _granted(rules, group, resource, verb), (
        f"the {source} Role grants {group or 'core'}/{resource}:{verb}, which "
        f"the chart's ClusterRole does not hold. Kubernetes will refuse to "
        f"create that Role — add the verb to helm/kubevirt-ui/templates/"
        f"rbac.yaml (ClusterRole, not the namespaced Role)."
    )
