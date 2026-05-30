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
  5. ResourceQuota in the tenant ns capping `persistentvolumeclaims` and
     `requests.storage`.

All steps are idempotent (409 / read-before-create) so re-running a
tenant create after partial failure doesn't error.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import yaml
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException

from app.core.kube_api_url import discover_external_api_url
from app.models.tenant import TenantCreateRequest

from app.api.v1.tenants_common import _tenant_ns

logger = logging.getLogger(__name__)


CSI_SA_NAME = "kubevirt-csi"
CSI_ROLE_NAME = "kubevirt-csi"
CSI_ROLE_BINDING_NAME = "kubevirt-csi"
CSI_KUBECONFIG_SECRET_NAME = "infra-cluster-credentials"
CSI_SA_TOKEN_SECRET_NAME = "kubevirt-csi-token"
CSI_RESOURCE_QUOTA_NAME = "tenant-storage"

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
    """RBAC rules — mirror upstream kubevirt/csi-driver Role exactly.

    Source: https://github.com/kubevirt/csi-driver/blob/main/deploy/infra-cluster-service-account.yaml
    """
    return [
        {
            "apiGroups": ["cdi.kubevirt.io"],
            "resources": ["datavolumes"],
            "verbs": ["get", "create", "delete"],
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
            "verbs": ["get", "patch"],
        },
        {
            # Pod read — used by the kubevirt-csi controller to find the
            # virt-launcher pod hosting the worker VMI when hotplugging.
            # Upstream doesn't require this in the namespaced Role (they
            # read VMIs instead) but it's harmless and matches what the
            # team-lead asked for in the T3 spec.
            "apiGroups": [""],
            "resources": ["pods"],
            "verbs": ["get", "list"],
        },
    ]


def _build_csi_kubeconfig(
    api_server_url: str,
    ca_data_b64: str,
    sa_token: str,
    tenant_ns: str,
) -> str:
    """Assemble the kubeconfig YAML that the tenant-CP CSI driver consumes.

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
                "user": CSI_SA_NAME,
                "namespace": tenant_ns,
            },
        }],
        "current-context": "only-context",
        "preferences": {},
        "users": [{
            "name": CSI_SA_NAME,
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


async def _ensure_service_account(core_api, ns: str, tenant_name: str) -> None:
    try:
        await core_api.read_namespaced_service_account(
            name=CSI_SA_NAME, namespace=ns,
        )
        return
    except ApiException as e:
        if e.status != 404:
            raise
    body = client.V1ServiceAccount(
        metadata=client.V1ObjectMeta(
            name=CSI_SA_NAME,
            namespace=ns,
            labels={
                "kubevirt-ui.io/managed": "true",
                "kubevirt-ui.io/tenant": tenant_name,
                "kubevirt-ui.io/role": "csi-infra",
            },
        ),
    )
    try:
        await core_api.create_namespaced_service_account(namespace=ns, body=body)
    except ApiException as e:
        if e.status != 409:
            raise


async def _ensure_role(rbac_api, ns: str, tenant_name: str) -> None:
    rules = [
        client.V1PolicyRule(
            api_groups=r["apiGroups"],
            resources=r["resources"],
            verbs=r["verbs"],
        )
        for r in _csi_role_rules()
    ]
    metadata = client.V1ObjectMeta(
        name=CSI_ROLE_NAME,
        namespace=ns,
        labels={
            "kubevirt-ui.io/managed": "true",
            "kubevirt-ui.io/tenant": tenant_name,
            "kubevirt-ui.io/role": "csi-infra",
        },
    )
    try:
        existing = await rbac_api.read_namespaced_role(name=CSI_ROLE_NAME, namespace=ns)
        # Replace rules so RBAC stays in sync if we change _csi_role_rules() across releases
        existing.rules = rules
        await rbac_api.replace_namespaced_role(
            name=CSI_ROLE_NAME, namespace=ns, body=existing,
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


async def _ensure_role_binding(rbac_api, ns: str, tenant_name: str) -> None:
    metadata = client.V1ObjectMeta(
        name=CSI_ROLE_BINDING_NAME,
        namespace=ns,
        labels={
            "kubevirt-ui.io/managed": "true",
            "kubevirt-ui.io/tenant": tenant_name,
            "kubevirt-ui.io/role": "csi-infra",
        },
    )
    role_ref = client.V1RoleRef(
        api_group="rbac.authorization.k8s.io",
        kind="Role",
        name=CSI_ROLE_NAME,
    )
    subject = client.RbacV1Subject(
        kind="ServiceAccount",
        name=CSI_SA_NAME,
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


async def _ensure_sa_token_secret(core_api, ns: str, tenant_name: str) -> str:
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
        name=CSI_SA_TOKEN_SECRET_NAME,
        namespace=ns,
        annotations={"kubernetes.io/service-account.name": CSI_SA_NAME},
        labels={
            "kubevirt-ui.io/managed": "true",
            "kubevirt-ui.io/tenant": tenant_name,
            "kubevirt-ui.io/role": "csi-infra",
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
            name=CSI_SA_TOKEN_SECRET_NAME, namespace=ns,
        )
        token_b64 = (secret.data or {}).get("token")
        if token_b64:
            return base64.b64decode(token_b64).decode("utf-8")
        await asyncio.sleep(CSI_TOKEN_POLL_INTERVAL_SEC)

    raise RuntimeError(
        f"SA-token Secret {CSI_SA_TOKEN_SECRET_NAME!r} in {ns!r} was not "
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
) -> None:
    kubeconfig_str = _build_csi_kubeconfig(
        api_server_url=api_server_url,
        ca_data_b64=ca_data_b64,
        sa_token=sa_token,
        tenant_ns=ns,
    )
    data = {"kubeconfig": base64.b64encode(kubeconfig_str.encode("utf-8")).decode("ascii")}
    metadata = client.V1ObjectMeta(
        name=CSI_KUBECONFIG_SECRET_NAME,
        namespace=ns,
        labels={
            "kubevirt-ui.io/managed": "true",
            "kubevirt-ui.io/tenant": tenant_name,
            "kubevirt-ui.io/role": "csi-infra",
        },
    )
    body = client.V1Secret(metadata=metadata, type="Opaque", data=data)
    try:
        existing = await core_api.read_namespaced_secret(
            name=CSI_KUBECONFIG_SECRET_NAME, namespace=ns,
        )
        # Always refresh the kubeconfig payload — the SA token changes on
        # every create call (we mint a new one) and we want the Secret to
        # reflect that so the tenant CP picks up the new credentials.
        existing.data = data
        await core_api.replace_namespaced_secret(
            name=CSI_KUBECONFIG_SECRET_NAME, namespace=ns, body=existing,
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
    # SA exists before TokenRequest, and the binding has a target Role).
    await _ensure_service_account(core_api, ns, req.name)
    await _ensure_role(rbac_api, ns, req.name)
    await _ensure_role_binding(rbac_api, ns, req.name)

    # 4. Ensure a non-expiring SA-token Secret and read the token. Stable
    #    across re-runs (Secret persists with the tenant ns).
    sa_token = await _ensure_sa_token_secret(core_api, ns, req.name)

    # 5. Resolve the external apiserver URL + cluster CA cert
    api_server_url, source = await discover_external_api_url(k8s)
    if source == "fallback":
        logger.warning(
            "kubevirt-csi kubeconfig will use the in-cluster fallback URL "
            f"({api_server_url}); set KUBE_API_EXTERNAL_URL or publish the "
            "kube-public/cluster-info ConfigMap so the tenant CSI driver "
            "can actually reach the host apiserver."
        )
    ca_data_b64 = _read_host_ca_b64(k8s)

    # 6. Secret with kubeconfig
    await _ensure_kubeconfig_secret(
        core_api, ns, req.name, api_server_url, ca_data_b64, sa_token,
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
        # NOTE: storage class is NOT plumbed through KubevirtCluster.spec —
        # the CAPK v1alpha1 schema has no `infraClusterStorageClassName`
        # field. Tenant SC selection happens in the kubevirt-csi-driver
        # HelmRelease values via the INFRA_STORAGE_CLASS_NAME addon param.
    }
