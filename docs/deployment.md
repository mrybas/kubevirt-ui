# Deployment Guide

This guide covers deploying KubeVirt UI to a Kubernetes cluster using the Helm chart.

## Prerequisites

- Kubernetes cluster >= 1.26 with [KubeVirt](https://kubevirt.io/) installed
- Helm >= 3.10
- `kubectl` access to the target cluster
- (Optional) [CDI](https://github.com/kubevirt/containerized-data-importer) for disk image management
- (Optional) An OIDC identity provider (Keycloak, Dex, etc.) if using OIDC auth

## Quick Start

### 1. Install with no authentication

The simplest deployment — useful for internal/development clusters:

```bash
helm install kubevirt-ui ./helm/kubevirt-ui \
  -n kubevirt-ui --create-namespace \
  --set auth.type=none
```

### 2. Verify the deployment

```bash
kubectl -n kubevirt-ui get pods
# NAME                                   READY   STATUS    RESTARTS   AGE
# kubevirt-ui-backend-...                1/1     Running   0          30s
# kubevirt-ui-frontend-...               1/1     Running   0          30s
```

### 3. Access the UI

```bash
kubectl -n kubevirt-ui port-forward svc/kubevirt-ui-frontend 8080:8080
# Open http://localhost:8080
```

## Deployment Scenarios

### Scenario A: No authentication

Suitable for air-gapped or trusted internal clusters.

```yaml
# values-no-auth.yaml
auth:
  type: none

ingress:
  enabled: true
  hosts:
    - host: kubevirt.internal
      paths:
        - path: /
          pathType: Prefix
```

### Scenario B: External OIDC provider

Use an existing identity provider (Keycloak, Okta, Azure AD, Google, etc.):

```yaml
# values-external-oidc.yaml
auth:
  type: oidc
  oidc:
    issuer: "https://keycloak.example.com/realms/kubevirt"
    clientId: "kubevirt-ui"
    clientSecretRef:
      name: kubevirt-ui-oidc-secret
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

Create the OIDC client secret before installing:

```bash
kubectl create namespace kubevirt-ui
kubectl -n kubevirt-ui create secret generic kubevirt-ui-oidc-secret \
  --from-literal=client-secret='YOUR_CLIENT_SECRET'
```

Install:

```bash
helm install kubevirt-ui ./helm/kubevirt-ui \
  -n kubevirt-ui -f values-external-oidc.yaml
```

### Scenario C: Bundled Dex + LLDAP (self-contained)

Full authentication stack with built-in user management. Good for air-gapped environments or when no external IdP is available.

```yaml
# values-bundled-auth.yaml
auth:
  type: oidc
  oidc:
    issuer: "https://kubevirt.example.com/dex"
    clientId: "kubevirt-ui"

dex:
  enabled: true
  storage:
    type: kubernetes  # use CRD-based storage for production

lldap:
  enabled: true
  existingSecret: kubevirt-lldap-credentials
  persistence:
    enabled: true
    size: 2Gi
    storageClass: ""  # uses default StorageClass

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

Create the LLDAP credentials secret:

```bash
kubectl create namespace kubevirt-ui
kubectl -n kubevirt-ui create secret generic kubevirt-lldap-credentials \
  --from-literal=admin-password='STRONG_PASSWORD_HERE' \
  --from-literal=jwt-secret='RANDOM_JWT_SECRET_HERE'
```

Install:

```bash
helm install kubevirt-ui ./helm/kubevirt-ui \
  -n kubevirt-ui -f values-bundled-auth.yaml
```

After deployment, access the LLDAP admin UI to create users:

```bash
kubectl -n kubevirt-ui port-forward svc/kubevirt-ui-lldap 17170:17170
# Open http://localhost:17170, log in with admin credentials
```

## RBAC Configuration

The chart creates a ServiceAccount with permissions to manage KubeVirt resources. Additionally, when `rbac.createRoles=true` (default), four user-facing ClusterRoles are created:

| ClusterRole | Use Case |
|-------------|----------|
| `kubevirt-ui-viewer` | Read-only access — monitoring, dashboards |
| `kubevirt-ui-editor` | Create/manage VMs — developer teams |
| `kubevirt-ui-admin` | Full VM + storage + network management |
| `kubevirt-ui-platform-admin` | Admin + node management + cluster settings |

Bind roles to users or groups:

```bash
# Grant admin access to a user
kubectl create clusterrolebinding kubevirt-admin-john \
  --clusterrole=kubevirt-ui-admin \
  --user=john@example.com

# Grant viewer access to a group
kubectl create clusterrolebinding kubevirt-viewers \
  --clusterrole=kubevirt-ui-viewer \
  --group=kubevirt-viewers
```

### Namespace-scoped RBAC

For multi-tenant setups where users should only see resources in their namespace:

```yaml
rbac:
  create: true
  clusterWide: false  # use namespace-scoped Role instead of ClusterRole
```

> **Note**: Namespace-scoped mode disables node listing and cross-namespace features.

## Metrics Configuration

The backend auto-discovers metrics endpoints in this order:

1. VMSingle CRD (VictoriaMetrics Operator)
2. Service with label `app.kubernetes.io/name: vmsingle`
3. Service with label `app.kubernetes.io/name: prometheus`
4. `METRICS_SERVICE` environment variable

To override auto-discovery:

```yaml
metrics:
  direct: "true"
  service: "monitoring/vmsingle-victoria:8429"
```

## Ingress

The ingress template routes traffic as follows:

| Path | Backend |
|------|---------|
| `/api/*`, `/health` | Backend service (:8000) |
| `/dex/*` | Dex service (:5556, if enabled) |
| Everything else | Frontend service (:8080) |

WebSocket connections (VNC and serial console) are supported with 86400s (24h) timeouts.

### Recommended annotations for nginx-ingress:

```yaml
ingress:
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "86400"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "86400"
    nginx.ingress.kubernetes.io/proxy-body-size: "0"
```

## Network Policy

Enable network policies to restrict traffic:

```yaml
networkPolicy:
  enabled: true
```

This creates NetworkPolicy resources allowing:
- Frontend: ingress from any source, egress to backend
- Backend: ingress from frontend, egress to Kubernetes API

## Upgrading

```bash
helm upgrade kubevirt-ui ./helm/kubevirt-ui -n kubevirt-ui -f my-values.yaml
```

## Troubleshooting

### Pods not starting

```bash
kubectl -n kubevirt-ui describe pod <pod-name>
kubectl -n kubevirt-ui logs <pod-name>
```

### Backend cannot reach Kubernetes API

Check the ServiceAccount and RBAC:

```bash
kubectl -n kubevirt-ui get serviceaccount
kubectl get clusterrolebinding | grep kubevirt-ui
```

### OIDC login not working

1. Verify the issuer URL is reachable from the browser
2. Check Dex logs (if using bundled Dex): `kubectl -n kubevirt-ui logs -l app.kubernetes.io/component=dex`
3. Ensure `auth.oidc.issuer` matches the issuer in the OIDC discovery document
4. If backend and IdP are on different networks, set `auth.oidc.issuerInternal`

### WebSocket console not connecting

1. Ensure ingress has WebSocket timeout annotations
2. Check that the backend ServiceAccount has permissions for `virtualmachineinstances/vnc` and `virtualmachineinstances/console` subresources
