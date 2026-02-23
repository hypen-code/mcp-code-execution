"""Pydantic models for MFP — swagger/OpenAPI, functions, execution, and cache."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Swagger / OpenAPI models (swagger.py namespace)
# ---------------------------------------------------------------------------


class ParamSchema(BaseModel):
    """Represents a single parameter to an API endpoint."""

    name: str
    location: str  # "query" | "path" | "header" | "body"
    param_type: str  # "string" | "integer" | "number" | "boolean" | "object" | "array"
    required: bool = False
    description: str = ""
    default: str | None = None
    enum: list[str] | None = None


class ResponseField(BaseModel):
    """Represents a field in an API response schema."""

    name: str
    field_type: str
    description: str = ""
    nested: list[ResponseField] | None = None  # 1 level only


class EndpointSpec(BaseModel):
    """Normalized representation of a single API endpoint."""

    path: str
    method: str
    operation_id: str
    summary: str
    description: str = ""
    parameters: list[ParamSchema] = Field(default_factory=list)
    request_body_schema: dict[str, Any] | None = None
    response_schema: list[ResponseField] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class ServerSpec(BaseModel):
    """Normalized representation of a complete API server from a swagger doc."""

    name: str
    description: str
    base_url: str
    is_read_only: bool
    endpoints: list[EndpointSpec] = Field(default_factory=list)
    swagger_hash: str


# ---------------------------------------------------------------------------
# Swagger source config model
# ---------------------------------------------------------------------------


class SwaggerSource(BaseModel):
    """Configuration for a single swagger/OpenAPI source."""

    name: str
    swagger_url: str
    base_url: str
    auth_header: str = ""
    is_read_only: bool = False
    extra_headers: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Function info models (function.py namespace)
# ---------------------------------------------------------------------------


class FunctionInfo(BaseModel):
    """Complete metadata for a compiled API function."""

    server_name: str
    function_name: str
    summary: str
    description: str = ""
    parameters: list[ParamSchema] = Field(default_factory=list)
    response_fields: list[ResponseField] = Field(default_factory=list)
    source_code: str
    method: str = ""
    path: str = ""


class ServerInfo(BaseModel):
    """Summary metadata for a compiled server (used in list_servers response)."""

    name: str
    description: str
    functions: list[str] = Field(default_factory=list)
    function_summaries: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Execution models (execution.py namespace)
# ---------------------------------------------------------------------------


class ExecutionResult(BaseModel):
    """Result from sandboxed code execution."""

    success: bool
    data: dict[str, Any] | list[Any] | str | None = None
    error: str | None = None
    traceback: str | None = None  # Only populated in debug mode
    execution_time_ms: int = 0
    cache_id: str | None = None


# ---------------------------------------------------------------------------
# Cache models (cache.py namespace)
# ---------------------------------------------------------------------------


class CacheEntry(BaseModel):
    """A single entry in the code cache."""

    id: str
    description: str
    code: str
    servers_used: list[str] = Field(default_factory=list)
    swagger_hash: str
    created_at: float
    last_used_at: float
    use_count: int = 1
    ttl_seconds: int = 3600


class CacheSummary(BaseModel):
    """Compact cache entry for listing (without full code)."""

    id: str
    description: str
    servers_used: list[str]
    use_count: int
    created_at: float


# ---------------------------------------------------------------------------
# Manifest model
# ---------------------------------------------------------------------------


class EndpointManifest(BaseModel):
    """Manifest entry for a single compiled endpoint."""

    function_name: str
    summary: str
    method: str
    path: str
    parameters_summary: str
    response_summary: str


class ServerManifest(BaseModel):
    """Compiled server manifest written to disk."""

    server_name: str
    description: str
    swagger_hash: str
    compiled_at: str
    base_url: str
    is_read_only: bool
    endpoints: list[EndpointManifest] = Field(default_factory=list)
