"""kubevirt-csi tenant storage provisioning.

Creates the host-side resources that let a tenant cluster's CSI driver
authenticate against the host apiserver and create DataVolumes / PVCs in
the tenant namespace.

The flow when `enable_storage=True`:

  1. ServiceAccount `kubevirt-csi` in `tenant-<name>` ns
  2. Role + RoleBinding scoped to the tenant ns granting the verbs the CSI
     driver actually uses (DataVolume create/get/delete, PVC get/patch,
     VMI/VirtualMachine list/get, VM addvolume/removevolume subresource,
     VolumeSnapshot create/get/delete). Verbs deliberately match upstream
     kubevirt/csi-driver `deploy/infra-cluster-service-account.yaml` —
     deviating breaks the controller.
  3. TokenRequest minting a bound token for the SA
  4. Secret `infra-cluster-credentials` containing a kubeconfig assembled
     from (external API URL, cluster CA, SA token) — this is the secret
     the kubevirt-csi HelmRelease consumes in the tenant CP.
  5. A second, host-side-only identity `capk-infra` (SA + Role +
     RoleBinding + token + Secret `capk-infra-credentials`) with a wider
     rule set, referenced by `KubevirtCluster.spec.infraClusterSecretRef`.
     See the CAPK_* block below for why CAPK cannot reuse (2).
  6. ResourceQuota in the tenant ns capping `persistentvolumeclaims` and
     `requests.storage`.

All steps are idempotent (409 / read-before-create) so re-running a
tenant create after partial failure doesn't error.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Callable
from typing import Any

import yaml
from fastapi import HTTPException
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiException

from app.core.kube_api_url import discover_external_api_url
from app.models.tenant import TenantCreateRequest, TenantStorageStatus

from app.api.v1.tenants_common import _tenant_ns

logger = logging.getLogger(__name__)


CSI_SA_NAME = "kubevirt-csi"
CSI_ROLE_NAME = "kubevirt-csi"
CSI_ROLE_BINDING_NAME = "kubevirt-csi"
# Cluster-scoped read on PVs — shared single ClusterRole across all
# tenants; the binding is per-tenant so it can be GC'd on tenant delete
# (cluster-scoped objects don't cascade with the tenant ns).
CSI_CLUSTER_ROLE_NAME = "kubevirt-csi-cluster"
CSI_KUBECONFIG_SECRET_NAME = "infra-cluster-credentials"
CSI_SA_TOKEN_SECRET_NAME = "kubevirt-csi-token"
CSI_RESOURCE_QUOTA_NAME = "tenant-storage"

# Setting KubevirtCluster.spec.infraClusterSecretRef makes CAPK stop using
# its own manager identity and speak to the host cluster as whatever that
# secret holds — for *everything*: reading the bootstrap Secret, creating
# the cloud-init Secret, creating the worker VirtualMachines. The CSI
# identity above cannot be reused for that: its kubeconfig is replicated
# INTO the tenant cluster (kube-system/infra-cluster-credentials), where
# any tenant admin can read it, so it must stay scoped to volume
# operations. Hence a second, host-side-only identity for CAPK.
CAPK_SA_NAME = "capk-infra"
CAPK_ROLE_NAME = "capk-infra"
CAPK_ROLE_BINDING_NAME = "capk-infra"
CAPK_SA_TOKEN_SECRET_NAME = "capk-infra-token"
CAPK_KUBECONFIG_SECRET_NAME = "capk-infra-credentials"

# Namespace inside the TENANT cluster where the kubevirt-csi-driver chart
# is released (matches the bootstrap component's `namespace:` field). The
# controller pod mounts `infra-cluster-credentials` from here, so the
# host-side secret must be replicated into this tenant ns. kube-system
# always exists → no namespace creation needed.
TENANT_DRIVER_NAMESPACE = "kube-system"

# The CSI controller in the tenant has no token-refresh path, so we use a
# legacy non-expiring ServiceAccount-token Secret (type
# kubernetes.io/service-account-token) instead of a bounded TokenRequest.
# Its lifetime == the SA's lifetime == the tenant ns lifetime: deleting the
# tenant deletes the ns → SA → token, no orphaned credential. Revocation is
# instant (token validated against the owning SA on every call, deleting
# the SA invalidates it). The kube-controller-manager token controller
# populates `.data.token` asynchronously; we poll briefly for it.
CSI_TOKEN_POLL_ATTEMPTS = 20
CSI_TOKEN_POLL_INTERVAL_SEC = 0.5


def _csi_role_rules() -> list[dict[str, Any]]:
    """Namespaced RBAC for the kubevirt-csi controller (in tenant-<name> ns).

    Matches the proven working set verified during the storage-passthrough
    bring-up (see csi-path/kubevirt-csi-manifests/01-infra-sa.yaml): the
    driver lists/watches the DataVolumes + PVCs it manages, gets/lists VMs
    & VMIs to map nodes, and updates the addvolume/removevolume
    subresources to hotplug disks. Cluster-scoped `persistentvolumes: get`
    lives in a separate ClusterRole (see _ensure_cluster_role).
    """
    return [
        {
            "apiGroups": ["cdi.kubevirt.io"],
            "resources": ["datavolumes"],
            "verbs": ["get", "create", "delete", "list", "watch"],
        },
        {
            "apiGroups": ["kubevirt.io"],
            "resources": ["virtualmachineinstances", "virtualmachines"],
            "verbs": ["list", "get"],
        },
        {
            "apiGroups": ["subresources.kubevirt.io"],
            "resources": ["virtualmachines/addvolume", "virtualmachines/removevolume"],
            "verbs": ["update"],
        },
        {
            "apiGroups": ["snapshot.storage.k8s.io"],
            "resources": ["volumesnapshots"],
            "verbs": ["get", "create", "delete"],
        },
        {
            "apiGroups": [""],
            "resources": ["persistentvolumeclaims"],
            "verbs": ["get", "list", "watch", "patch"],
        },
    ]


def _capk_role_rules() -> list[dict[str, Any]]:
    """Namespaced RBAC for CAPK acting on the host cluster (tenant-<name> ns).

    Only consulted when tenant storage is enabled, because that is the only
    case where we set `infraClusterSecretRef` and thereby take CAPK off its
    own (cluster-admin-ish) manager identity. Everything CAPK does per
    machine happens inside the tenant namespace:

      * reads the CABPK bootstrap Secret and writes the cloud-init Secret
      * creates/deletes the worker VirtualMachine and watches its VMI
      * reads the virt-launcher Pod to find the VMI's address
      * emits Events

    Without at least secrets:get and virtualmachines:create the machines
    sit at `VMProvisioned=False(WaitingForBootstrapData)` forever with no
    VMI ever created and nothing logged on the tenant side — the failure is
    entirely silent from the tenant's point of view.
    """
    return [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
        },
        {
            "apiGroups": ["kubevirt.io"],
            "resources": ["virtualmachines", "virtualmachineinstances"],
            "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
        },
        {
            "apiGroups": [""],
            "resources": ["pods", "services", "persistentvolumeclaims"],
            "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
        },
        {
            "apiGroups": ["cdi.kubevirt.io"],
            "resources": ["datavolumes"],
            "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
        },
        {
            "apiGroups": [""],
            "resources": ["events"],
            "verbs": ["create", "patch"],
        },
    ]


def _build_infra_kubeconfig(
    api_server_url: str,
    ca_data_b64: str,
    sa_token: str,
    tenant_ns: str,
    sa_name: str = CSI_SA_NAME,
) -> str:
    """Assemble a host-cluster kubeconfig for one of the two SA identities.

    Used for both `kubevirt-csi` (consumed by the tenant-CP CSI driver) and
    `capk-infra` (consumed by CAPK in the host cluster) — same shape, only
    `sa_name`/token differ.

    Mirrors upstream `deploy/example/infracluster-kubeconfig.yaml`. The
    `namespace` is set so the driver doesn't have to be told twice (the
    `infraClusterNamespace` ConfigMap value is the authoritative source
    inside the driver, but kubeconfig context.namespace is what `kubectl`
    impersonation paths use).
    """
    cluster_entry: dict[str, Any] = {"server": api_server_url}
    if ca_data_b64:
        cluster_entry["certificate-authority-data"] = ca_data_b64
    else:
        # No CA available — only happens when the in-cluster CA file is
        # missing (unusual). Falling back to insecure-skip-tls-verify is
        # better than failing the whole tenant create.
        cluster_entry["insecure-skip-tls-verify"] = True

    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "infra-cluster", "cluster": cluster_entry}],
        "contexts": [{
            "name": "only-context",
            "context": {
                "cluster": "infra-cluster",
                "user": sa_name,
                "namespace": tenant_ns,
            },
        }],
        "current-context": "only-context",
        "preferences": {},
        "users": [{
            "name": sa_name,
            "user": {"token": sa_token},
        }],
    }
    return yaml.safe_dump(kubeconfig, default_flow_style=False, sort_keys=False)


def _read_host_ca_b64(k8s) -> str:
    """Read the in-cluster CA cert file and return it base64-encoded.

    The kubernetes_asyncio Configuration object stores the CA path; the
    file is the apiserver's self-signed cert (or the kubeadm CA) which
    is exactly what we want the tenant-CP CSI driver to trust.
    """
    config = k8s._api_client.configuration
    ca_path = config.ssl_ca_cert
    if not ca_path:
        return ""
    try:
        with open(ca_path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except (OSError, IOError) as e:
        logger.warning(
            f"Failed to read host cluster CA from {ca_path!r}: {e}; "
            "CSI kubeconfig will use insecure-skip-tls-verify=true"
        )
        return ""


async def _ensure_service_account(
    core_api, ns: str, tenant_name: str,
    sa_name: str = CSI_SA_NAME, role_label: str = "csi-infra",
) -> None:
    try:
        await core_api.read_namespaced_service_account(
            name=sa_name, namespace=ns,
        )
        return
    except ApiException as e:
        if e.status != 404:
            raise
    body = client.V1ServiceAccount(
        metadata=client.V1ObjectMeta(
            name=sa_name,
            namespace=ns,
            labels={
                "kubevirt-ui.io/managed": "true",
                "kubevirt-ui.io/tenant": tenant_name,
                "kubevirt-ui.io/role": role_label,
            },
        ),
    )
    try:
        await core_api.create_namespaced_service_account(namespace=ns, body=body)
    except ApiException as e:
        if e.status != 409:
            raise


async def _ensure_role(
    rbac_api, ns: str, tenant_name: str,
    role_name: str = CSI_ROLE_NAME,
    rules_fn: Callable[[], list[dict[str, Any]]] = _csi_role_rules,
    role_label: str = "csi-infra",
) -> None:
    rules = [
        client.V1PolicyRule(
            api_groups=r["apiGroups"],
            resources=r["resources"],
            verbs=r["verbs"],
        )
        for r in rules_fn()
    ]
    metadata = client.V1ObjectMeta(
        name=role_name,
        namespace=ns,
        labels={
            "kubevirt-ui.io/managed": "true",
            "kubevirt-ui.io/tenant": tenant_name,
            "kubevirt-ui.io/role": role_label,
        },
    )
    try:
        existing = await rbac_api.read_namespaced_role(name=role_name, namespace=ns)
        # Replace rules so RBAC stays in sync if we change the rule set across releases
        existing.rules = rules
        await rbac_api.replace_namespaced_role(
            name=role_name, namespace=ns, body=existing,
        )
        return
    except ApiException as e:
        if e.status != 404:
            raise
    body = client.V1Role(metadata=metadata, rules=rules)
    try:
        await rbac_api.create_namespaced_role(namespace=ns, body=body)
    except ApiException as e:
        if e.status != 409:
            raise


async def _ensure_role_binding(
    rbac_api, ns: str, tenant_name: str,
    binding_name: str = CSI_ROLE_BINDING_NAME,
    role_name: str = CSI_ROLE_NAME,
    sa_name: str = CSI_SA_NAME,
    role_label: str = "csi-infra",
) -> None:
    metadata = client.V1ObjectMeta(
        name=binding_name,
        namespace=ns,
        labels={
            "kubevirt-ui.io/managed": "true",
            "kubevirt-ui.io/tenant": tenant_name,
            "kubevirt-ui.io/role": role_label,
        },
    )
    role_ref = client.V1RoleRef(
        api_group="rbac.authorization.k8s.io",
        kind="Role",
        name=role_name,
    )
    subject = client.RbacV1Subject(
        kind="ServiceAccount",
        name=sa_name,
        namespace=ns,
    )
    body = client.V1RoleBinding(
        metadata=metadata, role_ref=role_ref, subjects=[subject],
    )
    try:
        await rbac_api.create_namespaced_role_binding(namespace=ns, body=body)
    except ApiException as e:
        if e.status != 409:
            raise


def _csi_cluster_binding_name(tenant_name: str) -> str:
    return f"kubevirt-csi-cluster-binding-{tenant_name}"


async def _ensure_cluster_role(rbac_api) -> None:
    """Shared ClusterRole granting `persistentvolumes: get` to the driver.

    The kubevirt-csi controller reads PV objects (cluster-scoped) to map a
    tenant PV to its backing infra DataVolume. One ClusterRole serves all
    tenants; per-tenant bindings attach each tenant's SA to it.
    """
    rules = [client.V1PolicyRule(
        api_groups=[""], resources=["persistentvolumes"], verbs=["get"],
    )]
    metadata = client.V1ObjectMeta(
        name=CSI_CLUSTER_ROLE_NAME,
        labels={"kubevirt-ui.io/managed": "true", "kubevirt-ui.io/role": "csi-infra"},
    )
    try:
        existing = await rbac_api.read_cluster_role(name=CSI_CLUSTER_ROLE_NAME)
        existing.rules = rules
        await rbac_api.replace_cluster_role(name=CSI_CLUSTER_ROLE_NAME, body=existing)
        return
    except ApiException as e:
        if e.status != 404:
            raise
    body = client.V1ClusterRole(metadata=metadata, rules=rules)
    try:
        await rbac_api.create_cluster_role(body=body)
    except ApiException as e:
        if e.status != 409:
            raise


async def _ensure_cluster_role_binding(rbac_api, ns: str, tenant_name: str) -> None:
    """Per-tenant ClusterRoleBinding: tenant SA → shared CSI ClusterRole."""
    name = _csi_cluster_binding_name(tenant_name)
    metadata = client.V1ObjectMeta(
        name=name,
        labels={
            "kubevirt-ui.io/managed": "true",
            "kubevirt-ui.io/tenant": tenant_name,
            "kubevirt-ui.io/role": "csi-infra",
        },
    )
    body = client.V1ClusterRoleBinding(
        metadata=metadata,
        role_ref=client.V1RoleRef(
            api_group="rbac.authorization.k8s.io",
            kind="ClusterRole",
            name=CSI_CLUSTER_ROLE_NAME,
        ),
        subjects=[client.RbacV1Subject(
            kind="ServiceAccount", name=CSI_SA_NAME, namespace=ns,
        )],
    )
    try:
        await rbac_api.create_cluster_role_binding(body=body)
    except ApiException as e:
        if e.status != 409:
            raise


async def delete_csi_cluster_role_binding(k8s, tenant_name: str) -> None:
    """Delete the per-tenant CSI ClusterRoleBinding (cluster-scoped → no
    cascade with the tenant ns). Best-effort; 404 tolerated. Called from
    tenant teardown."""
    rbac_api = client.RbacAuthorizationV1Api(k8s._api_client)
    name = _csi_cluster_binding_name(tenant_name)
    try:
        await rbac_api.delete_cluster_role_binding(name=name)
    except ApiException as e:
        if e.status != 404:
            logger.warning(
                f"Failed to delete CSI ClusterRoleBinding {name!r}: {e}"
            )


async def _ensure_sa_token_secret(
    core_api, ns: str, tenant_name: str,
    secret_name: str = CSI_SA_TOKEN_SECRET_NAME,
    sa_name: str = CSI_SA_NAME,
    role_label: str = "csi-infra",
) -> str:
    """Create (idempotently) a non-expiring SA-token Secret and return token.

    A Secret of type ``kubernetes.io/service-account-token`` annotated with
    the SA name makes the token controller mint a long-lived token bound to
    `kubevirt-csi`. Unlike TokenRequest the token never expires on its own —
    it dies only when the SA / namespace is deleted (tenant teardown). The
    default audience is the apiserver itself, which is exactly what the CSI
    driver authenticates against.

    The token controller populates `.data.token` a moment after the Secret
    is created, so we poll until it appears.
    """
    metadata = client.V1ObjectMeta(
        name=secret_name,
        namespace=ns,
        annotations={"kubernetes.io/service-account.name": sa_name},
        labels={
            "kubevirt-ui.io/managed": "true",
            "kubevirt-ui.io/tenant": tenant_name,
            "kubevirt-ui.io/role": role_label,
        },
    )
    body = client.V1Secret(
        metadata=metadata,
        type="kubernetes.io/service-account-token",
    )
    try:
        await core_api.create_namespaced_secret(namespace=ns, body=body)
    except ApiException as e:
        if e.status != 409:
            raise  # already exists → fall through to read the token

    for _ in range(CSI_TOKEN_POLL_ATTEMPTS):
        secret = await core_api.read_namespaced_secret(
            name=secret_name, namespace=ns,
        )
        token_b64 = (secret.data or {}).get("token")
        if token_b64:
            return base64.b64decode(token_b64).decode("utf-8")
        await asyncio.sleep(CSI_TOKEN_POLL_INTERVAL_SEC)

    raise RuntimeError(
        f"SA-token Secret {secret_name!r} in {ns!r} was not "
        "populated with a token by the controller within "
        f"{CSI_TOKEN_POLL_ATTEMPTS * CSI_TOKEN_POLL_INTERVAL_SEC:.0f}s"
    )


async def _ensure_kubeconfig_secret(
    core_api,
    ns: str,
    tenant_name: str,
    api_server_url: str,
    ca_data_b64: str,
    sa_token: str,
    secret_name: str = CSI_KUBECONFIG_SECRET_NAME,
    sa_name: str = CSI_SA_NAME,
    role_label: str = "csi-infra",
) -> None:
    kubeconfig_str = _build_infra_kubeconfig(
        api_server_url=api_server_url,
        ca_data_b64=ca_data_b64,
        sa_token=sa_token,
        tenant_ns=ns,
        sa_name=sa_name,
    )
    data = {"kubeconfig": base64.b64encode(kubeconfig_str.encode("utf-8")).decode("ascii")}
    metadata = client.V1ObjectMeta(
        name=secret_name,
        namespace=ns,
        labels={
            "kubevirt-ui.io/managed": "true",
            "kubevirt-ui.io/tenant": tenant_name,
            "kubevirt-ui.io/role": role_label,
        },
    )
    body = client.V1Secret(metadata=metadata, type="Opaque", data=data)
    try:
        existing = await core_api.read_namespaced_secret(
            name=secret_name, namespace=ns,
        )
        # Always refresh the kubeconfig payload — the SA token changes on
        # every create call (we mint a new one) and we want the Secret to
        # reflect that so the tenant CP picks up the new credentials.
        existing.data = data
        await core_api.replace_namespaced_secret(
            name=secret_name, namespace=ns, body=existing,
        )
        return
    except ApiException as e:
        if e.status != 404:
            raise
    try:
        await core_api.create_namespaced_secret(namespace=ns, body=body)
    except ApiException as e:
        if e.status != 409:
            raise


async def _ensure_resource_quota(
    core_api,
    ns: str,
    tenant_name: str,
    pvc_count: int,
    storage_gi: int,
) -> None:
    hard = {
        "persistentvolumeclaims": str(pvc_count),
        "requests.storage": f"{storage_gi}Gi",
    }
    metadata = client.V1ObjectMeta(
        name=CSI_RESOURCE_QUOTA_NAME,
        namespace=ns,
        labels={
            "kubevirt-ui.io/managed": "true",
            "kubevirt-ui.io/tenant": tenant_name,
            "kubevirt-ui.io/role": "csi-infra",
        },
    )
    body = client.V1ResourceQuota(
        metadata=metadata,
        spec=client.V1ResourceQuotaSpec(hard=hard),
    )
    try:
        existing = await core_api.read_namespaced_resource_quota(
            name=CSI_RESOURCE_QUOTA_NAME, namespace=ns,
        )
        existing.spec = client.V1ResourceQuotaSpec(hard=hard)
        await core_api.replace_namespaced_resource_quota(
            name=CSI_RESOURCE_QUOTA_NAME, namespace=ns, body=existing,
        )
        return
    except ApiException as e:
        if e.status != 404:
            raise
    try:
        await core_api.create_namespaced_resource_quota(namespace=ns, body=body)
    except ApiException as e:
        if e.status != 409:
            raise


async def create_csi_infrastructure_resources(
    k8s, req: TenantCreateRequest,
) -> dict[str, str]:
    """Provision host-side kubevirt-csi resources for a tenant.

    Idempotent. Returns a dict the CAPI builders use to wire the
    KubevirtCluster CR's `infraClusterSecretRef` (the only CAPK field
    we set here — storage class is plumbed via the addon chart values,
    not the cluster CR).

    Caller must guarantee the tenant ns already exists.
    """
    ns = _tenant_ns(req.name)
    core_api = k8s.core_api
    rbac_api = client.RbacAuthorizationV1Api(k8s._api_client)

    # 1+2+3. SA / Role / RoleBinding (must be created in this order so the
    # SA exists before the token Secret, and the binding has a target Role).
    await _ensure_service_account(core_api, ns, req.name)
    await _ensure_role(rbac_api, ns, req.name)
    await _ensure_role_binding(rbac_api, ns, req.name)

    # 3b. Cluster-scoped PV read (shared ClusterRole + per-tenant binding).
    await _ensure_cluster_role(rbac_api)
    await _ensure_cluster_role_binding(rbac_api, ns, req.name)

    # 4. Ensure a non-expiring SA-token Secret and read the token. Stable
    #    across re-runs (Secret persists with the tenant ns).
    sa_token = await _ensure_sa_token_secret(core_api, ns, req.name)

    # 5. Resolve the external apiserver URL + cluster CA cert.
    #    Fail-fast on fallback: the fallback (`kubernetes.default.svc`)
    #    resolves to the TENANT apiserver from inside a tenant pod, NOT the
    #    host — a kubeconfig built with it is silently broken (the CSI
    #    controller would auth against the wrong cluster). Better to abort
    #    tenant create with an actionable error than ship a dead driver.
    #    Caller (create_tenant) preserves this HTTPException and rolls back
    #    the half-created tenant.
    api_server_url, source = await discover_external_api_url(k8s)
    if source == "fallback":
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot provision tenant storage: the host apiserver's "
                "externally-reachable URL could not be determined. Set the "
                "KUBE_API_EXTERNAL_URL env var on the backend, or publish the "
                "kube-public/cluster-info ConfigMap with the host control-plane "
                "VIP, then retry. (Create the tenant without storage to skip "
                "this requirement.)"
            ),
        )
    ca_data_b64 = _read_host_ca_b64(k8s)

    # 6. Secret with kubeconfig
    await _ensure_kubeconfig_secret(
        core_api, ns, req.name, api_server_url, ca_data_b64, sa_token,
    )

    # 6b. The separate CAPK identity (see CAPK_SA_NAME above). Same shape,
    #     different rule set, and its kubeconfig stays in the host cluster —
    #     only the CSI one is replicated into the tenant.
    await _ensure_service_account(
        core_api, ns, req.name, sa_name=CAPK_SA_NAME, role_label="capk-infra",
    )
    await _ensure_role(
        rbac_api, ns, req.name,
        role_name=CAPK_ROLE_NAME, rules_fn=_capk_role_rules,
        role_label="capk-infra",
    )
    await _ensure_role_binding(
        rbac_api, ns, req.name,
        binding_name=CAPK_ROLE_BINDING_NAME, role_name=CAPK_ROLE_NAME,
        sa_name=CAPK_SA_NAME, role_label="capk-infra",
    )
    capk_token = await _ensure_sa_token_secret(
        core_api, ns, req.name,
        secret_name=CAPK_SA_TOKEN_SECRET_NAME, sa_name=CAPK_SA_NAME,
        role_label="capk-infra",
    )
    await _ensure_kubeconfig_secret(
        core_api, ns, req.name, api_server_url, ca_data_b64, capk_token,
        secret_name=CAPK_KUBECONFIG_SECRET_NAME, sa_name=CAPK_SA_NAME,
        role_label="capk-infra",
    )

    # 7. ResourceQuota
    await _ensure_resource_quota(
        core_api, ns, req.name,
        pvc_count=req.storage_pvc_count,
        storage_gi=req.storage_quota_gi,
    )

    logger.info(
        f"Provisioned kubevirt-csi host-side resources for tenant {req.name!r} "
        f"in {ns}: SA={CSI_SA_NAME} Secret={CSI_KUBECONFIG_SECRET_NAME} "
        f"Quota={CSI_RESOURCE_QUOTA_NAME} api_url_source={source}"
    )
    return {
        "secret_name": CSI_KUBECONFIG_SECRET_NAME,
        "secret_namespace": ns,
        # What KubevirtCluster.spec.infraClusterSecretRef must point at.
        # Deliberately NOT the CSI secret — see the CAPK_* block at the top.
        "capk_secret_name": CAPK_KUBECONFIG_SECRET_NAME,
        # NOTE: storage class is NOT plumbed through KubevirtCluster.spec —
        # the CAPK v1alpha1 schema has no `infraClusterStorageClassName`
        # field. Tenant SC selection happens in the kubevirt-csi-driver
        # HelmRelease values via the INFRA_STORAGE_CLASS_NAME addon param.
    }


async def _build_tenant_core_api(k8s, tenant_name: str):
    """Build a CoreV1Api talking to the tenant cluster, or None if not ready.

    Uses the in-cluster-reachable `super-admin.svc` kubeconfig from the
    Kamaji `<tenant>-admin-kubeconfig` secret (points at the TCP Service,
    reachable from the backend pod in the host cluster). Caller must close
    the returned client's `api_client`.
    """
    ns = _tenant_ns(tenant_name)
    try:
        sec = await k8s.core_api.read_namespaced_secret(
            name=f"{tenant_name}-admin-kubeconfig", namespace=ns,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise
    data = sec.data or {}
    # Prefer the in-cluster Service endpoint; the external (ingress) variants
    # may not be routable from the backend pod.
    raw = data.get("super-admin.svc") or data.get("admin.svc")
    if not raw:
        return None
    try:
        kc_dict = yaml.safe_load(base64.b64decode(raw).decode("utf-8"))
        api_client = await config.new_client_from_config_dict(kc_dict)
    except Exception as e:  # malformed kubeconfig / unreachable
        logger.warning(
            f"Could not build tenant client for {tenant_name!r}: {e}"
        )
        return None
    return client.CoreV1Api(api_client)


async def replicate_csi_credentials_to_tenant(k8s, tenant_name: str) -> bool:
    """Copy `infra-cluster-credentials` into the tenant cluster kube-system ns.

    The CSI controller pod runs in the tenant and mounts this secret to talk
    to the host apiserver. The host-side copy (created by
    `create_csi_infrastructure_resources`) is consumed by CAPK; this is the
    second consumer, in a different cluster.

    Idempotent and re-runnable (create→409→replace). Returns True on success,
    False when the tenant isn't reachable yet (cold control plane) — callers
    treat that as non-fatal and retry from the storage-reconcile path.
    """
    ns = _tenant_ns(tenant_name)
    try:
        infra_sec = await k8s.core_api.read_namespaced_secret(
            name=CSI_KUBECONFIG_SECRET_NAME, namespace=ns,
        )
    except ApiException as e:
        if e.status == 404:
            logger.warning(
                f"Cannot replicate CSI creds for {tenant_name!r}: host-side "
                f"secret {CSI_KUBECONFIG_SECRET_NAME!r} missing in {ns!r}"
            )
            return False
        raise
    payload = (infra_sec.data or {}).get("kubeconfig")
    if not payload:
        return False

    tenant_core = await _build_tenant_core_api(k8s, tenant_name)
    if tenant_core is None:
        logger.info(
            f"Tenant {tenant_name!r} not reachable yet — deferring CSI "
            "credential replication to the storage-reconcile path"
        )
        return False

    try:
        metadata = client.V1ObjectMeta(
            name=CSI_KUBECONFIG_SECRET_NAME,
            namespace=TENANT_DRIVER_NAMESPACE,
            labels={"kubevirt-ui.io/managed": "true"},
        )
        body = client.V1Secret(
            metadata=metadata, type="Opaque", data={"kubeconfig": payload},
        )
        try:
            await tenant_core.create_namespaced_secret(
                namespace=TENANT_DRIVER_NAMESPACE, body=body,
            )
        except ApiException as e:
            if e.status != 409:
                raise
            existing = await tenant_core.read_namespaced_secret(
                name=CSI_KUBECONFIG_SECRET_NAME, namespace=TENANT_DRIVER_NAMESPACE,
            )
            existing.data = {"kubeconfig": payload}
            await tenant_core.replace_namespaced_secret(
                name=CSI_KUBECONFIG_SECRET_NAME,
                namespace=TENANT_DRIVER_NAMESPACE, body=existing,
            )
        logger.info(
            f"Replicated {CSI_KUBECONFIG_SECRET_NAME!r} into tenant "
            f"{tenant_name!r} ns {TENANT_DRIVER_NAMESPACE!r}"
        )
        return True
    finally:
        await tenant_core.api_client.close()


# ---------------------------------------------------------------------------
# Storage wiring status + background completion
# ---------------------------------------------------------------------------

async def _secret_exists(core_api, name: str, ns: str) -> bool:
    try:
        await core_api.read_namespaced_secret(name=name, namespace=ns)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


async def get_storage_status(k8s, tenant_name: str) -> TenantStorageStatus:
    """Report how far the CSI passthrough wiring got for one tenant.

    Cheap enough to poll: three secret reads in the host cluster plus one
    in the tenant (skipped when the tenant CP isn't reachable).
    """
    ns = _tenant_ns(tenant_name)
    core_api = client.CoreV1Api(k8s._api_client)

    host_ready = await _secret_exists(core_api, CSI_KUBECONFIG_SECRET_NAME, ns)
    capk_ready = await _secret_exists(core_api, CAPK_KUBECONFIG_SECRET_NAME, ns)

    if not host_ready:
        return TenantStorageStatus(
            tenant=tenant_name,
            phase="disabled",
            detail=(
                "Tenant storage is not enabled — the host-side CSI "
                "credentials were never created."
            ),
        )

    tenant_core = await _build_tenant_core_api(k8s, tenant_name)
    if tenant_core is None:
        return TenantStorageStatus(
            tenant=tenant_name,
            phase="pending",
            host_credentials_ready=True,
            capk_credentials_ready=capk_ready,
            detail=(
                "Tenant control plane not reachable yet. The CSI controller "
                "stays in ContainerCreating until the credentials land; "
                "this finishes on its own, or run reconcile now."
            ),
        )

    try:
        replicated = await _secret_exists(
            tenant_core, CSI_KUBECONFIG_SECRET_NAME, TENANT_DRIVER_NAMESPACE,
        )
    finally:
        await tenant_core.api_client.close()

    return TenantStorageStatus(
        tenant=tenant_name,
        phase="ready" if replicated else "pending",
        host_credentials_ready=True,
        capk_credentials_ready=capk_ready,
        tenant_reachable=True,
        credentials_replicated=replicated,
        detail=(
            "Storage wiring complete."
            if replicated
            else "Tenant is up but the credentials have not been replicated "
                 "yet — run reconcile to finish now."
        ),
    )


# Cumulative ~23 min, front-loaded: a tenant CP is usually reachable within
# a couple of minutes, and anything past that is a real problem the operator
# should see (the reconcile endpoint stays available either way).
REPLICATION_RETRY_DELAYS_SEC = (20, 30, 30, 60, 60, 120, 180, 300, 300, 300)

# Strong refs to in-flight retry tasks — asyncio only holds weak ones, so
# without this the loop can be garbage-collected mid-wait.
_replication_tasks: set[asyncio.Task] = set()


async def _retry_credential_replication(k8s, tenant_name: str) -> None:
    """Keep retrying replication until it lands, the tenant is gone, or we
    run out of attempts. Never raises — this runs detached."""
    ns = _tenant_ns(tenant_name)
    for attempt, delay in enumerate(REPLICATION_RETRY_DELAYS_SEC, start=1):
        await asyncio.sleep(delay)

        try:
            await k8s.core_api.read_namespace(name=ns)
        except ApiException as e:
            if e.status == 404:
                logger.info(
                    f"Tenant {tenant_name!r} disappeared — abandoning CSI "
                    "credential replication"
                )
                return
        except Exception as exc:
            logger.warning(
                f"Tenant {tenant_name!r}: could not check namespace before "
                f"CSI replication retry {attempt}: {exc}"
            )
            continue

        try:
            if await replicate_csi_credentials_to_tenant(k8s, tenant_name):
                logger.info(
                    f"Tenant {tenant_name!r}: CSI credentials replicated on "
                    f"background retry {attempt}"
                )
                return
        except Exception as exc:
            logger.warning(
                f"Tenant {tenant_name!r}: CSI replication retry {attempt} "
                f"failed: {exc}"
            )

    logger.warning(
        f"Tenant {tenant_name!r}: CSI credentials still not replicated after "
        f"{len(REPLICATION_RETRY_DELAYS_SEC)} attempts. The tenant's CSI "
        "controller will stay in ContainerCreating until "
        f"POST /tenants/{tenant_name}/storage/reconcile succeeds."
    )


def schedule_credential_replication(k8s, tenant_name: str) -> None:
    """Fire-and-forget background completion of the storage wiring.

    Create-time replication normally defers (cold control plane), and
    leaving it there means the operator has to know to call reconcile. This
    drives it to completion instead; the endpoint and the UI button remain
    as the manual escape hatch.
    """
    task = asyncio.create_task(_retry_credential_replication(k8s, tenant_name))
    _replication_tasks.add(task)
    task.add_done_callback(_replication_tasks.discard)
