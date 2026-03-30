"""Cluster Pydantic models."""

from typing import Any

from pydantic import BaseModel, Field


class NodeResourceUsage(BaseModel):
    """Per-node resource usage."""
    total: float
    used: float
    free: float
    percentage: float


class NodeResponse(BaseModel):
    """Response model for a Node."""

    name: str
    status: str
    roles: list[str] = Field(default_factory=list)
    version: str | None = None
    os: str | None = None
    cpu: str | None = None
    memory: str | None = None
    internal_ip: str | None = None
    cpu_usage: NodeResourceUsage | None = None
    memory_usage: NodeResourceUsage | None = None


class NodeListResponse(BaseModel):
    """Response model for listing Nodes."""

    items: list[NodeResponse]
    total: int


class ClusterStatusResponse(BaseModel):
    """Response model for cluster status."""

    kubevirt: dict[str, Any]
    cdi: dict[str, Any]
    nodes_count: int
    nodes_ready: int
