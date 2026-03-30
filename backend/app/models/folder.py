"""Folder Pydantic models.

Architecture:
  - Folder = hierarchical grouping stored in ConfigMap (replaces flat Projects)
  - Folders support recursive nesting with RBAC inheritance
  - Environments (K8s namespaces) belong to a folder
  - Access at any folder level propagates to all descendants
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Folder quota
# ---------------------------------------------------------------------------

class FolderQuota(BaseModel):
    """Optional folder-level quota (soft limit enforced by UI)."""

    cpu: str | None = None        # e.g. "16"
    memory: str | None = None     # e.g. "32Gi"
    storage: str | None = None    # e.g. "200Gi"


# ---------------------------------------------------------------------------
# Folder CRUD requests
# ---------------------------------------------------------------------------

class FolderCreateRequest(BaseModel):
    """Request to create a new folder."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
    )
    display_name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    parent_id: str | None = None  # null = root-level folder
    environments: list[str] = []  # optional initial environments
    quota: FolderQuota | None = None


class FolderUpdateRequest(BaseModel):
    """Request to update folder metadata."""

    display_name: str | None = None
    description: str | None = None
    quota: FolderQuota | None = None


class FolderMoveRequest(BaseModel):
    """Request to move a folder to a new parent."""

    new_parent_id: str | None = None  # null = move to root


# ---------------------------------------------------------------------------
# Folder response
# ---------------------------------------------------------------------------

class FolderResponse(BaseModel):
    """Folder information with optional nested children and environments."""

    name: str
    display_name: str
    description: str
    parent_id: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    quota: FolderQuota | None = None

    # Ancestor chain from root to this folder (not including self)
    path: list[str] = []

    # Nested children (populated in tree mode)
    children: list[FolderResponse] = []

    # Environments (namespaces) directly under this folder
    environments: list[FolderEnvironmentResponse] = []

    # Aggregated stats (including descendants)
    total_vms: int = 0
    total_storage: str | None = None

    # Access summary
    teams: list[str] = []
    users: list[str] = []


class FolderTreeResponse(BaseModel):
    """Tree of folders (root-level items with nested children)."""

    items: list[FolderResponse]
    total: int  # total folder count (flat)


class FolderListResponse(BaseModel):
    """Flat list of folders."""

    items: list[FolderResponse]
    total: int


# ---------------------------------------------------------------------------
# Environment (reuse from project, but with folder reference)
# ---------------------------------------------------------------------------

class FolderEnvironmentResponse(BaseModel):
    """Environment (namespace) within a folder."""

    name: str          # Full namespace name: {folder}-{environment}
    environment: str   # Short name: dev, staging, prod
    folder: str        # Parent folder name
    created: str | None = None

    # Stats
    vm_count: int = 0
    storage_used: str | None = None

    # Quotas
    quota_cpu: str | None = None
    quota_memory: str | None = None
    quota_storage: str | None = None


class AddFolderEnvironmentRequest(BaseModel):
    """Request to add an environment (namespace) to a folder."""

    environment: str = Field(
        ...,
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        description="Environment name, e.g. 'dev', 'staging', 'prod'",
    )
    quota_cpu: str | None = None
    quota_memory: str | None = None
    quota_storage: str | None = None


# ---------------------------------------------------------------------------
# Access (RBAC) — same structure as project access
# ---------------------------------------------------------------------------

class FolderAccessEntry(BaseModel):
    """Access entry for a folder or environment."""

    id: str        # RoleBinding name
    type: str      # "team" or "user"
    name: str      # Group or user name
    role: str      # "admin", "editor", "viewer"
    scope: str = "folder"  # "folder" or "environment"
    environment: str | None = None  # set if scope == "environment"
    folder: str | None = None       # folder where this binding originates
    inherited: bool = False         # true if inherited from ancestor
    created: str | None = None


class FolderAccessListResponse(BaseModel):
    """List of access entries."""

    items: list[FolderAccessEntry]
    total: int


class AddFolderAccessRequest(BaseModel):
    """Request to add access to a folder or environment."""

    type: str = Field(..., pattern=r"^(team|user)$")
    name: str = Field(..., min_length=1)
    role: str = Field(default="editor", pattern=r"^(admin|editor|viewer)$")
    scope: str = Field(default="folder", pattern=r"^(folder|environment)$")
    environment: str | None = None  # required if scope == "environment"


# Rebuild models to resolve forward references (FolderResponse references FolderEnvironmentResponse)
FolderResponse.model_rebuild()
