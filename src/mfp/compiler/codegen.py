"""Jinja2-based code generator — transforms ServerSpec into Python function modules."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from mfp.errors import CompileError
from mfp.models import EndpointSpec, ParamSchema, ServerSpec
from mfp.utils.logging import get_logger

logger = get_logger(__name__)

# Directory containing Jinja2 templates
_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Type mapping from swagger/JSON schema types to Python type annotations
_TYPE_MAP: dict[str, str] = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "object": "dict[str, Any]",
    "array": "list[Any]",
}


def _swagger_type_to_python(swagger_type: str) -> str:
    """Convert swagger type string to Python type annotation.

    Args:
        swagger_type: Swagger/JSON schema type.

    Returns:
        Python type annotation string.
    """
    return _TYPE_MAP.get(swagger_type, "Any")


def _build_param_annotation(param: ParamSchema) -> str:
    """Build a type annotation string for a parameter.

    Args:
        param: Parameter schema descriptor.

    Returns:
        Python type annotation (e.g. 'str | None').
    """
    base = _swagger_type_to_python(param.param_type)
    if not param.required:
        return f"{base} | None"
    return base


def _build_function_signature(endpoint: EndpointSpec) -> str:
    """Build function signature string (parameters only) for a function def.

    Args:
        endpoint: Endpoint specification.

    Returns:
        Comma-separated parameter list string.
    """
    parts: list[str] = []

    # Required params first
    for p in endpoint.parameters:
        if p.required:
            annotation = _swagger_type_to_python(p.param_type)
            parts.append(f"{_safe_name(p.name)}: {annotation}")

    # Optional params with defaults
    for p in endpoint.parameters:
        if not p.required:
            annotation = _swagger_type_to_python(p.param_type)
            default = f'"{p.default}"' if p.default and p.param_type == "string" else p.default or "None"
            parts.append(f"{_safe_name(p.name)}: {annotation} | None = {default}")

    # Request body as json_body for mutating methods
    if endpoint.request_body_schema:
        parts.append("json_body: dict[str, Any] | None = None")

    return ", ".join(parts)


def _build_params_dict(endpoint: EndpointSpec) -> str:
    """Build the query params dict construction code.

    Args:
        endpoint: Endpoint specification.

    Returns:
        Python code string for building the params dict.
    """
    query_params = [p for p in endpoint.parameters if p.location == "query"]
    if not query_params:
        return "None"

    lines = ["{"]
    for p in query_params:
        safe = _safe_name(p.name)
        lines.append(f'        "{p.name}": {safe},')
    lines.append("    }")
    raw = "\n".join(lines)
    return raw


def _build_path_formatted(endpoint: EndpointSpec) -> str:
    """Build python format expression for URL path substitution.

    Args:
        endpoint: Endpoint specification.

    Returns:
        Python f-string body for path formatting.
    """
    path = endpoint.path
    path_params = [p for p in endpoint.parameters if p.location == "path"]
    for p in path_params:
        path = path.replace(f"{{{p.name}}}", f"{{{_safe_name(p.name)}}}")
    return f'f"{path}"'


def _safe_name(name: str) -> str:
    """Sanitize a parameter name to a valid Python identifier.

    Args:
        name: Raw parameter name.

    Returns:
        Safe Python identifier.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    if sanitized and sanitized[0].isdigit():
        sanitized = f"p_{sanitized}"
    return sanitized or "param"


def _build_docstring_args(endpoint: EndpointSpec) -> list[dict[str, Any]]:
    """Build argument list for docstring template rendering.

    Args:
        endpoint: Endpoint specification.

    Returns:
        List of arg dicts with name, type, required, description keys.
    """
    args = []
    for p in endpoint.parameters:
        args.append(
            {
                "name": _safe_name(p.name),
                "type": _swagger_type_to_python(p.param_type),
                "required": p.required,
                "description": p.description or p.name,
            }
        )
    if endpoint.request_body_schema:
        args.append(
            {
                "name": "json_body",
                "type": "dict[str, Any] | None",
                "required": False,
                "description": "Request body as JSON object",
            }
        )
    return args


class CodeGenerator:
    """Generate Python function modules from compiled ServerSpec."""

    def __init__(self) -> None:
        """Initialize the Jinja2 environment with the templates directory."""
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._env.globals["swagger_type_to_python"] = _swagger_type_to_python
        self._env.globals["safe_name"] = _safe_name

    def generate(self, spec: ServerSpec) -> str:
        """Generate Python source code for the given ServerSpec.

        Args:
            spec: Compiled server specification.

        Returns:
            Generated Python source code as a string.

        Raises:
            CompileError: If template rendering fails.
        """
        template = self._env.get_template("function.py.j2")

        functions_data = [self._prepare_function_data(ep) for ep in spec.endpoints]

        try:
            code = template.render(
                server_name=spec.name,
                description=spec.description,
                base_url=spec.base_url,
                is_read_only=spec.is_read_only,
                functions=functions_data,
            )
        except Exception as exc:
            raise CompileError(f"Template rendering failed for {spec.name}: {exc}") from exc

        logger.debug(
            "code_generated",
            server=spec.name,
            functions=len(functions_data),
            code_size=len(code),
        )
        return code

    def _prepare_function_data(self, endpoint: EndpointSpec) -> dict[str, Any]:
        """Prepare template context data for a single endpoint.

        Args:
            endpoint: Endpoint specification.

        Returns:
            Dict with all template variables for the function.
        """
        return {
            "name": endpoint.operation_id,
            "method": endpoint.method,
            "path": endpoint.path,
            "path_expr": _build_path_formatted(endpoint),
            "summary": endpoint.summary,
            "description": endpoint.description,
            "signature": _build_function_signature(endpoint),
            "params_dict": _build_params_dict(endpoint),
            "has_body": bool(endpoint.request_body_schema) and endpoint.method in ("POST", "PUT", "PATCH"),
            "docstring_args": _build_docstring_args(endpoint),
            "response_fields": [r.name for r in endpoint.response_schema],
            "has_query_params": any(p.location == "query" for p in endpoint.parameters),
        }
