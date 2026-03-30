"""Project and Access Pydantic models.

Architecture:
  - Project = logical grouping (stored in ConfigMap, no own namespace)
  - Environment = K8s namespace belonging to a project
  - Access = RBAC at project level (all envs) or environment level
"""

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Project quota (optional soft limit)
# ---------------------------------------------------------------------------

class ProjectQuota(BaseModel):
    """Optional project-level quota (soft limit enforced by UI)."""
    
    cpu: str | None = None        # e.g. "16"
    memory: str | None = None     # e.g. "32Gi"
    storage: str | None = None    # e.g. "200Gi"


# ---------------------------------------------------------------------------
# Project (logical group, ConfigMap-based)
# ---------------------------------------------------------------------------

class ProjectCreateRequest(BaseModel):
    """Request to create a new project with optional initial environments."""
    
    name: str = Field(
        ..., 
        min_length=1, 
        max_length=63, 
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
    )
    display_name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    environments: list[str] = []  # e.g. ["dev", "staging", "prod"]
    quota: ProjectQuota | None = None  # optional project-level quota


class UpdateProjectRequest(BaseModel):
    """Request to update project metadata."""
    
    display_name: str | None = None
    description: str | None = None
    quota: ProjectQuota | None = None


class ProjectResponse(BaseModel):
    """Project information with nested environments."""
    
    name: str
    display_name: str
    description: str
    created_by: str | None = None
    
    # Optional project-level quota (soft limit)
    quota: ProjectQuota | None = None
    
    # Nested environments (namespaces)
    environments: list["EnvironmentResponse"] = []
    
    # Aggregated stats
    total_vms: int = 0
    total_storage: str | None = None
    
    # Project-level access summary
    teams: list[str] = []
    users: list[str] = []


class ProjectListResponse(BaseModel):
    """List of projects."""
    
    items: list[ProjectResponse]
    total: int


# ---------------------------------------------------------------------------
# Environment (K8s namespace within a project)
# ---------------------------------------------------------------------------

class AddEnvironmentRequest(BaseModel):
    """Request to add an environment (namespace) to a project."""
    
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


class EnvironmentResponse(BaseModel):
    """Environment (namespace) within a project."""
    
    name: str  # Full namespace name: {project}-{environment}
    environment: str  # Short name: dev, staging, prod
    project: str  # Parent project name
    created: str | None = None
    
    # Stats
    vm_count: int = 0
    storage_used: str | None = None
    
    # Quotas
    quota_cpu: str | None = None
    quota_memory: str | None = None
    quota_storage: str | None = None


# ---------------------------------------------------------------------------
# Access (RBAC)
# ---------------------------------------------------------------------------

class AccessEntry(BaseModel):
    """Access entry for a project or environment."""
    
    id: str  # RoleBinding name
    type: str  # "team" or "user"
    name: str  # Group or user name
    role: str  # "admin", "editor", "viewer"
    scope: str = "project"  # "project" or "environment"
    environment: str | None = None  # set if scope == "environment"
    created: str | None = None


class AccessListResponse(BaseModel):
    """List of access entries."""
    
    items: list[AccessEntry]
    total: int


class AddAccessRequest(BaseModel):
    """Request to add access to a project or environment."""
    
    type: str = Field(..., pattern=r"^(team|user)$")
    name: str = Field(..., min_length=1)
    role: str = Field(default="editor", pattern=r"^(admin|editor|viewer)$")
    scope: str = Field(default="project", pattern=r"^(project|environment)$")
    environment: str | None = None  # required if scope == "environment"


# ---------------------------------------------------------------------------
# Teams (from identity provider groups)
# ---------------------------------------------------------------------------

class TeamResponse(BaseModel):
    """Team information."""
    
    name: str
    display_name: str
    description: str = ""


class TeamListResponse(BaseModel):
    """List of teams."""
    
    items: list[TeamResponse]
    total: int


# ---------------------------------------------------------------------------
# Role mappings
# ---------------------------------------------------------------------------

ROLE_TO_CLUSTERROLE = {
    "admin": "kubevirt-ui-admin",
    "editor": "kubevirt-ui-editor",
    "viewer": "kubevirt-ui-viewer",
}

CLUSTERROLE_TO_ROLE = {v: k for k, v in ROLE_TO_CLUSTERROLE.items()}
