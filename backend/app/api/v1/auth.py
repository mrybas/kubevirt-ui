"""Authentication API endpoints."""

import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.core.auth import (
    OIDC_CLIENT_ID,
    OIDC_CLIENT_SECRET,
    OIDC_ISSUER,
    User,
    get_auth_config,
    get_internal_url,
    get_oidc_config,
    require_auth,
)
from app.core.groups import get_user_namespaces, is_admin
from fastapi import Depends

router = APIRouter()
logger = logging.getLogger(__name__)


class AuthConfigResponse(BaseModel):
    """Authentication configuration for frontend."""

    type: str
    issuer: str | None = None
    client_id: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    userinfo_endpoint: str | None = None
    user_management: str = "none"


class TokenRequest(BaseModel):
    """Token exchange request."""

    code: str
    redirect_uri: str


class TokenResponse(BaseModel):
    """Token exchange response."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int | None = None
    refresh_token: str | None = None
    id_token: str | None = None


class UserInfoResponse(BaseModel):
    """Current user information."""

    id: str
    email: str
    username: str
    groups: list[str]


@router.get("/config", response_model=AuthConfigResponse)
async def get_config() -> AuthConfigResponse:
    """Get authentication configuration for frontend."""
    config = await get_auth_config()
    return AuthConfigResponse(
        type=config.type,
        issuer=config.issuer,
        client_id=config.client_id,
        authorization_endpoint=config.authorization_endpoint,
        token_endpoint=config.token_endpoint,
        userinfo_endpoint=config.userinfo_endpoint,
        user_management=config.user_management,
    )


@router.post("/token", response_model=TokenResponse)
async def exchange_token(request: TokenRequest) -> TokenResponse:
    """Exchange authorization code for tokens."""
    try:
        oidc_config = await get_oidc_config()
        token_endpoint = oidc_config.get("token_endpoint")

        if not token_endpoint:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Token endpoint not configured",
            )

        # Use internal URL if configured for backend->OIDC communication
        internal_token_endpoint = get_internal_url(token_endpoint)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                internal_token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": request.code,
                    "redirect_uri": request.redirect_uri,
                    "client_id": OIDC_CLIENT_ID,
                    "client_secret": OIDC_CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if response.status_code != 200:
                logger.error(f"Token exchange failed: {response.text}")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token exchange failed",
                )

            data = response.json()
            return TokenResponse(
                access_token=data["access_token"],
                token_type=data.get("token_type", "Bearer"),
                expires_in=data.get("expires_in"),
                refresh_token=data.get("refresh_token"),
                id_token=data.get("id_token"),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token exchange error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(refresh_token: str) -> TokenResponse:
    """Refresh access token."""
    try:
        oidc_config = await get_oidc_config()
        token_endpoint = oidc_config.get("token_endpoint")

        if not token_endpoint:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Token endpoint not configured",
            )

        # Use internal URL if configured for backend->OIDC communication
        internal_token_endpoint = get_internal_url(token_endpoint)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                internal_token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": OIDC_CLIENT_ID,
                    "client_secret": OIDC_CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token refresh failed",
                )

            data = response.json()
            return TokenResponse(
                access_token=data["access_token"],
                token_type=data.get("token_type", "Bearer"),
                expires_in=data.get("expires_in"),
                refresh_token=data.get("refresh_token"),
                id_token=data.get("id_token"),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


@router.get("/me", response_model=UserInfoResponse)
async def get_current_user_info(user: User = Depends(require_auth)) -> UserInfoResponse:
    """Get current user information."""
    return UserInfoResponse(
        id=user.id,
        email=user.email,
        username=user.username,
        groups=user.groups,
    )


class KubeconfigVariant(BaseModel):
    """A single kubeconfig variant."""
    id: str
    label: str
    description: str
    kubeconfig: str
    instructions: str


class KubeconfigResponse(BaseModel):
    """Kubeconfig for CLI access."""
    variants: list[KubeconfigVariant]
    cluster_name: str
    server: str
    username: str
    auth_type: str


class KubeconfigTokensRequest(BaseModel):
    """Optional OIDC tokens from the frontend session for auto-renewal kubeconfig."""
    id_token: str | None = None
    refresh_token: str | None = None


SA_NAMESPACE = "kubevirt-ui-system"
SA_TOKEN_EXPIRATION = 72 * 3600  # Short-lived tokens; frontend should refresh via OIDC
# Set K8S_OIDC_ENABLED=true if the K8s API server is configured with --oidc-issuer-url
K8S_OIDC_ENABLED = os.environ.get("K8S_OIDC_ENABLED", "false").lower() == "true"


def _sanitize_sa_name(username: str) -> str:
    """Sanitize username to a valid K8s ServiceAccount name."""
    import re
    name = username.lower().split("@")[0]
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return f"kubevirt-ui-{name}"[:63]


async def _ensure_service_account(
    k8s_client: Any,
    sa_name: str,
    username: str,
    groups: list[str],
    api_server_url: str = "https://kubernetes.default.svc",
) -> str:
    """Create ServiceAccount + ClusterRoleBinding for user, return a bound token."""
    from kubernetes_asyncio.client import (
        CoreV1Api,
        RbacAuthorizationV1Api,
        V1ServiceAccount,
        V1ObjectMeta,
        V1ClusterRoleBinding,
        V1RoleRef,
        RbacV1Subject,
    )
    from kubernetes_asyncio.client.rest import ApiException

    core_api = CoreV1Api(k8s_client._api_client)
    rbac_api = RbacAuthorizationV1Api(k8s_client._api_client)

    # 1. Ensure namespace exists
    try:
        await core_api.read_namespace(SA_NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            from kubernetes_asyncio.client import V1Namespace
            await core_api.create_namespace(V1Namespace(
                metadata=V1ObjectMeta(
                    name=SA_NAMESPACE,
                    labels={"kubevirt-ui.io/managed": "true"},
                ),
            ))
        else:
            raise

    # 2. Create or update ServiceAccount
    sa_labels = {
        "kubevirt-ui.io/managed": "true",
        "kubevirt-ui.io/cli-access": "true",
    }
    sa_annotations = {
        "kubevirt-ui.io/user": username,
    }
    try:
        existing_sa = await core_api.read_namespaced_service_account(sa_name, SA_NAMESPACE)
        # Update annotations if needed
        if existing_sa.metadata.annotations != sa_annotations:
            existing_sa.metadata.annotations = sa_annotations
            existing_sa.metadata.labels = sa_labels
            await core_api.replace_namespaced_service_account(sa_name, SA_NAMESPACE, existing_sa)
    except ApiException as e:
        if e.status == 404:
            await core_api.create_namespaced_service_account(
                namespace=SA_NAMESPACE,
                body=V1ServiceAccount(
                    metadata=V1ObjectMeta(
                        name=sa_name,
                        namespace=SA_NAMESPACE,
                        labels=sa_labels,
                        annotations=sa_annotations,
                    ),
                ),
            )
        else:
            raise

    # 3. Determine role from groups
    user_is_admin = any(g in groups for g in ["kubevirt-ui-admins", "system:masters"])
    binding_name = f"{sa_name}-binding"
    subject = RbacV1Subject(kind="ServiceAccount", name=sa_name, namespace=SA_NAMESPACE)

    if user_is_admin:
        # Admins: ClusterRoleBinding with cluster-admin
        cluster_role = "cluster-admin"
        binding = V1ClusterRoleBinding(
            metadata=V1ObjectMeta(name=binding_name, labels=sa_labels, annotations=sa_annotations),
            role_ref=V1RoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name=cluster_role),
            subjects=[subject],
        )
        try:
            existing = await rbac_api.read_cluster_role_binding(binding_name)
            if existing.role_ref.name != cluster_role:
                await rbac_api.delete_cluster_role_binding(binding_name)
                await rbac_api.create_cluster_role_binding(binding)
            else:
                existing.subjects = binding.subjects
                existing.metadata.labels = sa_labels
                existing.metadata.annotations = sa_annotations
                await rbac_api.replace_cluster_role_binding(binding_name, existing)
        except ApiException as e:
            if e.status == 404:
                await rbac_api.create_cluster_role_binding(binding)
            else:
                raise
    else:
        # Non-admins: per-namespace RoleBindings (edit) only in allowed namespaces
        # Remove stale admin ClusterRoleBinding if it exists (e.g. user was demoted)
        try:
            await rbac_api.delete_cluster_role_binding(binding_name)
        except ApiException:
            pass

        from kubernetes_asyncio.client import V1RoleBinding, V1ClusterRole, V1PolicyRule
        allowed_ns = await get_user_namespaces(k8s_client, type("U", (), {"email": username, "groups": groups})())

        # Shared ClusterRole: read-only access to namespaces.
        # K8s RBAC cannot filter namespace list — users see all ns names
        # but can only access resources in namespaces where they have RoleBindings.
        ns_viewer_role = "kubevirt-ui-ns-viewer"
        desired_rules = [V1PolicyRule(
            api_groups=[""],
            resources=["namespaces"],
            verbs=["get", "list", "watch"],
        )]
        try:
            existing_cr = await rbac_api.read_cluster_role(ns_viewer_role)
            # Update rules if they changed
            existing_cr.rules = desired_rules
            await rbac_api.replace_cluster_role(ns_viewer_role, existing_cr)
        except ApiException as e:
            if e.status == 404:
                try:
                    await rbac_api.create_cluster_role(V1ClusterRole(
                        metadata=V1ObjectMeta(name=ns_viewer_role, labels={"kubevirt-ui.io/managed": "true"}),
                        rules=desired_rules,
                    ))
                except ApiException as ce:
                    if ce.status != 409:
                        raise

        # Bind SA to the ns-viewer ClusterRole
        ns_viewer_binding = f"{sa_name}-ns-viewer"
        ns_viewer_crb = V1ClusterRoleBinding(
            metadata=V1ObjectMeta(name=ns_viewer_binding, labels=sa_labels, annotations=sa_annotations),
            role_ref=V1RoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name=ns_viewer_role),
            subjects=[subject],
        )
        try:
            existing_crb = await rbac_api.read_cluster_role_binding(ns_viewer_binding)
            existing_crb.subjects = [subject]
            existing_crb.metadata.labels = sa_labels
            await rbac_api.replace_cluster_role_binding(ns_viewer_binding, existing_crb)
        except ApiException as e:
            if e.status == 404:
                try:
                    await rbac_api.create_cluster_role_binding(ns_viewer_crb)
                except ApiException as ce:
                    if ce.status != 409:
                        raise
            else:
                raise

        # Per-namespace RoleBindings with edit role
        for ns in allowed_ns:
            ns_binding_name = f"{sa_name}-{ns}"
            ns_binding = V1RoleBinding(
                metadata=V1ObjectMeta(
                    name=ns_binding_name, namespace=ns,
                    labels={**sa_labels, "kubevirt-ui.io/cli-sa": sa_name},
                    annotations=sa_annotations,
                ),
                role_ref=V1RoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name="edit"),
                subjects=[subject],
            )
            try:
                await rbac_api.create_namespaced_role_binding(namespace=ns, body=ns_binding)
            except ApiException as e:
                if e.status == 409:
                    pass  # already exists
                else:
                    logger.warning(f"Failed to create RoleBinding in {ns}: {e.reason}")

    # 5. Generate bound token via TokenRequest API
    from kubernetes_asyncio.client import (
        AuthenticationV1TokenRequest,
        V1TokenRequestSpec,
    )
    token_request = AuthenticationV1TokenRequest(
        spec=V1TokenRequestSpec(
            audiences=[api_server_url],
            expiration_seconds=SA_TOKEN_EXPIRATION,
        ),
    )
    result = await core_api.create_namespaced_service_account_token(
        sa_name, SA_NAMESPACE, token_request,
    )
    return result.status.token


@router.post("/kubeconfig", response_model=KubeconfigResponse)
async def get_kubeconfig(
    request: Request,
    body: KubeconfigTokensRequest,
    user: User = Depends(require_auth),
) -> KubeconfigResponse:
    """Generate kubeconfig(s) for CLI/Terraform access.

    Primary variant: ServiceAccount token (always works against K8s API server).
    Optional OIDC variants: only when K8S_OIDC_ENABLED=true (requires API server OIDC config).
    """
    import yaml
    from app.core.auth import AUTH_TYPE

    k8s_client = request.app.state.k8s_client

    # Get cluster API server URL from the loaded configuration
    k8s_config = k8s_client._api_client.configuration
    server = k8s_config.host
    cluster_name = "kubevirt-cluster"

    # Get CA data if available
    ca_data = None
    if k8s_config.ssl_ca_cert:
        try:
            import base64
            with open(k8s_config.ssl_ca_cert, "rb") as f:
                ca_data = base64.b64encode(f.read()).decode("utf-8")
        except Exception:
            pass

    # Build cluster entry
    cluster_entry: dict[str, Any] = {"server": server}
    if ca_data:
        cluster_entry["certificate-authority-data"] = ca_data
    else:
        cluster_entry["insecure-skip-tls-verify"] = True

    username = user.email or user.username
    variants: list[KubeconfigVariant] = []

    # --- Variant 1 (primary): ServiceAccount token ---
    # Always works because it uses K8s-native authentication
    try:
        sa_name = _sanitize_sa_name(username)
        sa_token = await _ensure_service_account(
            k8s_client, sa_name, username, user.groups,
            api_server_url=server,
        )

        # Set default namespace for non-admin users
        user_is_admin_flag = is_admin(user.groups)
        allowed_ns = await get_user_namespaces(k8s_client, user) if not user_is_admin_flag else []
        context_entry: dict[str, Any] = {
            "cluster": cluster_name,
            "user": sa_name,
        }
        if not user_is_admin_flag and allowed_ns:
            context_entry["namespace"] = allowed_ns[0]

        sa_kubeconfig = {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": cluster_name,
            "clusters": [{"name": cluster_name, "cluster": cluster_entry}],
            "contexts": [{"name": cluster_name, "context": context_entry}],
            "users": [{"name": sa_name, "user": {"token": sa_token}}],
        }
        role_str = "cluster-admin (full cluster access)" if user_is_admin_flag else f"edit (scoped to {len(allowed_ns)} namespace(s))"
        ns_list_str = ""
        if not user_is_admin_flag and allowed_ns:
            ns_list_str = "\n**Allowed namespaces:** " + ", ".join(f"`{n}`" for n in allowed_ns) + "\n"

        sa_instructions = (
            "## ServiceAccount Kubeconfig\n\n"
            "This kubeconfig uses a Kubernetes ServiceAccount token "
            "created for your user. It works immediately with kubectl and Terraform.\n\n"
            f"**ServiceAccount:** `{sa_name}` in namespace `{SA_NAMESPACE}`\n\n"
            f"**Role:** {role_str}\n"
            f"{ns_list_str}\n"
            f"**Token expires:** ~72 hours (come back to download a fresh one)\n\n"
            "### 1. Save the kubeconfig\n"
            "```bash\n"
            "mkdir -p ~/.kube\n"
            "# Click 'Download' above, then:\n"
            "mv ~/Downloads/kubeconfig-kubevirt-sa-token.yaml ~/.kube/config-kubevirt\n"
            "chmod 600 ~/.kube/config-kubevirt\n"
            "```\n\n"
            "### 2. Use it\n"
            "```bash\n"
            "export KUBECONFIG=~/.kube/config-kubevirt\n"
            + (
                "kubectl get namespaces\n"
                "kubectl get vm -A\n"
                if user_is_admin_flag else
                "kubectl get namespaces  # read-only, shows all ns names\n"
                + "kubectl get vm  # default namespace: " + (allowed_ns[0] if allowed_ns else "N/A") + "\n"
                + ("".join(f"kubectl get vm -n {n}\n" for n in allowed_ns) if len(allowed_ns) > 1 else "")
            ) +
            "```\n\n"
            "### Terraform\n"
            "```hcl\n"
            "provider \"kubernetes\" {\n"
            "  config_path    = \"~/.kube/config-kubevirt\"\n"
            f"  config_context = \"{cluster_name}\"\n"
            "}\n"
            "```\n"
        )
        variants.append(KubeconfigVariant(
            id="sa-token",
            label="ServiceAccount Token",
            description="Works immediately. Uses a K8s-native ServiceAccount token (~72h expiry).",
            kubeconfig=yaml.dump(sa_kubeconfig, default_flow_style=False, sort_keys=False),
            instructions=sa_instructions,
        ))
    except Exception as e:
        logger.error(f"Failed to create ServiceAccount token: {e}")
        # Continue — other variants may still work

    # --- Variant 2: OIDC auth-provider with refresh_token (only if K8s API has OIDC) ---
    if K8S_OIDC_ENABLED and AUTH_TYPE == "oidc" and OIDC_ISSUER and body.id_token and body.refresh_token:
        oidc_auth_provider = {
            "auth-provider": {
                "name": "oidc",
                "config": {
                    "idp-issuer-url": OIDC_ISSUER,
                    "client-id": OIDC_CLIENT_ID,
                    "client-secret": OIDC_CLIENT_SECRET,
                    "id-token": body.id_token,
                    "refresh-token": body.refresh_token,
                },
            }
        }
        oidc_renew_kubeconfig = {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": cluster_name,
            "clusters": [{"name": cluster_name, "cluster": cluster_entry}],
            "contexts": [{"name": cluster_name, "context": {"cluster": cluster_name, "user": username}}],
            "users": [{"name": username, "user": oidc_auth_provider}],
        }
        oidc_renew_instructions = (
            "## Auto-renewing OIDC Kubeconfig\n\n"
            "This kubeconfig includes your OIDC tokens with a refresh token. "
            "kubectl will **automatically refresh** the token when it expires.\n\n"
            "**Requires:** K8s API server configured with `--oidc-issuer-url`\n\n"
            "### 1. Save the kubeconfig\n"
            "```bash\n"
            "mkdir -p ~/.kube\n"
            "mv ~/Downloads/kubeconfig-kubevirt-oidc-renew.yaml ~/.kube/config-kubevirt\n"
            "```\n\n"
            "### 2. Use it\n"
            "```bash\n"
            f"export KUBECONFIG=~/.kube/config-kubevirt\n"
            f"kubectl get namespaces\n"
            "```\n"
        )
        variants.append(KubeconfigVariant(
            id="oidc-renew",
            label="OIDC (auto-renewing)",
            description="Requires K8s API server OIDC. Tokens refresh automatically.",
            kubeconfig=yaml.dump(oidc_renew_kubeconfig, default_flow_style=False, sort_keys=False),
            instructions=oidc_renew_instructions,
        ))

    # --- Variant 3: OIDC exec plugin (only if K8s API has OIDC) ---
    if K8S_OIDC_ENABLED and AUTH_TYPE == "oidc" and OIDC_ISSUER:
        oidc_kubeconfig = {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": cluster_name,
            "clusters": [{"name": cluster_name, "cluster": cluster_entry}],
            "contexts": [{"name": cluster_name, "context": {"cluster": cluster_name, "user": username}}],
            "users": [{"name": username, "user": {
                "exec": {
                    "apiVersion": "client.authentication.k8s.io/v1beta1",
                    "command": "kubectl",
                    "args": [
                        "oidc-login", "get-token",
                        f"--oidc-issuer-url={OIDC_ISSUER}",
                        f"--oidc-client-id={OIDC_CLIENT_ID}",
                        f"--oidc-client-secret={OIDC_CLIENT_SECRET}",
                    ],
                    "interactiveMode": "IfAvailable",
                },
            }}],
        }
        oidc_instructions = (
            "## Persistent Access (kubelogin)\n\n"
            "This kubeconfig uses the `kubelogin` exec plugin. "
            "Requires one-time install, authenticates via browser.\n\n"
            "**Requires:** K8s API server configured with `--oidc-issuer-url` + kubelogin installed\n\n"
            "### 1. Install kubelogin\n"
            "```bash\n"
            "# Homebrew (macOS/Linux)\n"
            "brew install int128/kubelogin/kubelogin\n\n"
            "# Or via krew\n"
            "kubectl krew install oidc-login\n"
            "```\n\n"
            "### 2. Save and use\n"
            "```bash\n"
            "mkdir -p ~/.kube\n"
            f"export KUBECONFIG=~/.kube/config-kubevirt\n"
            f"kubectl get namespaces\n"
            "```\n"
            "A browser window will open for authentication on first use.\n"
        )
        variants.append(KubeconfigVariant(
            id="oidc-exec",
            label="kubelogin (exec plugin)",
            description="Requires kubelogin install + K8s OIDC. Authenticates via browser, never expires.",
            kubeconfig=yaml.dump(oidc_kubeconfig, default_flow_style=False, sort_keys=False),
            instructions=oidc_instructions,
        ))

    if not variants:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate kubeconfig. Check backend logs.",
        )

    return KubeconfigResponse(
        variants=variants,
        cluster_name=cluster_name,
        server=server,
        username=username,
        auth_type=AUTH_TYPE,
    )


@router.post("/logout")
async def logout() -> dict[str, str]:
    """Logout (client should clear tokens)."""
    return {"status": "ok", "message": "Logged out. Clear tokens on client."}
