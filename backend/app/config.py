"""Application configuration."""

import os
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # API
    api_prefix: str = "/api/v1"
    api_title: str = "KubeVirt UI API"
    api_version: str = "0.1.0"

    # CORS
    # Default to empty (no CORS). Set CORS_ORIGINS="http://localhost:3333" in env.
    # Using "*" with allow_credentials=True is a browser security violation.
    cors_origins: str = ""

    # Kubernetes
    kubeconfig: str | None = None
    k8s_in_cluster: bool = False

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Auth
    auth_enabled: bool = False

    # Feature flags
    enable_tenants: bool = False

    # Auth — comma-separated list of group names whose members are KubeVirt UI
    # admins (full cluster-wide access). Set this via ADMIN_GROUPS env var to
    # match the names of groups returned by your IdP (e.g. "admins" for FreeIPA
    # default, "kubevirt-ui-admins" for our bundled LLDAP).
    admin_groups: str = "kubevirt-ui-admins"

    # Admins named individually, by email or username (ADMIN_USERS env var,
    # comma-separated). Groups are the right mechanism when the IdP emits
    # them — but not every one does. Dex's local password database emits no
    # groups at all, so with a group-only rule a deployment using it has no
    # way to make anyone an admin, and the whole admin half of the UI is
    # unreachable for everybody.
    admin_users: str = ""

    @property
    def admin_groups_list(self) -> list[str]:
        return [g.strip() for g in self.admin_groups.split(",") if g.strip()]

    @property
    def admin_users_list(self) -> list[str]:
        return [u.strip().lower() for u in self.admin_users.split(",") if u.strip()]

    # The aggregate every tenant VPC is carved out of (TENANT_SUPERNET env
    # var), e.g. "10.198.192.0/18". Tenant isolation is expressed as "drop
    # traffic whose peer is another tenant, allow everything else", and this
    # is what scopes that drop: without it the catch-all would take the
    # internet with it.
    #
    # No default on purpose. The right value is a property of the site's
    # addressing plan, and guessing it wrong is worse than not isolating:
    # too wide silently blackholes the cluster's own pod/service CIDRs, too
    # narrow leaves tenants reachable while the UI claims otherwise. When it
    # is unset the isolation ACLs are skipped and the VPC reports
    # `isolated: false`, so the gap is visible rather than assumed.
    tenant_supernet: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS origins from comma-separated string."""
        if self.cors_origins == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",")]

    @property
    def kubeconfig_path(self) -> str | None:
        """Get kubeconfig path, with fallback to KUBECONFIG env var."""
        return self.kubeconfig or os.environ.get("KUBECONFIG")


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
