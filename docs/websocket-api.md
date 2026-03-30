# WebSocket API Reference

The KubeVirt UI backend exposes WebSocket endpoints that proxy VM console connections through the Kubernetes API to KubeVirt's subresource API.

## Endpoints

### VNC Console

```
ws(s)://<host>/api/v1/namespaces/{namespace}/vms/{name}/console/vnc
```

Proxies a VNC console session to a running VirtualMachineInstance. The backend connects to the KubeVirt subresource API:

```
/apis/subresources.kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstances/{name}/vnc
```

**Data format:** Binary frames (raw VNC/RFB protocol). The frontend uses [react-vnc](https://github.com/nicknisi/react-vnc) (`VncScreen` component) to render the VNC session.

### Serial Console

```
ws(s)://<host>/api/v1/namespaces/{namespace}/vms/{name}/console/serial
```

Proxies a serial console (TTY) session to a running VirtualMachineInstance. The backend connects to:

```
/apis/subresources.kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstances/{name}/console
```

**Data format:** Text frames (UTF-8). Binary frames from the K8s API are decoded to UTF-8 with null bytes stripped. The frontend uses [xterm.js](https://xtermjs.org/) to render the terminal.

## Authentication

WebSocket connections cannot use standard HTTP `Authorization` headers (browser WebSocket API limitation). Instead, the token is passed as a query parameter:

```
ws://<host>/api/v1/namespaces/{namespace}/vms/{name}/console/vnc?token=<bearer_token>
```

### Auth modes

| `AUTH_TYPE` | Behavior |
|---|---|
| `none` | No token required. All connections are allowed (development only). |
| `oidc` | Token is validated against the OIDC provider's userinfo endpoint. |
| `token` | Token is validated as a Kubernetes ServiceAccount token via TokenReview. |

### Authentication flow

1. Client opens WebSocket with `?token=<access_token>` query parameter
2. Backend calls `_ws_authenticate()` before accepting the connection
3. If token is missing → connection closed with code `1008` ("Authentication required")
4. If token is invalid/expired → connection closed with code `1008` ("Invalid or expired token")
5. If valid → connection is accepted and proxying begins

### Namespace access check

After token validation, the backend verifies the user has access to the target namespace by reading the namespace object and checking its labels. Fine-grained RBAC enforcement happens at the KubeVirt API level when the proxy connects to the subresource endpoint.

## Connection Architecture

```
Browser                    Backend (FastAPI)              K8s API Server
  │                            │                              │
  │─── WS connect ────────────▶│                              │
  │    ?token=xxx              │                              │
  │                            │── validate token ───────────▶│ (OIDC/TokenReview)
  │                            │◀── user info ───────────────│
  │                            │                              │
  │◀── WS accept ─────────────│                              │
  │                            │── WS connect ───────────────▶│
  │                            │   /apis/subresources.        │
  │                            │   kubevirt.io/v1/...         │
  │                            │◀── WS accept ───────────────│
  │                            │                              │
  │◀══ bidirectional proxy ═══▶│◀══ bidirectional proxy ═══▶│
  │    (binary for VNC,        │    (K8s auth via            │
  │     text for serial)       │     SA token/cert)          │
```

The backend maintains two concurrent `asyncio` tasks per connection:
- `forward_to_k8s()` — reads from browser WebSocket, writes to K8s WebSocket
- `forward_to_browser()` — reads from K8s WebSocket, writes to browser WebSocket

Both tasks run via `asyncio.gather()` and terminate when either side disconnects.

## K8s API Connection

The backend authenticates to the Kubernetes API using the kubeconfig loaded at startup (`KUBECONFIG` env var). Connection parameters are built from the K8s client configuration:

- **Authorization**: Bearer token or Basic auth from kubeconfig
- **TLS**: SSL context from CA cert, client cert/key in kubeconfig
- **Protocol**: `wss://` for HTTPS clusters, `ws://` for HTTP

## WebSocket Close Codes

| Code | Meaning |
|---|---|
| `1000` | Normal closure |
| `1008` | Authentication failure (missing or invalid token) |
| `1011` | Failed to connect to VM console (KubeVirt API error) |

## Frontend Integration

### VNC Console (`VNCViewer.tsx`)

```typescript
const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const host = window.location.host;
const wsUrl = `${protocol}//${host}/api/v1/namespaces/${namespace}/vms/${vmName}/console/vnc`;

// Uses react-vnc VncScreen component
<VncScreen url={wsUrl} ref={vncRef} />
```

### Serial Console (`SerialConsole.tsx`)

```typescript
const wsUrl = `${protocol}//${host}/api/v1/namespaces/${namespace}/vms/${vmName}/console/serial`;
const ws = new WebSocket(wsUrl);
ws.binaryType = 'arraybuffer';

// Data piped to xterm.js Terminal instance
ws.onmessage = (event) => {
  terminal.write(new Uint8Array(event.data));
};
terminal.onData((data) => {
  ws.send(data);
});
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `1008` close immediately | Missing/expired token | Refresh OIDC token, pass via `?token=` |
| `1011` close after accept | VM not running or KubeVirt API unreachable | Check VMI status: `kubectl get vmi -n <ns>` |
| VNC connects but black screen | VM has no display device | Ensure VM template includes a VNC graphics device |
| Serial console no output | VM has no serial console configured | Add `serial0` device to VM spec |
| Connection drops after ~30s | Idle timeout on load balancer/ingress | Configure WebSocket timeout on ingress annotations |
