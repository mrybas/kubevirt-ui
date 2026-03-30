# Folders Design Document

## Overview

Replace flat Projects with hierarchical Folders. Folders support recursive nesting
(Department > Team > Group), RBAC inheritance (access to a folder grants access to
all children), image scoping (images visible downward through the tree), and quotas
(child cannot exceed parent).

## Current Architecture

| Concept     | Implementation                                           |
|-------------|----------------------------------------------------------|
| Project     | Key in ConfigMap `kubevirt-ui-projects` in `kubevirt-ui-system` |
| Environment | K8s namespace with labels `kubevirt-ui.io/project={name}` |
| RBAC        | RoleBindings in environment namespaces with managed labels |
| Images      | DataVolumes with `kubevirt-ui.io/scope=environment\|project` |
| Auth        | OIDC via Dex, K8s SubjectAccessReview                    |

## Target Architecture

### Data Model

A **Folder** is a node in a tree. Each folder can contain:
- Child folders (unlimited depth)
- Environments (K8s namespaces, leaf resources)

```
Root
 ├── Engineering (folder, quota: 64 CPU)
 │   ├── Platform Team (folder, quota: 32 CPU)
 │   │   ├── dev (environment/namespace)
 │   │   └── staging (environment/namespace)
 │   └── App Team (folder, quota: 32 CPU)
 │       └── dev (environment/namespace)
 └── QA (folder)
     └── testing (environment/namespace)
```

### Storage: ConfigMap `kubevirt-ui-folders`

Same pattern as projects. One ConfigMap key per folder, value is JSON:

```json
{
  "display_name": "Platform Team",
  "description": "Infrastructure platform team",
  "parent_id": "engineering",
  "created_by": "admin@example.com",
  "created_at": "2026-03-12T10:00:00Z",
  "quota": {
    "cpu": "32",
    "memory": "64Gi",
    "storage": "500Gi"
  }
}
```

- `parent_id: null` means root-level folder.
- Folder `name` (ConfigMap key) is a DNS-safe slug, globally unique.
- Tree is reconstructed by reading all entries and building parent→children index.

### Namespace Labeling

Environments keep existing labels, replacing project with folder:

```yaml
labels:
  kubevirt-ui.io/enabled: "true"
  kubevirt-ui.io/managed: "true"
  kubevirt-ui.io/folder: "platform-team"      # direct parent folder
  kubevirt-ui.io/environment: "dev"
```

### RBAC Inheritance

Access granted at a folder level propagates to **all descendant folders and their
environments**. Implementation:

1. When access is added to folder F:
   - Create RoleBinding in every environment namespace under F (recursively).
   - Label bindings with `kubevirt-ui.io/folder={F}` and `kubevirt-ui.io/access-scope=folder`.

2. When checking access for a user on folder F:
   - Walk up from F to root, collecting all folder IDs in the ancestor chain.
   - User has access if they have a binding in **any** ancestor folder.

3. When a new environment is created under folder F:
   - Collect all access bindings from F and all ancestors.
   - Create corresponding RoleBindings in the new namespace.

### Image Scoping

Images (DataVolumes) are scoped to a folder. Visibility rules:

- **Environment scope**: visible only in that namespace (unchanged).
- **Folder scope**: visible in all environments within that folder and **all descendant folders**.
- **Global scope** (future): visible everywhere.

When listing images for an environment in folder F:
1. Get environment-scoped images from the namespace.
2. Walk up from F to root, collecting folder-scoped images from each ancestor.
3. Merge and deduplicate.

Label: `kubevirt-ui.io/scope=folder`, `kubevirt-ui.io/folder={name}`.

### Quota Enforcement

- Each folder can have an optional quota (cpu, memory, storage).
- A child folder's quota must not exceed its parent's quota.
- Validation happens at folder creation/update time.
- Environment ResourceQuotas are created as before within namespace.
- Quota is a soft limit enforced by the UI (not by K8s admission).

## API Design

### Folders CRUD

```
GET    /api/v1/folders                    # List all folders (tree or flat)
POST   /api/v1/folders                    # Create folder
GET    /api/v1/folders/{name}             # Get folder with children + environments
PATCH  /api/v1/folders/{name}             # Update folder metadata/quota
DELETE /api/v1/folders/{name}             # Delete folder (must be empty or cascade)
```

### Folder Environments

```
POST   /api/v1/folders/{name}/environments                  # Add environment
DELETE /api/v1/folders/{name}/environments/{environment}     # Remove environment
```

### Folder Access (RBAC)

```
GET    /api/v1/folders/{name}/access      # List access (includes inherited)
POST   /api/v1/folders/{name}/access      # Add access
DELETE /api/v1/folders/{name}/access/{id}  # Remove access
```

### Folder Move

```
POST   /api/v1/folders/{name}/move        # Move folder to new parent
```

### Query Parameters

- `GET /folders?flat=true` — flat list (default: tree structure)
- `GET /folders/{name}?depth=2` — limit children depth

## Pydantic Models

### Request Models

```python
class FolderCreateRequest(BaseModel):
    name: str           # DNS-safe slug
    display_name: str
    description: str = ""
    parent_id: str | None = None   # null = root
    environments: list[str] = []   # initial environments
    quota: FolderQuota | None = None

class FolderUpdateRequest(BaseModel):
    display_name: str | None = None
    description: str | None = None
    quota: FolderQuota | None = None

class FolderMoveRequest(BaseModel):
    new_parent_id: str | None = None  # null = move to root

class FolderQuota(BaseModel):
    cpu: str | None = None
    memory: str | None = None
    storage: str | None = None
```

### Response Models

```python
class FolderResponse(BaseModel):
    name: str
    display_name: str
    description: str
    parent_id: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    quota: FolderQuota | None = None
    path: list[str] = []            # ancestor chain: ["root", "engineering"]
    children: list["FolderResponse"] = []
    environments: list[EnvironmentResponse] = []
    total_vms: int = 0
    total_storage: str | None = None
    teams: list[str] = []
    users: list[str] = []

class FolderTreeResponse(BaseModel):
    items: list[FolderResponse]     # root-level folders with nested children
    total: int                      # total folder count (flat)

class FolderListResponse(BaseModel):
    items: list[FolderResponse]     # flat list
    total: int
```

## Migration Plan

### Phase 1: Add Folders API (non-breaking)

1. Create `kubevirt-ui-folders` ConfigMap alongside `kubevirt-ui-projects`.
2. Implement `/api/v1/folders` endpoints.
3. Keep `/api/v1/projects` working as-is.

### Phase 2: Migrate Projects to Folders

1. For each project in `kubevirt-ui-projects`:
   - Create a root-level folder with the same name/metadata.
   - Update environment namespace labels: add `kubevirt-ui.io/folder={name}`.
   - Copy RBAC bindings with folder labels.
2. Keep project ConfigMap as read-only backup.

### Phase 3: Backward Compatibility

1. `/api/v1/projects` endpoints proxy to folders API:
   - `GET /projects` → list root-level folders.
   - `POST /projects` → create root-level folder.
   - Project environments → folder environments.
   - Project access → folder access.
2. Frontend switches to folders API.
3. Deprecate projects API after transition.

## Label Summary

| Label                             | Value                    | Used On        |
|-----------------------------------|--------------------------|----------------|
| `kubevirt-ui.io/folder`           | folder name              | Namespace, RoleBinding, DataVolume |
| `kubevirt-ui.io/access-scope`     | `folder` / `environment` | RoleBinding    |
| `kubevirt-ui.io/scope`            | `folder` / `environment` | DataVolume     |
| `kubevirt-ui.io/managed`          | `true`                   | All managed resources |
| `kubevirt-ui.io/enabled`          | `true`                   | Namespace      |
| `kubevirt-ui.io/environment`      | environment name         | Namespace      |

## Key Decisions

1. **ConfigMap storage** (not database) — consistent with existing project/template storage.
2. **Tree in single ConfigMap** — all folders in one ConfigMap, tree built in-memory. Simpler than nested ConfigMaps.
3. **RBAC propagation** — eagerly create RoleBindings in all descendant namespaces. Matches existing project pattern. Alternative (lazy SAR check) would require SubjectAccessReview per request.
4. **Namespace naming** — `{folder}-{environment}` for direct children. For deep nesting, use `{leaf-folder}-{environment}` to avoid exceeding 63-char limit.
5. **No circular references** — validated at create/move time by walking parent chain.
