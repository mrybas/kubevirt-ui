# KubeVirt UI Helm Chart

A lightweight web interface for managing KubeVirt virtual machines on Kubernetes.

## Prerequisites

- Kubernetes >= 1.26
- Helm >= 3.10
- [KubeVirt](https://kubevirt.io/) installed on the cluster
- (Optional) [CDI](https://github.com/kubevirt/containerized-data-importer) for disk image management

## Installing the Chart

```bash
helm install kubevirt-ui ./helm/kubevirt-ui -n kubevirt-ui --create-namespace
```

With custom values:

```bash
helm install kubevirt-ui ./helm/kubevirt-ui -n kubevirt-ui --create-namespace -f my-values.yaml
```

## Uninstalling the Chart

```bash
helm uninstall kubevirt-ui -n kubevirt-ui
```

## Architecture

The chart deploys the following components:

| Component | Description | Default |
|-----------|-------------|---------|
| **Backend** | FastAPI service proxying Kubernetes API | Always deployed |
| **Frontend** | Nginx serving the React SPA | Always deployed |
| **Dex** | OIDC identity provider | Disabled |
| **LLDAP** | Lightweight LDAP server for user management | Disabled |

```
                  ┌──────────┐
  Browser ──────► │ Frontend │ (nginx + SPA)
                  │  :8080   │
                  └────┬─────┘
                       │ /api/*
                  ┌────▼─────┐      ┌──────────────┐
                  │ Backend  │─────►│ Kubernetes   │
                  │  :8000   │      │ API Server   │
                  └────┬─────┘      └──────────────┘
                       │ (optional)
                  ┌────▼─────┐      ┌──────────────┐
                  │   Dex    │◄────►│    LLDAP     │
                  │  :5556   │      │  :3890/17170 │
                  └──────────┘      └──────────────┘
```

## Parameters

### Global

| Parameter | Description | Default |
|-----------|-------------|---------|
| `replicaCount` | Number of replicas for backend and frontend | `1` |
| `imagePullSecrets` | Image pull secrets | `[]` |
| `nameOverride` | Override chart name | `""` |
| `fullnameOverride` | Override full release name | `""` |

### Backend

| Parameter | Description | Default |
|-----------|-------------|---------|
| `backend.image.repository` | Backend image repository | `ghcr.io/mrybas/kubevirt-ui/backend` |
| `backend.image.tag` | Backend image tag (defaults to `appVersion`) | `""` |
| `backend.image.pullPolicy` | Image pull policy | `IfNotPresent` |
| `backend.resources.requests.memory` | Memory request | `128Mi` |
| `backend.resources.requests.cpu` | CPU request | `100m` |
| `backend.resources.limits.memory` | Memory limit | `512Mi` |
| `backend.resources.limits.cpu` | CPU limit | `1` |
| `backend.service.type` | Service type | `ClusterIP` |
| `backend.service.port` | Service port | `8000` |
| `backend.env.LOG_LEVEL` | Log level | `INFO` |
| `backend.env.CORS_ORIGINS` | Allowed CORS origins (comma-separated) | `""` |
| `backend.env.ENABLE_TENANTS` | Enable multi-tenant mode | `"false"` |
| `backend.envFrom` | Extra env sources (secretRef/configMapRef) | `[]` |

### Frontend

| Parameter | Description | Default |
|-----------|-------------|---------|
| `frontend.image.repository` | Frontend image repository | `ghcr.io/mrybas/kubevirt-ui/frontend` |
| `frontend.image.tag` | Frontend image tag (defaults to `appVersion`) | `""` |
| `frontend.image.pullPolicy` | Image pull policy | `IfNotPresent` |
| `frontend.resources.requests.memory` | Memory request | `64Mi` |
| `frontend.resources.requests.cpu` | CPU request | `50m` |
| `frontend.resources.limits.memory` | Memory limit | `128Mi` |
| `frontend.resources.limits.cpu` | CPU limit | `200m` |
| `frontend.service.type` | Service type | `ClusterIP` |
| `frontend.service.port` | Service port | `8080` |

### Metrics

| Parameter | Description | Default |
|-----------|-------------|---------|
| `metrics.direct` | Force direct HTTP to metrics backend (`""` = auto-detect, `"true"` = direct, `"false"` = K8s API proxy) | `""` |
| `metrics.service` | Override metrics service (`namespace/service:port`) | `""` |

When `metrics.direct` is empty, the backend auto-detects: direct HTTP when running in-cluster, K8s API proxy otherwise. When `metrics.service` is empty, the backend auto-discovers metrics using VMSingle CRD, then falls back to service label matching (vmsingle, prometheus).

### ServiceAccount

| Parameter | Description | Default |
|-----------|-------------|---------|
| `serviceAccount.create` | Create a ServiceAccount | `true` |
| `serviceAccount.annotations` | ServiceAccount annotations | `{}` |
| `serviceAccount.name` | ServiceAccount name (auto-generated if empty) | `""` |

### Pod Security

| Parameter | Description | Default |
|-----------|-------------|---------|
| `podAnnotations` | Pod annotations | `{}` |
| `podSecurityContext.runAsNonRoot` | Run as non-root | `true` |
| `podSecurityContext.runAsUser` | User ID | `1000` |
| `podSecurityContext.fsGroup` | Filesystem group | `1000` |
| `podSecurityContext.seccompProfile.type` | Seccomp profile | `RuntimeDefault` |
| `securityContext.allowPrivilegeEscalation` | Allow privilege escalation | `false` |
| `securityContext.readOnlyRootFilesystem` | Read-only root filesystem | `true` |
| `securityContext.capabilities.drop` | Dropped capabilities | `[ALL]` |

### Scheduling

| Parameter | Description | Default |
|-----------|-------------|---------|
| `nodeSelector` | Node selector labels | `{}` |
| `tolerations` | Pod tolerations | `[]` |
| `affinity` | Pod affinity rules | `{}` |

### Ingress

| Parameter | Description | Default |
|-----------|-------------|---------|
| `ingress.enabled` | Enable ingress | `false` |
| `ingress.className` | Ingress class name | `nginx` |
| `ingress.annotations` | Ingress annotations | `{}` |
| `ingress.hosts[0].host` | Hostname | `kubevirt-ui.local` |
| `ingress.hosts[0].paths[0].path` | Path | `/` |
| `ingress.hosts[0].paths[0].pathType` | Path type | `Prefix` |
| `ingress.tls` | TLS configuration | `[]` |

The ingress template routes `/api` and `/health` to the backend, `/dex` to Dex (if enabled), and everything else to the frontend. WebSocket support is included for VNC and serial consoles.

### RBAC

| Parameter | Description | Default |
|-----------|-------------|---------|
| `rbac.create` | Create RBAC resources for the ServiceAccount | `true` |
| `rbac.clusterWide` | Use ClusterRole (required for multi-namespace, node listing) | `true` |
| `rbac.createRoles` | Create user-facing ClusterRoles | `true` |

When `rbac.createRoles` is enabled, four ClusterRoles are created:

| Role | Description |
|------|-------------|
| `kubevirt-ui-admin` | Full access to VMs, storage, networking |
| `kubevirt-ui-editor` | Create and manage VMs and related resources |
| `kubevirt-ui-viewer` | Read-only access to VMs and resources |
| `kubevirt-ui-platform-admin` | Admin + node management + cluster settings |

Bind these roles to users/groups via ClusterRoleBindings to control access in the UI.

### Network Policy

| Parameter | Description | Default |
|-----------|-------------|---------|
| `networkPolicy.enabled` | Enable NetworkPolicy resources | `false` |

When enabled, creates NetworkPolicy resources for both frontend and backend with appropriate ingress/egress rules.

### UI Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `config.defaultNamespace` | Default namespace for VM operations | `default` |
| `config.vmDefaults.cpu` | Default VM CPU count | `2` |
| `config.vmDefaults.memory` | Default VM memory | `2Gi` |
| `config.ui.theme` | UI theme (`dark` / `light`) | `dark` |
| `config.ui.language` | UI language | `en` |
| `config.ui.pagination` | Items per page | `25` |
| `config.features.enableVNCConsole` | Enable VNC console | `true` |
| `config.features.enableSerialConsole` | Enable serial console | `true` |
| `config.features.enableLiveMigration` | Enable live migration controls | `true` |
| `config.features.enableSnapshots` | Enable VM snapshots | `true` |

This configuration is mounted into the backend as `/app/config/config.yaml`.

### Authentication

| Parameter | Description | Default |
|-----------|-------------|---------|
| `auth.type` | Auth type: `none`, `oidc`, `token` | `oidc` |
| `auth.oidc.issuer` | OIDC issuer URL (must be reachable from the browser) | `""` |
| `auth.oidc.issuerInternal` | Internal OIDC issuer URL (backend to IdP, falls back to `issuer`) | `""` |
| `auth.oidc.clientId` | OIDC client ID | `kubevirt-ui` |
| `auth.oidc.clientSecretRef.name` | Secret name holding the OIDC client secret | `""` |
| `auth.oidc.clientSecretRef.key` | Key within the secret | `client-secret` |

### Dex (Optional OIDC Provider)

| Parameter | Description | Default |
|-----------|-------------|---------|
| `dex.enabled` | Deploy bundled Dex OIDC provider | `false` |
| `dex.image.repository` | Dex image | `ghcr.io/dexidp/dex` |
| `dex.image.tag` | Dex image tag | `v2.38.0` |
| `dex.image.pullPolicy` | Image pull policy | `IfNotPresent` |
| `dex.resources.requests.memory` | Memory request | `64Mi` |
| `dex.resources.requests.cpu` | CPU request | `50m` |
| `dex.resources.limits.memory` | Memory limit | `128Mi` |
| `dex.resources.limits.cpu` | CPU limit | `200m` |
| `dex.service.type` | Service type | `ClusterIP` |
| `dex.service.port` | Service port | `5556` |
| `dex.storage.type` | Storage backend (`memory` or `kubernetes`) | `memory` |
| `dex.connectors` | Upstream identity provider connectors | `[]` |
| `dex.staticPasswords` | Static user accounts | `[]` |
| `dex.enablePasswordDB` | Enable Dex built-in password database | `false` |
| `dex.logLevel` | Log level (`debug`, `info`, `warn`, `error`) | `info` |

When Dex and LLDAP are both enabled, the LDAP connector is auto-configured.

### LLDAP (Optional LDAP Server)

| Parameter | Description | Default |
|-----------|-------------|---------|
| `lldap.enabled` | Deploy bundled LLDAP server | `false` |
| `lldap.image.repository` | LLDAP image | `lldap/lldap` |
| `lldap.image.tag` | LLDAP image tag | `latest` |
| `lldap.image.pullPolicy` | Image pull policy | `IfNotPresent` |
| `lldap.baseDN` | LDAP base DN | `dc=kubevirt,dc=local` |
| `lldap.adminUser` | Admin username | `admin` |
| `lldap.adminPassword` | Admin password (ignored if `existingSecret` is set) | `admin_password` |
| `lldap.jwtSecret` | JWT secret for LLDAP web UI | `change-me-in-production` |
| `lldap.existingSecret` | Use existing secret (keys: `admin-password`, `jwt-secret`) | `""` |
| `lldap.verbose` | Enable verbose logging | `"false"` |
| `lldap.resources.requests.memory` | Memory request | `64Mi` |
| `lldap.resources.requests.cpu` | CPU request | `50m` |
| `lldap.resources.limits.memory` | Memory limit | `256Mi` |
| `lldap.resources.limits.cpu` | CPU limit | `500m` |
| `lldap.persistence.enabled` | Enable persistent storage | `false` |
| `lldap.persistence.size` | PVC size | `1Gi` |
| `lldap.persistence.storageClass` | Storage class (empty = default) | `""` |

> **Warning**: Change `lldap.adminPassword` and `lldap.jwtSecret` in production, or use `lldap.existingSecret` to reference a pre-created Secret.

## Examples

### Minimal (no auth)

```yaml
auth:
  type: none
```

### OIDC with external provider

```yaml
auth:
  type: oidc
  oidc:
    issuer: "https://auth.example.com"
    clientId: "kubevirt-ui"
    clientSecretRef:
      name: kubevirt-ui-oidc
      key: client-secret

ingress:
  enabled: true
  className: nginx
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
  hosts:
    - host: kubevirt.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: kubevirt-ui-tls
      hosts:
        - kubevirt.example.com
```

### Bundled Dex + LLDAP (self-contained auth)

```yaml
auth:
  type: oidc
  oidc:
    issuer: "https://kubevirt.example.com/dex"
    clientId: "kubevirt-ui"

dex:
  enabled: true
  storage:
    type: kubernetes

lldap:
  enabled: true
  existingSecret: kubevirt-lldap-credentials
  persistence:
    enabled: true
    size: 2Gi

ingress:
  enabled: true
  hosts:
    - host: kubevirt.example.com
      paths:
        - path: /
          pathType: Prefix
```

### Custom VM defaults and features

```yaml
config:
  defaultNamespace: production
  vmDefaults:
    cpu: 4
    memory: 8Gi
  ui:
    theme: light
    pagination: 50
  features:
    enableLiveMigration: false
    enableSnapshots: false
```

### Metrics with VictoriaMetrics

```yaml
metrics:
  direct: "true"
  service: "monitoring/vmsingle-victoria:8429"
```

## Resource Requirements

Minimum resources for a complete deployment:

| Component | Memory (request/limit) | CPU (request/limit) |
|-----------|----------------------|---------------------|
| Backend | 128Mi / 512Mi | 100m / 1 |
| Frontend | 64Mi / 128Mi | 50m / 200m |
| Dex (optional) | 64Mi / 128Mi | 50m / 200m |
| LLDAP (optional) | 64Mi / 256Mi | 50m / 500m |

**Total (all components)**: ~320Mi request / ~1Gi limit memory.

## Development

```bash
# Lint the chart
make helm-lint

# Render templates locally
make helm-template

# Package the chart
make helm-package
```
