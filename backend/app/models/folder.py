"""Folder Pydantic models.

Architecture:
  - Folder = hierarchical grouping stored in ConfigMap (replaces flat Projects)
  - Folders support recursive nesting with RBAC inheritance
  - Environments (K8s namespaces) belong to a folder
  - Access at any folder level propagates to all descendants
"""

from __future__ import annotations

from typing import Literal

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
# Phase 2 — folder-level authorization (group-based access)
# ---------------------------------------------------------------------------

class FolderEnvAccessSpec(BaseModel):
    """Env-specific access (union with folder-level access for that env)."""

    admins: list[str] = []     # FreeIPA / OIDC group names
    members: list[str] = []
    viewers: list[str] = []


class FolderAccessSpec(BaseModel):
    """Folder-level access block (Phase 2).

    Stored on the folder ConfigMap entry under the `access` key. Empty
    block (or missing) means the folder is only accessible to global
    admins (legacy "global admins only" behaviour — no breakage).

    The lists hold group names exactly as they appear in the user's OIDC
    `groups` claim (no naming convention assumed).
    """

    admins: list[str] = []
    members: list[str] = []
    viewers: list[str] = []
    # Optional per-env overrides — union with folder-level lists.
    env_access: dict[str, FolderEnvAccessSpec] = {}


class FolderAccessPatchRequest(BaseModel):
    """Full-replace PATCH body for the folder access block.

    Any field left `None` is left untouched.  To clear a list, send `[]`.
    `env_access` is full-replace when provided.
    """

    admins: list[str] | None = None
    members: list[str] | None = None
    viewers: list[str] | None = None
    env_access: dict[str, FolderEnvAccessSpec] | None = None


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

    # What this folder's subtree already holds — its own environments plus its
    # children. The floor under any attempt to shrink the quota: room that is
    # spoken for cannot be handed to someone else.
    allocated: dict[str, str] | None = None

    # Ancestor chain from root to this folder (not including self)
    path: list[str] = []

    # Nested children (populated in tree mode)
    children: list[FolderResponse] = []

    # Environments (namespaces) directly under this folder
    environments: list[FolderEnvironmentResponse] = []

    # Aggregated stats (including descendants)
    total_vms: int = 0
    total_storage: str | None = None

    # Access summary (Phase 1 — derived from materialized RoleBindings).
    teams: list[str] = []
    users: list[str] = []

    # Access block (Phase 2 — folder + per-env group ACLs from ConfigMap).
    # `None` here means the folder ConfigMap entry has no `access` block —
    # legacy "global admins only" behaviour, no breakage.
    access: FolderAccessSpec | None = None


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

    # What the quota already has spoken for. The floor under any attempt to
    # shrink it — room that is in use cannot be handed to someone else.
    used_cpu: str | None = None
    used_memory: str | None = None
    used_storage: str | None = None


class QuotaReallocation(BaseModel):
    """Room taken from a sibling to make space for something new.

    Carries the sibling's *new* quota rather than a delta: a delta has to be
    applied to a number the client read a moment ago, and two admins
    rebalancing at once would each subtract from a stale total.
    """

    source: str
    kind: Literal["environment", "folder"] = "environment"
    cpu: str | None = None
    memory: str | None = None
    storage: str | None = None


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

    # Sibling quotas to shrink first, so "I need more than is free" is one
    # request instead of a rebalance the user has to remember to finish.
    reallocate: list[QuotaReallocation] = []


class SetEnvironmentQuotaRequest(BaseModel):
    """Replace one environment's quota."""

    cpu: str | None = None
    memory: str | None = None
    storage: str | None = None


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
