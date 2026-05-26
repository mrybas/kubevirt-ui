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

    @property
    def admin_groups_list(self) -> list[str]:
        return [g.strip() for g in self.admin_groups.split(",") if g.strip()]

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
