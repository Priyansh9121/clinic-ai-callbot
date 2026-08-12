"""Shared schema types."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    """Base for response models built from ORM objects.

    Response models are always explicit field lists. Database models are never
    serialised directly, so adding a column to a table can never silently start
    exposing it through the API.
    """

    model_config = ConfigDict(from_attributes=True)


class ErrorResponse(BaseModel):
    code: str = Field(description="Stable machine-readable error code")
    message: str = Field(description="Message safe to show an end user")
    #: Present only for validation failures; never contains internal detail.
    details: list[dict[str, Any]] | None = None


class HealthComponent(BaseModel):
    name: str
    status: str = Field(description="ok | degraded | down | not_configured")
    detail: str | None = None
    latency_ms: float | None = None


class HealthResponse(BaseModel):
    status: str = Field(description="ok | degraded | down")
    version: str
    environment: str
    components: list[HealthComponent]
