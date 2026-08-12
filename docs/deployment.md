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

Every request is served as an anonymous admin, and `auth.adminGroups` is
ignored — with authentication off there is no identity to check a group
against. Anyone who can reach the UI has full cluster access.

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

## Tenant VPC Isolation

Separate VPCs are separate routing domains, but every tenant prefix is
announced to the same upstream router, and that router forwards between them.
Without ACLs a VPC is reachable from every other tenant — the traffic simply
leaves and comes back:

```
 1  10.198.224.1     t1 VPC router
 2  10.198.224.7     t1 egress gateway
 3  10.198.191.254   upstream router      <- hairpin
 4  10.198.190.211   t2 egress gateway
 5  10.198.240.4     t2 pod
```

The VPC wizard's **Isolated** checkbox (on by default) closes this at
creation. It writes one rule set on the VPC's default subnet:

| Priority | Rule |
|----------|------|
| 3200 | allow the VPC's own CIDR |
| 3100+ | allow each prefix listed in **Shared networks** |
| 3000 | drop everything inside `TENANT_SUPERNET` |

The drop is scoped, not universal, so traffic leaving tenant space — the
internet — matches no rule and stays allowed:

```
t1 -> internet OK    t1 -> shared OK    t1 -> t2 BLOCKED
```

### Configuring TENANT_SUPERNET

```yaml
backend:
  env:
    TENANT_SUPERNET: "10.198.192.0/18"
```

**Isolation does nothing until this is set.** There is no default, because
the right value is a property of your addressing plan and a wrong guess is
worse than no isolation at all. With it unset, VPCs are created without
isolation ACLs, the backend logs a warning, and `GET /api/v1/vpcs` reports
`isolated: false`.

Two requirements, both hard. The aggregate must:

- **contain every tenant VPC CIDR**, present and future — new tenants fall
  under the existing drop automatically, so no other VPC has to be edited
  when one is added;
- **contain nothing else.** Overlap the cluster's pod CIDR and VPC workloads
  lose DNS (VpcDns forwards to CoreDNS *pod* IPs). Overlap the service CIDR
  and tenant clusters cannot reach their own control plane. Overlap the node
  or management network and you will be debugging it for a while.

So carve tenants a dedicated block rather than reusing a wide one like
`10.0.0.0/8` or all of RFC1918: everything inside the aggregate needs an
explicit allow to work, and each allow is a hole to maintain.

Check a candidate against reality before setting it:

```bash
kubectl get subnet -o custom-columns=NAME:.metadata.name,CIDR:.spec.cidrBlock
kubectl cluster-info dump | grep -m1 cluster-cidr            # pod CIDR
kubectl cluster-info dump | grep -m1 service-cluster-ip-range
```

If the aggregate covers either of the last two, pick a narrower one.

Note that the built-in VPC CIDR allocator hands out `10.{200+N}.0.0/24`,
whose smallest aggregate (`10.192.0.0/10`) also swallows the common
`10.244.0.0/16` pod CIDR. If you rely on the allocator, either set
`TENANT_SUPERNET` to a block you have verified is tenant-only, or create VPCs
with an explicit `subnet_cidr` inside your chosen aggregate — overlaps are
rejected at creation either way.

### Shared services

Prefixes a tenant may still reach while isolated (corporate git, a package
mirror) go in the wizard's **Shared networks** field, or `shared_cidrs` on
the create request. They become higher-priority allows above the drop.

### Not `Subnet.spec.private`

Kube-OVN's `private: true` looks like the knob for this and is not. It does
block tenant-to-tenant, but it also drops return traffic from the internet
(8.8.8.8 is not in `allowSubnets`), so the tenant silently loses egress.

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
