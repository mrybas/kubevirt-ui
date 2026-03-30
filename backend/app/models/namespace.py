"""Namespace Pydantic models."""

from pydantic import BaseModel, Field


class NamespaceResponse(BaseModel):
    """Response model for a Namespace."""

    name: str
    status: str
    labels: dict[str, str] = Field(default_factory=dict)
    created: str | None = None


class NamespaceListResponse(BaseModel):
    """Response model for listing Namespaces."""

    items: list[NamespaceResponse]
    total: int
