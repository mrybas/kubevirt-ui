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
    ("kyverno.io", "clusterpolicies", "delete", "vpcs.disable_vpc_dns_policy"),
    # --- VPC underlay fabric ---
    ("kubeovn.io", "provider-networks", "create", "vpc_underlay.ensure"),
    ("kubeovn.io", "vlans", "create", "vpc_underlay.ensure"),
    ("kubeovn.io", "subnets", "create", "vpc_underlay.ensure"),
    ("k8s.cni.cncf.io", "network-attachment-definitions", "create", "vpc_underlay.ensure"),
    ("apps", "daemonsets", "create", "vpc_underlay._ensure_daemonset"),
    ("apps", "daemonsets", "patch", "vpc_underlay._ensure_daemonset (reconcile)"),
    ("", "nodes", "patch", "vpc_underlay._label_gateway_nodes"),
    ("rbac.authorization.k8s.io", "rolebindings", "list", "groups.get_user_namespaces"),
    ("cluster.x-k8s.io", "machines", "list", "tenants_crud._enrich_with_workers"),
    # A dead worker is only replaced if something remediates it; CAPI itself
    # stays content while the infrastructure VM exists.
    ("cluster.x-k8s.io", "machinehealthchecks", "create",
     "tenants_capi._build_machine_health_check_cr"),
    ("cluster.x-k8s.io", "machinehealthchecks", "delete",
     "tenants_crud.delete_tenant (namespace teardown)"),
    ("", "resourcequotas", "list", "folders._allocated_env_quota"),
    ("", "limitranges", "create", "folders._create_environment_ns"),
    # Kamaji's control-plane pods declare no requests at all; without a
    # LimitRange to default them the tenant quota refuses the control plane.
    ("", "limitranges", "create", "tenants_crud._ensure_tenant_limit_range"),
    ("apps", "daemonsets", "get", "vpc_underlay._kubeovn_cni_image"),
    ("apps", "daemonsets", "list", "vpc_underlay.get (workaround DaemonSets)"),
    # --- Talos tenants: PKI via cert-manager for the CSR signer ---
    ("cert-manager.io", "issuers", "create", "tenants_talos.build_talos_pki"),
    ("cert-manager.io", "certificates", "create", "tenants_talos.build_talos_pki"),
    # Deleting a VPC has to watch its dependents actually disappear before it
    # removes the router they are finalized against.
    ("kubeovn.io", "subnets", "get", "vpcs._await_dependents_gone"),
    ("kubeovn.io", "ovn-eips", "get", "vpcs._await_dependents_gone"),
    ("kubeovn.io", "ovn-eips", "list", "vpcs.delete_vpc (NAT inventory)"),
    ("kubeovn.io", "ovn-snat-rules", "get", "vpcs._await_dependents_gone"),
    ("kubeovn.io", "ovn-snat-rules", "list", "vpcs.delete_vpc (NAT inventory)"),
    ("kubeovn.io", "ovn-fips", "list", "vpcs.delete_vpc (NAT inventory)"),
    ("kubeovn.io", "ovn-dnat-rules", "list", "vpcs.delete_vpc (NAT inventory)"),
    ("kubeovn.io", "vpc-egress-gateways", "get",
     "egress_gateway._await_gateway_gone"),
    # --- The operator's own custom resources -------------------------------
    # These went missing while four operator paths were already written, and
    # nothing here caught it, because this list is kept by hand and nobody
    # added the rows. `test_every_operator_call_site_is_listed` below now reads
    # the call sites and fails if that happens again.
    ("platform.kubevirt-ui.io", "managedvms", "get", "operator.patch_managed_disks"),
    ("platform.kubevirt-ui.io", "managedvms", "list", "disks (managed VM inventory)"),
    ("platform.kubevirt-ui.io", "managedvms", "create", "vms._create_managed_vm"),
    ("platform.kubevirt-ui.io", "managedvms", "patch", "operator.patch_managed_disks"),
    ("platform.kubevirt-ui.io", "managedvms", "delete", "vms.delete_vm"),
    ("platform.kubevirt-ui.io", "managedvmoperations", "create",
     "vm_actions._start_operation"),
    ("platform.kubevirt-ui.io", "managedvmtemplates", "list",
     "templates._list_template_crs"),
    ("platform.kubevirt-ui.io", "managedvmtemplates", "create",
     "templates._create_template_cr"),
    ("platform.kubevirt-ui.io", "managedvmtemplates", "delete", "templates.delete"),
    # Editing a template edits it where it lives, and for a CR-backed one that
    # is a patch — see tests/test_template_readers_agree.py.
    ("platform.kubevirt-ui.io", "managedvmtemplates", "patch",
     "templates.update_template"),
    ("platform.kubevirt-ui.io", "managedimages", "get", "templates (image lookup)"),
    ("platform.kubevirt-ui.io", "managedimages", "create",
     "templates._create_managed_image"),
    ("platform.kubevirt-ui.io", "managedimages", "delete", "templates.delete_image"),
    ("platform.kubevirt-ui.io", "managedunderlays", "get",
     "vpc_underlay.read_underlay_cr"),
    ("platform.kubevirt-ui.io", "managedunderlays", "create",
     "vpc_underlay.ensure_underlay_cr"),
    ("platform.kubevirt-ui.io", "managedunderlays", "patch",
     "vpc_underlay.ensure_underlay_cr"),
    ("platform.kubevirt-ui.io", "managednetworks", "get",
     "vpcs._managed_network_exists"),
    ("platform.kubevirt-ui.io", "managednetworks", "create",
     "vpcs._create_managed_network"),
    ("platform.kubevirt-ui.io", "managednetworks", "delete",
     "vpcs.delete_vpc (the operator cascades)"),
    # Peering under OPERATOR_PEERING_ENABLED: the endpoint describes the link
    # and the operator writes both ends. List, because deleting a VPC has to
    # know which of its peerings the operator holds.
    ("platform.kubevirt-ui.io", "managednetworkpeerings", "get",
     "vpcs._peering_cr"),
    ("platform.kubevirt-ui.io", "managednetworkpeerings", "list",
     "vpcs._claimed_remotes"),
    ("platform.kubevirt-ui.io", "managednetworkpeerings", "create",
     "vpcs._describe_peering"),
    ("platform.kubevirt-ui.io", "managednetworkpeerings", "delete",
     "vpcs.delete_vpc_peering"),
    # Enabling an addon edits the tenant's description rather than writing a
    # second HelmRelease — see tests/test_addons_have_one_writer.py.
    ("platform.kubevirt-ui.io", "managedtenants", "get",
     "tenants_crud._described_tenant"),
    ("platform.kubevirt-ui.io", "managedtenants", "patch",
     "tenants_crud._write_described_addons"),
    # Creating a tenant by describing it, under OPERATOR_TENANT_ENABLED.
    ("platform.kubevirt-ui.io", "managedtenants", "create",
     "tenants_crud._create_managed_tenant"),
    # Not gated by that flag: a tenant the operator holds must stay deletable
    # after it is turned off again.
    ("platform.kubevirt-ui.io", "managedtenants", "delete",
     "tenants_crud._delete_managed_tenant"),
]


def _chart_documents() -> list[dict]:
    """Every document the template renders, with Helm directives stripped.

    The template is plain YAML apart from `{{ }}` lines, none of which sit
    inside the rules themselves — so stripping them is enough, and avoids
    needing helm in the test image.

    Two kinds of line need different treatment, and conflating them is how
    this silently parsed only the first document for a while: a line that is
    *only* a template call (`{{- include "kubevirt-ui.labels" . }}`) leaves a
    bare scalar where a mapping is expected, so it has to go entirely, while
    an inline call (`name: {{ ... }}`) just needs a value.
    """
    raw = RBAC_TEMPLATE.read_text()
    kept: list[str] = []
    for line in raw.splitlines():
        if re.match(r"^\s*\{\{-?\s*(if|else|end|range|with)\b", line):
            continue
        substituted = re.sub(r"\{\{-?.*?-?\}\}", "placeholder", line)
        if substituted.strip() == "placeholder":
            # Whole-line call: it would render a block, not a scalar.
            continue
        kept.append(substituted)

    return [
        doc for doc in yaml.safe_load_all("\n".join(kept))
        if isinstance(doc, dict)
    ]


def _cluster_role_rules() -> list[dict]:
    """Rules of the cluster-wide ClusterRole."""
    for doc in _chart_documents():
        if doc.get("kind") == "ClusterRole":
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


# ---------------------------------------------------------------------------
# The list above is kept by hand, and for the operator group that is exactly
# what failed. These call sites are machine-readable, so read them.
# ---------------------------------------------------------------------------

_OPERATOR_GROUP = "platform.kubevirt-ui.io"

# kubernetes_asyncio method prefix -> the RBAC verb it needs.
_VERB_OF = {
    "get": "get", "list": "list", "watch": "watch", "create": "create",
    "patch": "patch", "replace": "update", "delete": "delete",
}

_CUSTOM_CALL = re.compile(
    r"(?P<verb>get|list|watch|create|patch|replace|delete)"
    r"_(?:namespaced|cluster)_custom_object\s*\((?P<args>[^)]*)\)",
    re.S,
)
_PLURAL_ARG = re.compile(
    r"plural\s*=\s*(?:\"(?P<literal>[a-z]+)\"|(?P<constant>[A-Z][A-Z_]*))"
)


def _operator_call_sites() -> set[tuple[str, str]]:
    """(resource, verb) pairs the backend issues against the operator's CRDs."""
    app = Path(__file__).resolve().parent.parent / "app"
    sources = {path: path.read_text() for path in app.rglob("*.py")}

    # Some call sites name the plural through a module constant.
    constants: dict[str, str] = {}
    for text in sources.values():
        for name, value in re.findall(
            r"^([A-Z][A-Z_]*)\s*=\s*\"(managed[a-z]+)\"", text, re.M
        ):
            constants[name] = value

    found: set[tuple[str, str]] = set()
    for text in sources.values():
        for call in _CUSTOM_CALL.finditer(text):
            arg = _PLURAL_ARG.search(call.group("args"))
            if arg is None:
                continue
            resource = arg.group("literal") or constants.get(arg.group("constant") or "", "")
            if not resource.startswith("managed"):
                continue
            found.add((resource, _VERB_OF[call.group("verb")]))
    return found


def test_the_operator_call_site_scan_finds_something() -> None:
    """A scan that matches nothing would pass the next test in silence."""
    found = _operator_call_sites()
    assert found, "no operator custom-resource call sites found — the scan is broken"
    assert ("managedunderlays", "get") in found, sorted(found)


def test_every_operator_call_site_is_listed() -> None:
    """REQUIRED must not fall behind the code for the operator group.

    Every row in REQUIRED is checked against the chart above; this closes the
    other end, where the code grows a call and the row is never written. That
    is precisely how the whole group came to be missing from the chart while
    every test here passed.
    """
    listed = {(resource, verb) for group, resource, verb, _ in REQUIRED
              if group == _OPERATOR_GROUP}
    missing = _operator_call_sites() - listed
    assert not missing, (
        f"the backend calls {sorted(missing)} on the operator's custom resources "
        f"and REQUIRED does not say so. Add a row (and the chart rule it implies) "
        f"— otherwise the call 403s on a live cluster while every test here passes."
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


# ---------------------------------------------------------------------------
# Cluster-scoped objects the backend manages by name
# ---------------------------------------------------------------------------

def _cluster_scoped_names_we_write() -> list[tuple[str, str]]:
    """(constant, name) for every cluster-scoped RBAC object the code replaces.

    The chart deliberately scopes ClusterRole writes with `resourceNames`,
    which is the right call — and it means every new managed name has to be
    added there or the write is refused at the cluster scope. That is not
    something to remember; it is something to check.
    """
    from app.api.v1 import tenants_storage

    return [("CSI_CLUSTER_ROLE_NAME", tenants_storage.CSI_CLUSTER_ROLE_NAME)]


def _write_allowlist(rules: list[dict]) -> set[str]:
    names: set[str] = set()
    for rule in rules:
        if "clusterroles" not in (rule.get("resources") or []):
            continue
        verbs = set(rule.get("verbs") or [])
        if not verbs & {"update", "patch", "*"}:
            continue
        listed = rule.get("resourceNames")
        if listed is None:
            # An unscoped write rule covers everything.
            return {"*"}
        names.update(listed)
    return names


@pytest.mark.parametrize("constant,name", _cluster_scoped_names_we_write())
def test_managed_cluster_roles_are_writable(
    rules: list[dict], constant: str, name: str,
) -> None:
    """On the cluster the omission reads:

        clusterroles.rbac.authorization.k8s.io "kubevirt-csi-cluster" is
        forbidden: User "system:serviceaccount:kubevirt-ui-system:kubevirt-ui"
        cannot update resource "clusterroles" ... at the cluster scope

    and it lands mid-way through tenant creation, after the namespace and
    several identities already exist.
    """
    allowed = _write_allowlist(rules)
    assert "*" in allowed or name in allowed, (
        f"{constant} = {name!r} is replaced by the backend but is not in the "
        f"ClusterRole write allowlist in helm/kubevirt-ui/templates/rbac.yaml "
        f"(resourceNames of the clusterroles update/patch/delete rule)."
    )


# ---------------------------------------------------------------------------
# Namespaced grants
# ---------------------------------------------------------------------------
# The list above only sees the cluster-wide ClusterRole. A permission granted
# by a namespaced Role is invisible to it — which is how the B3 announcement
# shipped with no grant at all: nobody could add a row that would have caught
# it, because the row had nowhere to live.
#
# Rows here are permissions the backend needs in a namespace that is NOT the
# release namespace, named by a value.

# (api group, resource, verb, who needs it)
REQUIRED_NAMESPACED: list[tuple[str, str, str, str]] = [
    ("frrk8s.metallb.io", "frrconfigurations", "get", "b3_announce.apply"),
    ("frrk8s.metallb.io", "frrconfigurations", "list", "bgp.get_b3_state"),
    ("frrk8s.metallb.io", "frrconfigurations", "create", "b3_announce.apply"),
    ("frrk8s.metallb.io", "frrconfigurations", "patch", "b3_announce.apply"),
]


def _role_rules() -> list[dict]:
    """Rules of every namespaced Role the chart renders."""
    rules: list[dict] = []
    for doc in _chart_documents():
        if doc.get("kind") == "Role":
            rules.extend(doc.get("rules") or [])
    return rules


@pytest.fixture(scope="module")
def namespaced_rules() -> list[dict]:
    return _role_rules()


@pytest.mark.parametrize(
    "group,resource,verb,caller",
    REQUIRED_NAMESPACED,
    ids=[f"{g}/{r}:{v}" for g, r, v, _ in REQUIRED_NAMESPACED],
)
def test_chart_grants_namespaced_permission(
    namespaced_rules: list[dict], group: str, resource: str, verb: str,
    caller: str,
) -> None:
    assert _granted(namespaced_rules, group, resource, verb), (
        f"{caller} calls {verb} on {group}/{resource}, but no Role in the "
        f"chart grants it. The write 403s in a loop on a live cluster while "
        f"the UI still reports the announcement as handed to FRR. Add the "
        f"verb to the -b3 Role in helm/kubevirt-ui/templates/rbac.yaml."
    )


def test_the_b3_role_does_not_ask_for_delete(namespaced_rules) -> None:
    # Withdrawing an announcement rewrites the object to zero prefixes; the
    # object itself is never removed (measured when every VPC was torn down
    # in UAT 2026-08-19). Asking for delete would widen the grant for a call
    # that does not exist.
    for rule in namespaced_rules:
        if "frrconfigurations" in (rule.get("resources") or []):
            assert "delete" not in (rule.get("verbs") or []), (
                "the B3 Role asks for delete, but withdrawing an announcement "
                "rewrites the FRRConfiguration to zero prefixes rather than "
                "deleting it — drop the verb or prove the call exists"
            )


def test_the_b3_role_and_the_backend_env_name_one_namespace() -> None:
    """A Role in one namespace and a write to another grant nothing.

    Both must resolve through `kubevirt-ui.b3FrrNamespace`. A second copy of
    the value would break this silently: RBAC would look correct on review
    and still 403 in production. The helper also keeps the env from being
    emitted twice when a site sets it through `backend.env`, which Kubernetes
    resolves by taking the last entry — quietly.
    """
    rbac = RBAC_TEMPLATE.read_text()
    deployment = (RBAC_TEMPLATE.parent / "backend-deployment.yaml").read_text()
    helper = 'include "kubevirt-ui.b3FrrNamespace"'

    assert helper in rbac, (
        "the B3 Role must take its namespace from the shared helper, not from "
        "a value read directly — the two readers would drift"
    )
    assert helper in deployment, (
        "B3_FRR_NAMESPACE must come from the same helper the Role does"
    )

    lines = deployment.splitlines()
    # The name line, not the `if` guard that also mentions the variable.
    idx = next(
        i for i, line in enumerate(lines)
        if line.strip() == "- name: B3_FRR_NAMESPACE"
    )
    assert helper in lines[idx + 1], (
        f"B3_FRR_NAMESPACE is set from {lines[idx + 1].strip()!r} rather than "
        f"the helper — the grant and the write can now name different "
        f"namespaces"
    )
    assert "if not (and .Values.backend.env" in deployment, (
        "the env must be skipped when backend.env already carries it, or the "
        "container gets two entries of the same name"
    )


# ---------------------------------------------------------------------------
# The other half: everything the code calls, not only what somebody remembered
# ---------------------------------------------------------------------------
#
# REQUIRED is a curated list — each row says who calls it and why it matters.
# The scan above closes the loop for the operator's own group, and only that
# group, because it keys on the `managed` prefix in the plural.
#
# That blind spot is where a real hole lived: the BGP page reads
# `bgpsessionstates` and `frrnodestates`, the chart granted neither, and both
# refusals were swallowed — one `logger.debug`, one bare `continue` — so a 403
# reached the user as "No session state reported yet", a fact about the cluster
# rather than an error. The chart did grant `frrconfigurations`: permission to
# write the config and none to read the result.
#
# This asks the whole question instead: for every custom-resource call in the
# backend, is there a rule that allows it? No table to keep in step — the code
# is the input.

_GROUP_ARG = re.compile(
    r"group\s*=\s*(?:\"(?P<literal>[a-z0-9.\-]+)\"|(?P<constant>[A-Za-z_][A-Za-z_0-9]*))"
)

# Call sites whose group or plural is computed at run time. Named one by one,
# with the reason, because "the scan could not read it" and "the scan found
# nothing to read" must not look the same.
_DYNAMIC_CALL_SITES = {
    ("?", "?"): "velero and cert-manager plurals chosen by the caller",
    ("?", "virtualmachines"): "group from a module constant built per call",
    ("cert-manager.io", "?"): "certificates and issuers through one helper",
    ("kubeovn.io", "?"): "the underlay helper takes the plural as an argument",
    ("velero.io", "?"): "backup and restore through one helper",
}


def _all_rbac_rules() -> list[dict]:
    """Every rule the chart grants, from the ClusterRole and the Roles alike.

    Both, because `bgpsessionstates` is namespaced and lives in a Role — read
    only the ClusterRole and the hole this test exists for stays invisible.
    """
    rules: list[dict] = []
    for doc in _chart_documents():
        if doc.get("kind") in ("ClusterRole", "Role"):
            rules.extend(doc.get("rules") or [])
    return rules


def _custom_resource_calls() -> set[tuple[str, str, str]]:
    """(group, resource, verb) for every custom-object call in the backend."""
    app = Path(__file__).resolve().parent.parent / "app"
    sources = {path: path.read_text() for path in app.rglob("*.py")}

    constants: dict[str, str] = {}
    for text in sources.values():
        for name, value in re.findall(
            r"^([A-Z][A-Z_0-9]*)\s*=\s*\"([a-z0-9.\-]+)\"", text, re.M
        ):
            constants[name] = value

    found: set[tuple[str, str, str]] = set()
    for text in sources.values():
        for call in _CUSTOM_CALL.finditer(text):
            args = call.group("args")
            plural = _PLURAL_ARG.search(args)
            if plural is None:
                continue
            group_arg = _GROUP_ARG.search(args)
            group = "?"
            if group_arg is not None:
                group = (group_arg.group("literal")
                         or constants.get(group_arg.group("constant") or "", "?"))
            resource = (plural.group("literal")
                        or constants.get(plural.group("constant") or "", "?"))
            found.add((group, resource, _VERB_OF[call.group("verb")]))
    return found


def test_the_chart_grants_every_custom_resource_call() -> None:
    rules = _all_rbac_rules()
    calls = _custom_resource_calls()
    assert len(calls) > 100, f"the scan found only {len(calls)} calls — it is broken"

    ungranted = sorted(
        (group, resource, verb) for group, resource, verb in calls
        if (group, resource) not in _DYNAMIC_CALL_SITES
        and not _granted(rules, group, resource, verb)
    )
    assert not ungranted, (
        "the backend makes these calls and the chart grants none of them — each "
        "403s on a live cluster, and a swallowed 403 reads as an empty answer:\n  "
        + "\n  ".join(f"{g}/{r}: {v}" for g, r, v in ungranted)
    )


def test_no_new_call_site_hides_behind_a_variable() -> None:
    """A call whose group or plural the scan cannot read is not covered by the
    test above, so the set of them is pinned. Growing it is a decision."""
    calls = _custom_resource_calls()
    unreadable = {(g, r) for g, r, _ in calls if g == "?" or r == "?"}
    unexpected = unreadable - set(_DYNAMIC_CALL_SITES)
    assert not unexpected, (
        f"new call sites the scan cannot read: {sorted(unexpected)}. Either name "
        "the group and plural literally, or add them above with the reason."
    )
