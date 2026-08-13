"""VM console WebSocket proxies: VNC and serial."""

import asyncio
import base64
import logging
import ssl

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.auth import AUTH_TYPE, validate_oidc_token, validate_k8s_token
from app.core.groups import is_admin, is_env_member, load_folder, resolve_env

router = APIRouter()
logger = logging.getLogger(__name__)


async def _ws_authenticate(websocket: WebSocket, namespace: str | None = None) -> bool:
    """Authenticate WebSocket connection via token query parameter.

    Returns True if authenticated AND authorized, False otherwise (the
    socket is closed with code 1008 in either failure mode — caller
    should just `return`).

    WebSocket cannot use HTTP header auth, so the token is passed as a
    query parameter: `?token=<bearer_token>`.

    Authorization model (when `namespace` is given): the user must be a
    folder/env member of the namespace's folder.  Global admins always
    pass.  This is enforced BEFORE the upgrade is accepted — opening a
    VNC/serial console grants raw guest console access, so we must not
    fall back to "let K8s sort it out" the way HTTP endpoints can.
    """
    if AUTH_TYPE == "none":
        return True

    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=1008, reason="Authentication required")
        return False

    user = None
    if AUTH_TYPE == "token":
        user = await validate_k8s_token(websocket, token)
    elif AUTH_TYPE == "oidc":
        user = await validate_oidc_token(token)

    if user is None:
        await websocket.close(code=1008, reason="Invalid or expired token")
        return False

    # Enforce env_member on the target namespace.  Global admins
    # bypass — `is_env_member` falls through to is_folder_admin →
    # is_admin internally, so the explicit early-return is just a
    # cheap path for the common case.
    if namespace:
        if is_admin(user.groups, user):
            return True

        k8s_client = websocket.app.state.k8s_client
        try:
            folder, env = await resolve_env(k8s_client, namespace)
            folder_meta = await load_folder(k8s_client, folder)
        except Exception as e:
            # resolve_env / load_folder raise HTTPException 404 when the
            # namespace isn't a managed env — treat that as forbidden
            # (we don't expose console for unmanaged namespaces).
            logger.warning(f"Console authz lookup failed for {namespace}: {e}")
            await websocket.close(code=1008, reason="Namespace not authorized")
            return False

        if not is_env_member(user, folder_meta, env):
            await websocket.close(
                code=1008,
                reason="Env member role required to access VM console",
            )
            return False

    return True


def _build_ws_connection_params(config) -> tuple[dict[str, str], ssl.SSLContext]:
    """Build authentication headers and SSL context from K8s client config.

    Shared by both VNC and serial console proxies.
    Supports three auth methods:
    1. Client certificates (Talos kubeconfig with client-certificate-data)
    2. Bearer token (api_key from kubeconfig)
    3. In-cluster SA token (/var/run/secrets/kubernetes.io/serviceaccount/token)
    """
    headers: dict[str, str] = {}
    auth_method = "none"

    ssl_context = ssl.create_default_context()
    if config.ssl_ca_cert:
        ssl_context.load_verify_locations(config.ssl_ca_cert)
    if config.verify_ssl is False:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    # Method 1: Client certificates (e.g. Talos kubeconfig)
    if hasattr(config, 'cert_file') and config.cert_file and hasattr(config, 'key_file') and config.key_file:
        ssl_context.load_cert_chain(config.cert_file, config.key_file)
        auth_method = "client-certificate"

    # Method 2: Bearer token from kubeconfig
    if config.api_key and config.api_key.get('authorization'):
        headers["Authorization"] = f"Bearer {config.api_key['authorization']}"
        auth_method = "bearer-token"
    elif hasattr(config, 'username') and config.username:
        credentials = base64.b64encode(f"{config.username}:{config.password}".encode()).decode()
        headers["Authorization"] = f"Basic {credentials}"
        auth_method = "basic-auth"

    # Method 3: In-cluster SA token fallback
    if auth_method == "none" or (auth_method == "client-certificate" and "Authorization" not in headers):
        sa_token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        try:
            with open(sa_token_path) as f:
                sa_token = f.read().strip()
            headers["Authorization"] = f"Bearer {sa_token}"
            auth_method = "service-account"
        except FileNotFoundError:
            pass

    logger.info(f"WebSocket auth method: {auth_method}")

    return headers, ssl_context


def _build_subresource_ws_url(config, namespace: str, name: str, subresource: str) -> str:
    """Build a KubeVirt subresource WebSocket URL."""
    api_host = config.host.replace("https://", "").replace("http://", "")
    path = (
        f"/apis/subresources.kubevirt.io/v1"
        f"/namespaces/{namespace}/virtualmachineinstances/{name}/{subresource}"
    )
    ws_protocol = "wss" if config.host.startswith("https") else "ws"
    return f"{ws_protocol}://{api_host}{path}"


@router.websocket("/{name}/console/vnc")
async def vnc_console_proxy(
    websocket: WebSocket,
    namespace: str,
    name: str,
):
    """WebSocket proxy for VNC console access to a VM."""
    if not await _ws_authenticate(websocket, namespace=namespace):
        return
    await websocket.accept()

    k8s_client = websocket.app.state.k8s_client
    config = k8s_client._api_client.configuration
    headers, ssl_context = _build_ws_connection_params(config)
    vnc_url = _build_subresource_ws_url(config, namespace, name, "vnc")

    try:
        logger.info(f"Connecting to VNC console for {namespace}/{name} at {vnc_url}")

        async with websockets.connect(
            vnc_url,
            additional_headers=headers,
            ssl=ssl_context,
        ) as k8s_ws:
            logger.info(f"Connected to VNC console for {namespace}/{name}, subprotocol: {k8s_ws.subprotocol}")

            async def forward_to_k8s():
                try:
                    while True:
                        data = await websocket.receive_bytes()
                        await k8s_ws.send(data)
                except WebSocketDisconnect:
                    logger.info(f"Browser disconnected from VNC {namespace}/{name}")
                except Exception as e:
                    logger.error(f"Error forwarding to K8s: {type(e).__name__}: {e}")

            async def forward_to_browser():
                try:
                    async for message in k8s_ws:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            try:
                                decoded = base64.b64decode(message)
                                await websocket.send_bytes(decoded)
                            except Exception:
                                await websocket.send_text(message)
                except websockets.exceptions.ConnectionClosed as e:
                    logger.info(f"K8s WebSocket closed for VNC {namespace}/{name}: {e}")
                except Exception as e:
                    logger.error(f"Error forwarding to browser: {type(e).__name__}: {e}")

            await asyncio.gather(
                forward_to_k8s(),
                forward_to_browser(),
                return_exceptions=True,
            )

    except websockets.exceptions.InvalidStatusCode as e:
        logger.error(f"Failed to connect to VNC: HTTP {e.status_code}")
        await websocket.close(code=1011, reason=f"Failed to connect to VM console: {e.status_code}")
    except Exception as e:
        logger.error(f"VNC proxy error: {e}")
        await websocket.close(code=1011, reason=str(e))


@router.websocket("/{name}/console/serial")
async def serial_console_proxy(
    websocket: WebSocket,
    namespace: str,
    name: str,
):
    """WebSocket proxy for serial console access to a VM."""
    if not await _ws_authenticate(websocket, namespace=namespace):
        return
    await websocket.accept()

    k8s_client = websocket.app.state.k8s_client
    config = k8s_client._api_client.configuration
    headers, ssl_context = _build_ws_connection_params(config)
    console_url = _build_subresource_ws_url(config, namespace, name, "console")

    try:
        logger.info(f"Connecting to serial console for {namespace}/{name}")

        async with websockets.connect(
            console_url,
            additional_headers=headers,
            ssl=ssl_context,
        ) as k8s_ws:
            logger.info(f"Connected to serial console for {namespace}/{name}")

            async def forward_to_k8s():
                try:
                    while True:
                        try:
                            data = await websocket.receive_text()
                            await k8s_ws.send(data)
                        except Exception:
                            data = await websocket.receive_bytes()
                            await k8s_ws.send(data)
                except WebSocketDisconnect:
                    logger.info(f"Browser disconnected from serial console {namespace}/{name}")
                except Exception as e:
                    logger.error(f"Error forwarding to K8s serial: {type(e).__name__}: {e}")

            async def forward_to_browser():
                try:
                    async for message in k8s_ws:
                        if isinstance(message, bytes):
                            try:
                                text = message.decode('utf-8', errors='replace')
                                text = text.replace('\x00', '')
                                await websocket.send_text(text)
                            except Exception:
                                await websocket.send_bytes(message)
                        else:
                            text = message.replace('\x00', '') if isinstance(message, str) else message
                            await websocket.send_text(text)
                except websockets.exceptions.ConnectionClosed as e:
                    logger.info(f"K8s serial console closed for {namespace}/{name}: {e}")
                except Exception as e:
                    logger.error(f"Error forwarding serial to browser: {type(e).__name__}: {e}")

            await asyncio.gather(
                forward_to_k8s(),
                forward_to_browser(),
                return_exceptions=True,
            )

    except websockets.exceptions.InvalidStatusCode as e:
        logger.error(f"Failed to connect to serial console: HTTP {e.status_code}")
        await websocket.close(code=1011, reason=f"Failed to connect to serial console: {e.status_code}")
    except Exception as e:
        logger.error(f"Serial console proxy error: {e}")
        await websocket.close(code=1011, reason=str(e))
