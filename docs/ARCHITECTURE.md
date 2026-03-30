# KubeVirt UI - Architecture Document

## Overview

KubeVirt UI is a lightweight web interface for managing virtual machines in KubeVirt.
The project follows a "Kubernetes-native" philosophy — Kubernetes API is the single source of truth.

```
┌─────────────────────────────────────────────────────────────────────┐
│                           User Browser                              │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      KubeVirt UI Frontend                           │
│                   (React + TypeScript + Vite)                       │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────────┐    │
│  │   VMs   │ │ Storage │ │ Network │ │  Nodes  │ │ YAML Editor │    │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│                       KubeVirt UI Backend                          │
│                      (Python + FastAPI)                            │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    REST API Layer                           │   │
│  │  /api/v1/vms  /api/v1/storage  /api/v1/network  /api/v1/... │   │
│  └─────────────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │              Kubernetes Client Layer                        │   │
│  │  • ServiceAccount Auth (in-cluster)                         │   │
│  │  • Kubeconfig Auth (external)                               │   │
│  │  • RBAC Validation (SubjectAccessReview)                    │   │
│  │  • Watch/Informers for real-time updates                    │   │
│  └─────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│                      Kubernetes Cluster                            │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐   │
│  │  KubeVirt   │ │     CDI     │ │  Kube-OVN   │ │   Longhorn  │   │
│  │  (VMs/VMIs) │ │(DataVolumes)│ │  (Network)  │ │  (Storage)  │   │
│  └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘   │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐   │
│  │ ConfigMaps  │ │   Secrets   │ │    RBAC     │ │ Namespaces  │   │
│  │(Projects,   │ │(Credentials)│ │   (ACL)     │ │(Environments│   │
│  │ Templates,  │ │             │ │             │ │  per project│   │
│  │ Profiles)   │ │             │ │             │ │           ) │   │
│  └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘   │
└────────────────────────────────────────────────────────────────────┘
```

## Key Principles

### 1. Kubernetes as the Single Source of Truth

| Need | Solution |
|---------|---------|
| Configuration storage | ConfigMap, Secret |
| Authentication | OIDC (DEX), ServiceAccount, Token |
| Authorization | RBAC (ClusterRole + RoleBinding per namespace) |
| Projects | ConfigMap `kubevirt-ui-projects` |
| Environments | Kubernetes Namespaces with labels |
| User profiles | ConfigMap `kubevirt-ui-profiles` (SSH keys) |
| VM Templates | ConfigMap `kubevirt-ui-templates` |
| Golden Images | DataVolumes per namespace |
| Networking | Kube-OVN CRDs (ProviderNetwork, Vlan, Subnet, IP) |

### 2. Stateless Backend

The backend stores no data locally:
- No databases (PostgreSQL, MySQL, SQLite)
- No caches (Redis, Memcached)
- No file storage
- All data is read from and written to the Kubernetes API

### 3. Security by Default

- Minimal RBAC privileges
- SubjectAccessReview for user permission checks
- Namespace-based isolation
- Air-gapped environment support

---

## Components

### Backend (Python + FastAPI)

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app factory, CORS, lifespan
│   ├── config.py               # Settings from env vars (pydantic-settings)
│   ├── api/
│   │   └── v1/
│   │       ├── router.py       # Aggregates all routers under /api/v1
│   │       ├── vms.py          # VM CRUD, start/stop/restart, VNC/serial WebSocket proxy
│   │       ├── storage.py      # DataVolumes, PVCs, StorageClasses
│   │       ├── network.py      # Kube-OVN: ProviderNetworks, VLANs, Subnets, IPs
│   │       ├── templates.py    # VM templates (ConfigMap), golden images (DataVolumes)
│   │       ├── projects.py     # Projects (ConfigMap) + Environments (Namespaces) + RBAC
│   │       ├── profile.py      # User profile, SSH public keys management
│   │       ├── disks.py        # Persistent disks management
│   │       ├── cluster.py      # Cluster status, nodes, KubeVirt/CDI status
│   │       ├── namespaces.py   # Namespace listing
│   │       └── auth.py         # Auth config endpoint, OIDC token exchange
│   ├── core/
│   │   ├── k8s_client.py       # Async K8s client wrapper (kubernetes-asyncio)
│   │   ├── auth.py             # OIDC/token/none auth, User dataclass, require_auth
│   │   └── groups.py           # Dev group mappings, known teams
│   └── models/
│       ├── vm.py               # VM Pydantic models
│       ├── storage.py          # Storage Pydantic models
│       ├── network.py          # Network Pydantic models (Kube-OVN)
│       ├── template.py         # Template & golden image models
│       ├── project.py          # Project, Environment, Access models
│       ├── cluster.py          # Cluster status models
│       └── namespace.py        # Namespace models
├── requirements.txt
└── Dockerfile
```

### Frontend (React + TypeScript + Vite)

```
frontend/
├── src/
│   ├── main.tsx                    # React root with QueryClient, BrowserRouter
│   ├── App.tsx                     # Routes and ProtectedRoute component
│   ├── api/
│   │   ├── client.ts              # apiRequest() fetch wrapper with auth headers
│   │   ├── vms.ts                 # VM API calls
│   │   ├── storage.ts             # Storage API calls
│   │   ├── network.ts             # Kube-OVN network API calls
│   │   ├── templates.ts           # VM templates & golden images
│   │   ├── projects.ts            # Projects, environments, access
│   │   ├── profile.ts             # User profile & SSH keys
│   │   ├── cluster.ts             # Cluster status
│   │   └── auth.ts                # Auth config, OIDC token exchange
│   ├── components/
│   │   ├── layout/
│   │   │   ├── Layout.tsx         # Main layout wrapper
│   │   │   ├── Sidebar.tsx        # Navigation sidebar
│   │   │   └── Header.tsx         # Top header with namespace picker
│   │   ├── common/
│   │   │   └── Notifications.tsx  # Toast notifications
│   │   ├── vm/                    # VM-specific components
│   │   └── network/
│   │       └── CreateNetworkWizard.tsx  # Multi-step network creation
│   ├── hooks/
│   │   ├── useVMs.ts              # VM queries with auto-refresh
│   │   ├── useStorage.ts          # Storage queries & mutations
│   │   ├── useNetwork.ts          # Kube-OVN network hooks
│   │   ├── useTemplates.ts        # Templates & golden images hooks
│   │   ├── useProjects.ts         # Projects, environments, access hooks
│   │   ├── useProfile.ts          # User profile hooks
│   │   └── useNamespaces.ts       # Namespace listing
│   ├── pages/
│   │   ├── Dashboard.tsx          # Overview dashboard
│   │   ├── VirtualMachines.tsx    # VM list page
│   │   ├── VMDetail.tsx           # VM detail/settings page
│   │   ├── VMTemplates.tsx        # Templates & golden images management
│   │   ├── Storage.tsx            # DataVolumes, PVCs management
│   │   ├── Network.tsx            # Kube-OVN: providers → VLANs → subnets
│   │   ├── NetworkDetail.tsx      # Subnet detail with IP leases
│   │   ├── SystemNetworks.tsx     # System-level network overview
│   │   ├── Projects.tsx           # Projects & environments management
│   │   ├── Profile.tsx            # User profile & SSH keys
│   │   ├── Cluster.tsx            # Cluster status & nodes
│   │   ├── Login.tsx              # Login page
│   │   └── AuthCallback.tsx       # OIDC callback handler
│   ├── store/
│   │   ├── index.ts               # Zustand: selectedNamespace
│   │   ├── auth.ts                # Zustand+persist: auth state
│   │   ├── notifications.ts       # Zustand: toast notifications
│   │   └── theme.ts               # Zustand: dark/light/system theme
│   ├── types/
│   │   ├── vm.ts                  # VM types
│   │   ├── storage.ts             # Storage types
│   │   ├── network.ts             # Kube-OVN network types
│   │   ├── template.ts            # Template types
│   │   ├── project.ts             # Project, Environment, Access types
│   │   ├── cluster.ts             # Cluster types
│   │   └── auth.ts                # Auth types
│   └── styles/
│       └── globals.css            # Tailwind CSS + custom styles
├── index.html
├── package.json
├── tsconfig.json
├── vite.config.ts                 # Vite proxy: /api → backend:8000
└── Dockerfile
```

---

## Projects & Environments

### Model

```
┌─────────────────────────────────────────────────────────────────┐
│  Project (logical grouping, no own namespace)                   │
│  Stored in ConfigMap "kubevirt-ui-projects"                     │
│  in namespace "kubevirt-ui-system"                              │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  Project: "analytics"                                     │  │
│  │  display_name: "Analytics Platform"                       │  │
│  │  description: "Data analytics project"                    │  │
│  │                                                           │  │
│  │  ┌──────────────────┐ ┌──────────────────┐ ┌────────────┐ │  │
│  │  │ Environment: dev │ │Environment: stg  │ │ Env: prod  │ │  │
│  │  │ NS: analytics-dev│ │NS: analytics-stg │ │NS: a-prod  │ │  │
│  │  └──────────────────┘ └──────────────────┘ └────────────┘ │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  Project: "webshop"                                       │  │
│  │  ...                                                      │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

- **Project** = logical grouping, ConfigMap entry. Does not create a namespace.
- **Environment** = K8s namespace within a project. Format: `{project}-{environment}`.
- Namespace labels: `kubevirt-ui.io/project`, `kubevirt-ui.io/environment`, `kubevirt-ui.io/enabled`, `kubevirt-ui.io/managed`.

### Access Control (RBAC)

```
┌─────────────────────────────────────────────────────────────────┐
│                    Cluster Level                                │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  ClusterRole: kubevirt-ui-admin                         │    │
│  │  ClusterRole: kubevirt-ui-editor                        │    │
│  │  ClusterRole: kubevirt-ui-viewer                        │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  Project-level access (scope=project)                           │
│  RoleBinding created in EVERY environment namespace             │
│  Label: kubevirt-ui.io/access-scope=project                     │
│                                                                 │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  │
│  │ analytics-dev   │  │ analytics-stg   │  │ analytics-prod  │  │
│  │ user-a → admin  │  │ user-a → admin  │  │ user-a → admin  │  │
│  │ team-x → editor │  │ team-x → editor │  │ team-x → editor │  │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘  │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  Environment-level access (scope=environment)                   │
│  RoleBinding only in the specific namespace                     │
│  Label: kubevirt-ui.io/access-scope=environment                 │
│                                                                 │
│  ┌─────────────────┐                                            │
│  │ analytics-prod  │                                            │
│  │ user-b → viewer │  ← prod only, no access to dev/stg         │
│  └─────────────────┘                                            │
└─────────────────────────────────────────────────────────────────┘
```

- When adding a new environment, project-level bindings are automatically copied from a sibling namespace.

### ClusterRoles

#### kubevirt-ui-admin
```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kubevirt-ui-admin
rules:
  # KubeVirt resources
  - apiGroups: ["kubevirt.io"]
    resources: ["virtualmachines", "virtualmachineinstances", 
                "virtualmachineinstancemigrations"]
    verbs: ["*"]
  # CDI resources
  - apiGroups: ["cdi.kubevirt.io"]
    resources: ["datavolumes", "datasources"]
    verbs: ["*"]
  # Core resources
  - apiGroups: [""]
    resources: ["persistentvolumeclaims", "configmaps", "secrets"]
    verbs: ["*"]
  - apiGroups: [""]
    resources: ["events", "pods", "pods/log"]
    verbs: ["get", "list", "watch"]
  # Snapshots
  - apiGroups: ["snapshot.kubevirt.io"]
    resources: ["virtualmachinesnapshots", "virtualmachinerestores"]
    verbs: ["*"]
```

#### kubevirt-ui-viewer
```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kubevirt-ui-viewer
rules:
  - apiGroups: ["kubevirt.io"]
    resources: ["virtualmachines", "virtualmachineinstances"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["cdi.kubevirt.io"]
    resources: ["datavolumes", "datasources"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["persistentvolumeclaims", "events"]
    verbs: ["get", "list", "watch"]
```

### SubjectAccessReview

The backend uses SubjectAccessReview to verify user permissions:

```python
async def can_user_perform_action(
    user: str,
    verb: str,
    resource: str,
    namespace: str
) -> bool:
    review = {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SubjectAccessReview",
        "spec": {
            "user": user,
            "resourceAttributes": {
                "namespace": namespace,
                "verb": verb,
                "group": "kubevirt.io",
                "resource": resource
            }
        }
    }
    result = await k8s_client.create_subject_access_review(review)
    return result.status.allowed
```

---

## Data Flows

### 1. Authentication

```
┌──────────┐     ┌──────────┐     ┌──────────────┐     ┌─────────────┐
│  Browser │────▶│ Frontend │────▶│   Backend    │────▶│ K8s API     │
└──────────┘     └──────────┘     └──────────────┘     └─────────────┘
                                         │
                 ┌───────────────────────┴─────────────────────────┐
                 │  In-cluster:                                    │
                 │    ServiceAccount token from                    │
                 │    /var/run/secrets/kubernetes.io/serviceaccount│
                 │                                                 │
                 │  External:                                      │
                 │    kubeconfig file / KUBECONFIG env             │
                 │    OIDC token from request header               │
                 └─────────────────────────────────────────────────┘
```

### 2. VM Listing

```
┌──────────┐     ┌──────────┐     ┌──────────────┐     ┌─────────────┐
│  Browser │────▶│ Frontend │────▶│ GET /api/v1/ │────▶│kubectl get  │
│          │     │          │     │ namespaces/  │     │vm -n ns     │
│          │     │          │     │ {ns}/vms     │     │             │
└──────────┘     └──────────┘     └──────────────┘     └─────────────┘
     ▲                                   │
     │                                   ▼
     │           ┌────────────────────────────────────────┐
     └───────────│ Response: [{name, namespace, status,   │
                 │            cpu, memory, ip, node}]     │
                 └────────────────────────────────────────┘
```

### 3. Real-time Updates (WebSocket)

```
┌──────────┐     ┌──────────┐     ┌──────────────┐     ┌─────────────┐
│  Browser │◀───▶│ Frontend │◀───▶│ WS /api/v1/  │◀───▶│ K8s Watch   │
│          │     │          │     │ watch/vms    │     │ API         │
└──────────┘     └──────────┘     └──────────────┘     └─────────────┘
                                         │
                 ┌───────────────────────┴────────────────────────┐
                 │  Events: ADDED, MODIFIED, DELETED              │
                 │  VM changes pushed to all connected clients    │
                 └────────────────────────────────────────────────┘
```

### 4. VM Creation (Wizard Flow)

```
Frontend                    Backend                     Kubernetes
    │                          │                            │
    │  POST /api/v1/vms        │                            │
    │  {wizard_data}           │                            │
    │─────────────────────────▶│                            │
    │                          │  Validate RBAC             │
    │                          │  (SubjectAccessReview)     │
    │                          │───────────────────────────▶│
    │                          │◀───────────────────────────│
    │                          │                            │
    │                          │  Create DataVolume         │
    │                          │  (if disk import needed)   │
    │                          │───────────────────────────▶│
    │                          │◀───────────────────────────│
    │                          │                            │
    │                          │  Create VirtualMachine     │
    │                          │───────────────────────────▶│
    │                          │◀───────────────────────────│
    │                          │                            │
    │  Response: VM created    │                            │
    │◀─────────────────────────│                            │
    │                          │                            │
    │  WS: VM status updates   │  Watch VM                  │
    │◀─────────────────────────│◀───────────────────────────│
```

---

## State Storage

All data is stored in Kubernetes. No external databases.

### ConfigMaps in namespace `kubevirt-ui-system`

| ConfigMap | Purpose |
|-----------|-------------|
| `kubevirt-ui-projects` | Project metadata (name, display_name, description, created_by) |
| `kubevirt-ui-templates` | VM templates (JSON configurations for VM creation) |
| `kubevirt-ui-profiles` | User profiles (SSH keys). Key = user email |

### Projects (ConfigMap)

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: kubevirt-ui-projects
  namespace: kubevirt-ui-system
  labels:
    kubevirt-ui.io/managed: "true"
data:
  analytics: |
    {"display_name": "Analytics Platform", "description": "Data analytics", "created_by": "admin@example.com"}
  webshop: |
    {"display_name": "Webshop", "description": "E-commerce", "created_by": "admin@example.com"}
```

### Profiles (ConfigMap)

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: kubevirt-ui-profiles
  namespace: kubevirt-ui-system
data:
  admin@example.com: |
    {"ssh_keys": ["ssh-rsa AAAA...", "ssh-ed25519 AAAA..."]}
```

SSH keys from the profile are automatically injected into cloud-init during VM creation.

### Environments (Namespaces)

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: analytics-dev
  labels:
    kubevirt-ui.io/enabled: "true"
    kubevirt-ui.io/managed: "true"
    kubevirt-ui.io/project: analytics
    kubevirt-ui.io/environment: dev
```

---

## API Specification

### Base URL
```
/api/v1
```

### Endpoints

#### Virtual Machines
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/vms?namespace={ns}` | List VMs (optional namespace filter) |
| GET | `/vms/{namespace}/{name}` | Get VM details |
| POST | `/vms` | Create VM (from template or manual) |
| DELETE | `/vms/{namespace}/{name}` | Delete VM |
| POST | `/vms/{namespace}/{name}/start` | Start VM |
| POST | `/vms/{namespace}/{name}/stop` | Stop VM (graceful/force) |
| POST | `/vms/{namespace}/{name}/restart` | Restart VM |
| PATCH | `/vms/{namespace}/{name}/resize-disk` | Resize VM disk |
| POST | `/vms/{namespace}/{name}/create-image` | Create image from VM disk |
| WS | `/vms/{namespace}/{name}/vnc` | VNC console WebSocket proxy |
| WS | `/vms/{namespace}/{name}/console` | Serial console WebSocket proxy |

#### Templates & Golden Images
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/templates` | List VM templates |
| POST | `/templates` | Create template |
| PUT | `/templates/{name}` | Update template |
| DELETE | `/templates/{name}` | Delete template |
| GET | `/templates/golden-images?namespace={ns}` | List golden images |
| POST | `/templates/golden-images` | Create golden image (DataVolume) |
| DELETE | `/templates/golden-images/{namespace}/{name}` | Delete golden image |

#### Storage
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/storage/datavolumes?namespace={ns}` | List DataVolumes |
| POST | `/storage/datavolumes` | Create DataVolume |
| DELETE | `/storage/datavolumes/{namespace}/{name}` | Delete DataVolume |
| GET | `/storage/pvcs?namespace={ns}` | List PVCs |
| GET | `/storage/storageclasses` | List StorageClasses |

#### Persistent Disks
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/disks?namespace={ns}` | List persistent disks |
| POST | `/disks` | Create persistent disk |
| DELETE | `/disks/{namespace}/{name}` | Delete persistent disk |

#### Network (Kube-OVN)
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/network/provider-networks` | List ProviderNetworks |
| POST | `/network/provider-networks` | Create ProviderNetwork |
| DELETE | `/network/provider-networks/{name}` | Delete ProviderNetwork |
| GET | `/network/vlans` | List VLANs |
| POST | `/network/vlans` | Create VLAN |
| DELETE | `/network/vlans/{name}` | Delete VLAN |
| GET | `/network/subnets` | List Subnets |
| POST | `/network/subnets` | Create Subnet |
| DELETE | `/network/subnets/{name}` | Delete Subnet |
| GET | `/network/subnets/{name}/ips` | List IP leases |
| POST | `/network/subnets/{name}/reserve` | Reserve IP |
| DELETE | `/network/ips/{name}` | Release IP |

#### Projects & Environments
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/projects` | List all projects |
| POST | `/projects` | Create project (with optional initial environments) |
| GET | `/projects/{name}` | Get project with environments |
| DELETE | `/projects/{name}` | Delete project + all environments |
| POST | `/projects/{name}/environments` | Add environment (creates namespace) |
| DELETE | `/projects/{name}/environments/{env}` | Remove environment |
| GET | `/projects/{name}/access` | List access entries |
| POST | `/projects/{name}/access` | Add access (project or env scope) |
| DELETE | `/projects/{name}/access/{id}` | Remove access |

#### Profile
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/profile` | Get user profile (SSH keys) |
| PUT | `/profile/ssh-keys` | Update SSH public keys |

#### Cluster
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/cluster/status` | KubeVirt/CDI status |
| GET | `/cluster/nodes` | List nodes |
| GET | `/namespaces` | List namespaces |

#### Auth
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/auth/config` | Get auth configuration |
| POST | `/auth/token` | Exchange OIDC code for token |

---

## Technologies

### Backend
- **Python 3.12+**
- **FastAPI** — async web framework
- **kubernetes-asyncio** — async K8s client
- **pydantic** — data validation
- **uvicorn** — ASGI server
- **websockets** — WebSocket support

### Frontend
- **React 18+** — UI library
- **TypeScript** — type safety
- **Vite** — build tool
- **TanStack Query** — data fetching
- **Zustand** — state management
- **Monaco Editor** — YAML editor
- **xterm.js** — terminal emulator (console)
- **noVNC** — VNC client
- **Tailwind CSS** — styling

### Infrastructure
- **Docker** — containerization
- **Helm** — Kubernetes deployment
- **nginx** — frontend serving

---

## Deployment Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Kubernetes Cluster                       │
│  ┌──────────────────────────────────────────────────────┐   │
│  │           Namespace: kubevirt-ui                     │   │
│  │  ┌───────────────┐  ┌───────────────┐                │   │
│  │  │  Deployment   │  │  Deployment   │                │   │
│  │  │  (Backend)    │  │  (Frontend)   │                │   │
│  │  │  replicas: 2  │  │  replicas: 2  │                │   │
│  │  └───────────────┘  └───────────────┘                │   │
│  │         │                   │                        │   │
│  │         ▼                   ▼                        │   │
│  │  ┌───────────────┐  ┌───────────────┐                │   │
│  │  │   Service     │  │   Service     │                │   │
│  │  │  (backend)    │  │  (frontend)   │                │   │
│  │  └───────────────┘  └───────────────┘                │   │
│  │              │               │                       │   │
│  │              └───────┬───────┘                       │   │
│  │                      ▼                               │   │
│  │              ┌───────────────┐                       │   │
│  │              │    Ingress    │                       │   │
│  │              │ kubevirt-ui   │                       │   │
│  │              └───────────────┘                       │   │
│  │                                                      │   │
│  │  ┌───────────────┐  ┌───────────────┐                │   │
│  │  │ ServiceAccount│  │  ConfigMap    │                │   │
│  │  │ kubevirt-ui   │  │  ui-config    │                │   │
│  │  └───────────────┘  └───────────────┘                │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

---

## Security

### Network Policies

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: kubevirt-ui-backend
  namespace: kubevirt-ui
spec:
  podSelector:
    matchLabels:
      app: kubevirt-ui-backend
  policyTypes:
    - Ingress
    - Egress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: kubevirt-ui-frontend
      ports:
        - port: 8000
  egress:
    - to:
        - namespaceSelector: {}
      ports:
        - port: 443  # K8s API
        - port: 6443
```

### Pod Security Standards

```yaml
apiVersion: v1
kind: Pod
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    fsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: backend
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop:
            - ALL
```

---

## Air-gapped Support

For operation in isolated environments:

1. **Container Images** — all images must be available in a local registry
2. **No External Dependencies** — UI does not load anything from external CDNs
3. **Offline Helm Install** — supports `helm install --set image.registry=local-registry`
4. **Bundled Assets** — all JS/CSS/fonts are included in the Docker image

---

## Monitoring

### Health Endpoints

```
GET /health          # Liveness probe
GET /health/ready    # Readiness probe (K8s API connectivity)
```

### Metrics (Prometheus)

```
GET /metrics

# Metrics:
kubevirt_ui_requests_total{method, endpoint, status}
kubevirt_ui_request_duration_seconds{method, endpoint}
kubevirt_ui_k8s_api_calls_total{resource, verb}
kubevirt_ui_active_websockets
```
