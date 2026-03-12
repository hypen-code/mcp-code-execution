"""Top-level MCP tool generator — creates direct FastMCP tool wrappers for selected functions.

During compilation, if a SwaggerSource declares ``top_level_functions``, this module
generates ``compiled/<server>/top_level_functions.py``.  That file contains async
wrapper functions (one per listed endpoint) and a ``_TOP_LEVEL_TOOLS`` registry that
``server.py`` reads at startup to register the tools with FastMCP directly — no
``list_servers`` / ``get_functions`` / ``execute_code`` round-trip required.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from mce.compiler.codegen import (
    _build_docstring_args,
    _build_function_signature,
    _build_return_type,
    _safe_name,
)
from mce.errors import CompileError
from mce.utils.logging import get_logger

if TYPE_CHECKING:
    from mce.models import EndpointSpec, ServerSpec

logger = get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _normalize_function_name(raw: str) -> str:
    """Normalise a user-supplied function name to the snake_case identifier used
    by the compiler.

    Handles camelCase / PascalCase entries in ``top_level_functions`` so that
    e.g. ``getForecast`` maps to the compiled ``get_forecast``.

    Args:
        raw: Function name as written in swaggers.yaml (may be camelCase).

    Returns:
        snake_case Python identifier matching the compiled operation_id.
    """
    # Mirror the same camelCase→snake_case logic used in swagger_parser._sanitize_identifier
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw)
    name = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", name)
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name.lower() or raw.lower()


class TopLevelFunctionGenerator:
    """Generates ``top_level_functions.py`` for a compiled server.

    The generated file contains:
    - Async wrapper functions that delegate to the sync API functions via
      ``asyncio.to_thread``, making them safe to await inside the FastMCP event loop.
    - A ``_TOP_LEVEL_TOOLS`` list that ``server.py`` consumes to register each
      function as a FastMCP ``@mcp.tool()`` at startup.
    """

    def __init__(self) -> None:
        """Initialise the Jinja2 environment."""
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )

    def generate(
        self,
        spec: ServerSpec,
        module_name: str,
        top_level_names: list[str],
    ) -> str | None:
        """Generate ``top_level_functions.py`` source code for the requested functions.

        Args:
            spec: Compiled server specification (provides endpoint details).
            module_name: Python module directory name (e.g. ``open_meteo_weather_api``).
            top_level_names: Function names from swaggers.yaml (camelCase or snake_case).

        Returns:
            Generated Python source code string, or ``None`` when none of the requested
            names could be resolved to a compiled endpoint.

        Raises:
            CompileError: If the Jinja2 template rendering fails.
        """
        if not top_level_names:
            return None

        # Normalise requested names to the snake_case form the compiler uses
        requested_normalized: set[str] = {_normalize_function_name(n) for n in top_level_names}

        # Match against compiled endpoints (operation_id is already snake_case)
        matched: list[EndpointSpec] = [ep for ep in spec.endpoints if ep.operation_id in requested_normalized]

        # Report any unresolved names so the user can fix the YAML config
        resolved: set[str] = {ep.operation_id for ep in matched}
        unresolved = requested_normalized - resolved
        if unresolved:
            available = [ep.operation_id for ep in spec.endpoints]
            logger.warning(
                "top_level_functions_not_found",
                server=spec.name,
                unresolved=sorted(unresolved),
                available=available,
            )

        if not matched:
            return None

        template = self._env.get_template("top_level_functions.py.j2")
        functions_data = [self._prepare_function_data(ep) for ep in matched]

        try:
            code: str = template.render(
                server_name=spec.name,
                module_name=module_name,
                functions=functions_data,
            )
        except Exception as exc:
            raise CompileError(f"top_level_functions template rendering failed for {spec.name}: {exc}") from exc

        logger.debug(
            "top_level_functions_generated",
            server=spec.name,
            module=module_name,
            count=len(matched),
            functions=[ep.operation_id for ep in matched],
        )
        return code

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _prepare_function_data(self, endpoint: EndpointSpec) -> dict[str, Any]:
        """Build the Jinja2 template context dict for one endpoint.

        Args:
            endpoint: Endpoint specification from the compiled ServerSpec.

        Returns:
            Dict with all template variables for this function.
        """
        sig = _build_function_signature(endpoint)

        # Build "kwarg=kwarg" strings so asyncio.to_thread gets the right values
        call_kwargs: list[str] = []
        for p in endpoint.parameters:
            safe = _safe_name(p.name)
            call_kwargs.append(f"{safe}={safe}")
        if endpoint.request_body_schema:
            call_kwargs.append("json_body=json_body")

        return {
            "name": endpoint.operation_id,
            "summary": endpoint.summary,
            "signature": sig,
            "params": sig.split(", ") if sig else [],
            "call_kwargs": call_kwargs,
            "docstring_args": _build_docstring_args(endpoint),
            "response_fields": [r.name for r in endpoint.response_schema],
            "return_type": _build_return_type(endpoint),
        }
