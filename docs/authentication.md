# Authentication Guide

KubeVirt UI supports three authentication modes controlled by the `AUTH_TYPE` environment variable.

## Auth Modes

### `none` (default)

No authentication. All requests are served as an anonymous admin user with group `kubevirt-ui-admins`. Suitable for local development only.

### `oidc`

OpenID Connect authentication via [Dex](https://dexidp.io/). This is the primary production mode.

The OIDC flow uses the Authorization Code grant:

1. Frontend redirects user to Dex authorization endpoint
2. Dex authenticates against the configured connector (LLDAP, LDAP, SAML, etc.)
3. User is redirected back to frontend with an authorization code
4. Frontend sends the code to `POST /api/v1/auth/token`
5. Backend exchanges the code for tokens at Dex's token endpoint
6. Frontend stores the access token and includes it in `Authorization: Bearer <token>` headers

### `token`

Kubernetes ServiceAccount token authentication. The token is validated via the K8s TokenReview API. Useful for service-to-service access or CI/CD pipelines.

## OIDC Architecture

```
Browser              Backend (FastAPI)         Dex              LLDAP
  │                       │                     │                 │
  │── GET /auth/config ──▶│                     │                 │
  │◀── {issuer, endpoints}│                     │                 │
  │                       │                     │                 │
  │── redirect ──────────────────────────────▶│                 │
  │                       │                     │── LDAP bind ──▶│
  │                       │                     │◀── user info ──│
  │◀── code callback ───────────────────────◀│                 │
  │                       │                     │                 │
  │── POST /auth/token ──▶│                     │                 │
  │   {code, redirect_uri}│── POST /token ─────▶│                 │
  │                       │◀── {access_token} ──│                 │
  │◀── {access_token} ───│                     │                 │
  │                       │                     │                 │
  │── GET /api/v1/... ───▶│                     │                 │
  │   Authorization:      │── GET /userinfo ───▶│                 │
  │     Bearer <token>    │◀── {sub, email,    │                 │
  │                       │     groups}         │                 │
  │◀── response ─────────│                     │                 │
```

## Backend Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `AUTH_TYPE` | `none` | Auth mode: `none`, `oidc`, or `token` |
| `OIDC_ISSUER` | (empty) | Public OIDC issuer URL (used by frontend and for token validation) |
| `OIDC_INTERNAL_URL` | (empty) | Internal URL for backend-to-Dex communication (e.g., `http://dex:5556`) |
| `OIDC_CLIENT_ID` | `kubevirt-ui` | OAuth2 client ID registered in Dex |
| `OIDC_CLIENT_SECRET` | (empty) | OAuth2 client secret |

### Internal vs Public URLs

The backend supports split URLs for OIDC communication:

- **`OIDC_ISSUER`** — Public URL that the browser can reach (e.g., `http://192.168.196.1:5556`)
- **`OIDC_INTERNAL_URL`** — Internal URL for backend-to-Dex calls within Docker network (e.g., `http://dex:5556`)

When `OIDC_INTERNAL_URL` is set, the backend replaces the host portion of OIDC endpoints (token, userinfo, discovery) with the internal URL while keeping the path from the public URL. This avoids routing backend traffic through the host network.

## Frontend Configuration

| Variable | Description |
|---|---|
| `VITE_API_URL` | Backend API base URL (e.g., `http://192.168.196.1:8888`) |
| `VITE_DEX_ISSUER` | Dex issuer URL for OIDC redirect (e.g., `http://192.168.196.1:5556`) |
| `VITE_OIDC_CLIENT_ID` | OAuth2 client ID (must match backend) |

## Auth API Endpoints

### `GET /api/v1/auth/config`

Returns authentication configuration for the frontend. The frontend uses this to determine which auth mode is active and where to redirect for login.

**Response:**
```json
{
  "type": "oidc",
  "issuer": "http://192.168.196.1:5556",
  "client_id": "kubevirt-ui",
  "authorization_endpoint": "http://192.168.196.1:5556/auth",
  "token_endpoint": "http://192.168.196.1:5556/token",
  "userinfo_endpoint": "http://192.168.196.1:5556/userinfo",
  "user_management": "lldap"
}
```

The `user_management` field indicates whether the UI should show user/group management:
- `lldap` — bundled LLDAP, full user CRUD available
- `external` — external IdP, no user management in UI
- `none` — auth disabled, no user management

### `POST /api/v1/auth/token`

Exchanges an authorization code for access/refresh tokens. The backend proxies this to Dex's token endpoint, adding the client secret (which the frontend should not have).

**Request:**
```json
{
  "code": "authorization_code_from_callback",
  "redirect_uri": "http://192.168.196.1:3333/auth/callback"
}
```

**Response:**
```json
{
  "access_token": "eyJ...",
  "token_type": "Bearer",
  "expires_in": 86400,
  "refresh_token": "eyJ...",
  "id_token": "eyJ..."
}
```

### `POST /api/v1/auth/refresh`

Refreshes an expired access token using a refresh token.

**Query parameter:** `refresh_token` (string)

**Response:** Same as `/token`.

### `GET /api/v1/auth/me`

Returns the current user's information. Requires `Authorization: Bearer <token>` header.

**Response:**
```json
{
  "id": "user-uuid",
  "email": "admin@kubevirt.local",
  "username": "admin",
  "groups": ["kubevirt-ui-admins"]
}
```

### `POST /api/v1/auth/kubeconfig`

Generates kubeconfig files for CLI/Terraform access. Creates a Kubernetes ServiceAccount bound to the user's RBAC permissions.

**Request:**
```json
{
  "id_token": "eyJ...",
  "refresh_token": "eyJ..."
}
```

Tokens are optional. When provided and `K8S_OIDC_ENABLED=true`, additional OIDC-based kubeconfig variants are generated.

**Response:** Contains one or more kubeconfig variants:
- **ServiceAccount Token** (always available) — 72-hour expiry, works with any K8s cluster
- **OIDC auto-renewing** (optional) — requires K8s API server OIDC config, auto-refreshes
- **kubelogin exec plugin** (optional) — requires kubelogin installed, browser-based auth

### `POST /api/v1/auth/logout`

Client-side logout. Returns `{"status": "ok"}`. The client should clear stored tokens.

## Authorization (RBAC)

### Groups

Authorization is group-based. Groups come from the OIDC token (populated by Dex from the LDAP connector):

| Group | Role |
|---|---|
| `kubevirt-ui-admins` | Platform admin — full cluster access |
| `disabled-users` | Account disabled — all requests return `403` |
| (other groups) | Regular user — access scoped to namespaces with matching RoleBindings |

### Namespace Access

The `check_namespace_access()` function verifies user access via Kubernetes SubjectAccessReview:

```python
# Checks if user can "get" virtualmachines.kubevirt.io in the namespace
V1SubjectAccessReview(spec=V1SubjectAccessReviewSpec(
    user=user.email,
    groups=user.groups,
    resource_attributes=V1ResourceAttributes(
        namespace=namespace,
        verb=verb,
        resource="virtualmachines",
        group="kubevirt.io",
    ),
))
```

Regular users see only namespaces labeled `kubevirt-ui.io/enabled=true` where they have a managed RoleBinding.

### Folder Access

Folders organize namespaces hierarchically. Access to a folder is granted if the user has a RoleBinding in any namespace belonging to that folder or any of its ancestors.

## Dex Configuration (Development)

The dev setup uses Dex with an LDAP connector pointing to bundled LLDAP:

```yaml
# dex/config.yaml
issuer: http://192.168.196.1:5556

staticClients:
  - id: kubevirt-ui
    secret: kubevirt-ui-secret
    redirectURIs:
      - "http://192.168.196.1:3333/callback"
      - "http://192.168.196.1:3333/auth/callback"

connectors:
  - type: ldap
    id: lldap
    config:
      host: lldap:3890
      insecureNoSSL: true
      bindDN: uid=admin,ou=people,dc=kubevirt,dc=local
      bindPW: admin_password
```

### Token Expiry (Development)

| Token | Lifetime |
|---|---|
| ID tokens | 24 hours |
| Signing keys | 6 hours |
| Refresh tokens (unused) | 168 hours (7 days) |
| Refresh tokens (absolute) | 720 hours (30 days) |
| ServiceAccount tokens | 72 hours |

## LLDAP (Bundled User Directory)

When `LLDAP_ENABLED=true`, the backend manages users and groups via LLDAP's GraphQL API.

| Variable | Default | Description |
|---|---|---|
| `LLDAP_ENABLED` | `false` | Enable bundled user management |
| `LLDAP_URL` | `http://lldap:17170` | LLDAP web API URL |
| `LLDAP_ADMIN_USER` | `admin` | LLDAP admin username |
| `LLDAP_ADMIN_PASSWORD` | `admin_password` | LLDAP admin password |
| `LLDAP_LDAP_BASE_DN` | `dc=kubevirt,dc=local` | LDAP base DN |

### Ports

| Service | Port | Description |
|---|---|---|
| LLDAP LDAP | 3890 | LDAP protocol (used by Dex connector) |
| LLDAP Web UI | 17170 | Admin web interface (dev only) |
| Dex | 5556 | OIDC provider |

## Production Deployment

For production, configure:

1. **Dex issuer** — use a real domain with TLS (e.g., `https://dex.example.com`)
2. **Client secret** — generate a strong random secret, do not use `kubevirt-ui-secret`
3. **LDAP connector** — point to your organization's LDAP/AD instead of LLDAP
4. **Redirect URIs** — update to match your production frontend URL
5. **TLS** — enable TLS on Dex and use `https://` for all URLs
6. **LLDAP** — disable (`LLDAP_ENABLED=false`) if using external IdP
7. **Token lifetimes** — reduce ID token expiry for tighter security
