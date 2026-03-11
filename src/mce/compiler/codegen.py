"""Jinja2-based code generator — transforms ServerSpec into Python function modules."""

from __future__ import annotations

import keyword
import re
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from mce.errors import CompileError
from mce.utils.logging import get_logger

if TYPE_CHECKING:
    from mce.models import EndpointSpec, ParamSchema, ServerSpec

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


def _wrap_text(text: str, width: int, subsequent_indent: str = "") -> str:
    """Wrap each paragraph of text to fit within width, preserving blank lines."""
    if not text:
        return text
    paragraphs = text.split("\n\n")
    wrapped = []
    for para in paragraphs:
        para = para.replace("\n", " ").strip()
        wrapped.append(
            textwrap.fill(
                para,
                width=width,
                subsequent_indent=subsequent_indent,
                break_long_words=True,
                break_on_hyphens=True,
            )
        )
    return "\n\n".join(wrapped)


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
            if p.param_type == "array":
                default = "None"
            elif p.default and p.param_type == "string":
                default = f'"{p.default}"'
            else:
                default = p.default or "None"
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
    return f'f"{path}"' if path_params else f'"{path}"'


def _safe_name(name: str) -> str:
    """Sanitize a parameter name to a valid Python snake_case identifier.

    Handles camelCase names (e.g. ``dashboardId`` → ``dashboard_id``) so that
    generated parameter names are readable and follow Python conventions.

    Args:
        name: Raw parameter name (may be camelCase or already snake_case).

    Returns:
        Safe snake_case Python identifier.
    """
    # Split camelCase/PascalCase boundaries before lowercasing
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    name = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", name)
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_").lower()
    if sanitized and sanitized[0].isdigit():
        sanitized = f"p_{sanitized}"
    if keyword.iskeyword(sanitized):
        sanitized = f"{sanitized}_"
    return sanitized or "param"


def _safe_field_name(name: str) -> str:
    """Make a field name safe for use as a Python identifier in a TypedDict.

    Only escapes Python keywords and names starting with digits; preserves
    camelCase so it matches the actual JSON response keys.

    Args:
        name: Raw field name from swagger schema.

    Returns:
        Safe Python identifier (appends underscore for keywords).
    """
    name = name.replace("@", "_")
    if not name or name[0].isdigit():
        return f"f_{name}"
    if keyword.iskeyword(name):
        return f"{name}_"
    return name


def _to_pascal_case(snake: str) -> str:
    """Convert snake_case identifier to PascalCase.

    Args:
        snake: Snake-case string (e.g. get_user_by_id).

    Returns:
        PascalCase string (e.g. GetUserById).
    """
    return "".join(word.capitalize() for word in snake.split("_"))


def _build_typeddict_classes(endpoint: EndpointSpec) -> list[str]:
    """Build TypedDict class definition strings for an endpoint's response schema.

    Nested object fields produce separate TypedDict classes emitted first.

    Args:
        endpoint: Endpoint specification with response_schema.

    Returns:
        List of class definition strings, ready to inject into generated source.
    """
    schema = endpoint.response_schema
    if not schema:
        return []

    pascal = _to_pascal_case(endpoint.operation_id)
    classes: list[str] = []

    # Detect array-of-items: single "items" field with nested structure
    is_array = len(schema) == 1 and schema[0].name == "items" and schema[0].nested
    fields = schema[0].nested if is_array else schema
    base_name = f"{pascal}ResponseItem" if is_array else f"{pascal}Response"

    if not fields:
        return []

    # Emit nested TypedDicts first (dependencies before parent)
    for field in fields:
        if field.field_type == "object" and field.nested:
            nested_name = f"{base_name}{_to_pascal_case(field.name)}"
            lines = [f"class {nested_name}(TypedDict, total=False):"]
            for nf in field.nested:
                py_type = _swagger_type_to_python(nf.field_type)
                lines.append(f"    {_safe_field_name(nf.name)}: {py_type}")
            classes.append("\n".join(lines))

    # Emit the main TypedDict
    lines = [f"class {base_name}(TypedDict, total=False):"]
    for field in fields:
        if field.field_type == "object" and field.nested:
            nested_name = f"{base_name}{_to_pascal_case(field.name)}"
            lines.append(f"    {_safe_field_name(field.name)}: {nested_name}")
        else:
            py_type = _swagger_type_to_python(field.field_type)
            lines.append(f"    {_safe_field_name(field.name)}: {py_type}")
    classes.append("\n".join(lines))

    return classes


def _build_return_type(endpoint: EndpointSpec) -> str:
    """Determine the Python return type annotation for an endpoint.

    Args:
        endpoint: Endpoint specification with response_schema.

    Returns:
        Python type annotation string (e.g. 'GetUserByIdResponse', 'list[Any]', 'Any').
    """
    schema = endpoint.response_schema
    if not schema:
        return "Any"

    pascal = _to_pascal_case(endpoint.operation_id)

    # Array response
    if len(schema) == 1 and schema[0].name == "items":
        if schema[0].nested:
            return f"list[{pascal}ResponseItem]"
        return "list[Any]"

    # Object response with fields
    if schema:
        return f"{pascal}Response"

    return "dict[str, Any]"


def _build_docstring_args(endpoint: EndpointSpec) -> list[dict[str, Any]]:
    """Build argument list for docstring template rendering.

    Args:
        endpoint: Endpoint specification.

    Returns:
        List of arg dicts with name, type, required, description keys.
    """
    args = []
    for p in endpoint.parameters:
        safe = _safe_name(p.name)
        # 120 - 8 (indent) - len(name) - 2 (": ") - 11 (" (required)")
        desc_width = max(40, 99 - len(safe))
        args.append(
            {
                "name": safe,
                "type": _swagger_type_to_python(p.param_type),
                "required": p.required,
                "description": _wrap_text(p.description or p.name, width=desc_width, subsequent_indent="            "),
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

        header_desc_width = max(40, 120 - len(f"# Server: {spec.name} \u2014 "))
        try:
            code: str = template.render(
                server_name=spec.name,
                description=spec.description,
                base_url=spec.base_url,
                is_read_only=spec.is_read_only,
                functions=functions_data,
                header_desc_width=header_desc_width,
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
            "summary": _wrap_text(endpoint.summary, width=113, subsequent_indent="    "),
            "description": _wrap_text(endpoint.description or "", width=116, subsequent_indent="    "),
            "signature": (_sig := _build_function_signature(endpoint)),
            "params": _sig.split(", ") if _sig else [],
            "params_dict": _build_params_dict(endpoint),
            "has_body": bool(endpoint.request_body_schema) and endpoint.method in ("POST", "PUT", "PATCH"),
            "docstring_args": _build_docstring_args(endpoint),
            "response_fields": [r.name for r in endpoint.response_schema],
            "has_query_params": any(p.location == "query" for p in endpoint.parameters),
            "base_url": endpoint.base_url,
            "return_type": _build_return_type(endpoint),
            "typeddict_classes": _build_typeddict_classes(endpoint),
        }
