"""KubeVirt helpers."""

import logging
import os
import ssl
import time
from typing import Any

import aiohttp
from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException

logger = logging.getLogger(__name__)

# Cache feature gates with TTL (5 minutes)
_feature_gates_cache: list[str] | None = None
_feature_gates_ts: float = 0.0
_FEATURE_GATES_TTL = 300  # seconds


async def get_feature_gates(k8s_client: Any) -> list[str]:
    """Get KubeVirt feature gates from the KubeVirt CR.

    Discovers the KubeVirt CR dynamically across all namespaces
    (the CR may live in 'kubevirt', 'o0-kubevirt', etc.).
    """
    global _feature_gates_cache, _feature_gates_ts
    if _feature_gates_cache is not None and (time.monotonic() - _feature_gates_ts) < _FEATURE_GATES_TTL:
        return _feature_gates_cache

    try:
        custom_api = client.CustomObjectsApi(k8s_client._api_client)
        result = await custom_api.list_cluster_custom_object(
            group="kubevirt.io",
            version="v1",
            plural="kubevirts",
        )
        items = result.get("items", [])
        if not items:
            logger.warning("No KubeVirt CR found in the cluster")
            return []
        kubevirt = items[0]
        gates = (
            kubevirt.get("spec", {})
            .get("configuration", {})
            .get("developerConfiguration", {})
            .get("featureGates", [])
        )
        ns = kubevirt.get("metadata", {}).get("namespace", "?")
        logger.info(f"KubeVirt CR found in namespace '{ns}', feature gates: {gates}")
        _feature_gates_cache = gates
        _feature_gates_ts = time.monotonic()
        return gates
    except ApiException as e:
        logger.warning(f"Failed to read KubeVirt feature gates: {e.reason}")
        return []


async def get_hotplug_mode(k8s_client: Any) -> str:
    """Determine the hotplug mode based on feature gates.

    Returns:
        'declarative' — DeclarativeHotplugVolumes only (virtio, no reboot)
        'legacy'      — HotplugVolumes present (scsi, reboot needed)
        'none'        — no hotplug support
    """
    gates = await get_feature_gates(k8s_client)
    has_legacy = "HotplugVolumes" in gates
    has_declarative = "DeclarativeHotplugVolumes" in gates

    if has_legacy:
        # Legacy takes priority when both are present (scsi + reboot)
        return "legacy"
    if has_declarative:
        return "declarative"
    return "none"


async def has_declarative_hotplug(k8s_client: Any) -> bool:
    """Check if DeclarativeHotplugVolumes feature gate is enabled."""
    return (await get_hotplug_mode(k8s_client)) == "declarative"


def clear_feature_gates_cache() -> None:
    """Clear cached feature gates (e.g. after config change)."""
    global _feature_gates_cache, _feature_gates_ts
    _feature_gates_cache = None
    _feature_gates_ts = 0.0


# --------------- Shared aiohttp helper for KubeVirt subresource API ---------------

def _build_ssl_context(config: Any) -> ssl.SSLContext | None:
    """Build SSL context from kubernetes client configuration."""
    if not (config.ssl_ca_cert or config.cert_file):
        return None
    ctx = ssl.create_default_context()
    if config.ssl_ca_cert:
        ctx.load_verify_locations(config.ssl_ca_cert)
    if config.cert_file and config.key_file:
        ctx.load_cert_chain(config.cert_file, config.key_file)
    # Use in-cluster CA cert if available; fall back to no-verify only in dev.
    sa_ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    if os.getenv("K8S_SSL_VERIFY", "").lower() == "true":
        # Explicit opt-in to strict verification
        pass
    elif os.path.exists(sa_ca_path) and not config.ssl_ca_cert:
        ctx.load_verify_locations(sa_ca_path)
    elif os.getenv("K8S_SSL_VERIFY", "").lower() != "true":
        # Dev fallback: no verification (not recommended for production)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _build_headers(config: Any) -> dict[str, str]:
    """Build HTTP headers (content-type + auth) from kubernetes client configuration.

    Handles both out-of-cluster (api_key['authorization']) and
    in-cluster (api_key['BearerToken'] + api_key_prefix) auth.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    api_key = getattr(config, "api_key", None) or {}
    api_key_prefix = getattr(config, "api_key_prefix", None) or {}

    if "authorization" in api_key:
        # Out-of-cluster: kubeconfig sets a full Authorization value
        prefix = api_key_prefix.get("authorization", "")
        token = api_key["authorization"]
        headers["Authorization"] = f"{prefix} {token}".strip() if prefix else token
    elif "BearerToken" in api_key:
        # In-cluster: kubernetes-asyncio sets BearerToken + prefix
        prefix = api_key_prefix.get("BearerToken", "Bearer")
        headers["Authorization"] = f"{prefix} {api_key['BearerToken']}"
    else:
        # Fallback: read SA token directly from disk
        sa_token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        if os.path.exists(sa_token_path):
            with open(sa_token_path) as f:
                headers["Authorization"] = f"Bearer {f.read().strip()}"
    return headers


async def kubevirt_subresource_call(
    k8s_client: Any,
    method: str,
    namespace: str,
    vm_name: str,
    subresource: str,
    body: dict | None = None,
) -> tuple[bool, str]:
    """Make an HTTP call to the KubeVirt subresource API.
    
    Returns (success: bool, response_text: str).
    """
    api_client = k8s_client._api_client
    config = api_client.configuration
    url = (
        f"{config.host}/apis/subresources.kubevirt.io/v1"
        f"/namespaces/{namespace}"
        f"/virtualmachines/{vm_name}/{subresource}"
    )

    ssl_context = _build_ssl_context(config)
    headers = _build_headers(config)

    try:
        connector = aiohttp.TCPConnector(ssl=ssl_context) if ssl_context else None
        async with aiohttp.ClientSession(connector=connector) as session:
            req_method = getattr(session, method.lower())
            async with req_method(url, json=body, headers=headers) as resp:
                text = await resp.text()
                if resp.status < 400:
                    return True, text
                else:
                    logger.warning(f"Subresource {subresource} failed (status {resp.status}): {text}")
                    return False, text
    except Exception as e:
        logger.warning(f"Subresource {subresource} request failed: {e}")
        return False, str(e)
