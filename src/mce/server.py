"""MCE FastMCP server — registers the 5 MCP tools and 1 prompt exposed to LLMs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP
from toon_format import encode as _toon_encode

from mce.errors import (
    CacheError,
    ExecutionError,
    ExecutionTimeoutError,
    FunctionNotFoundError,
    LintError,
    SecurityViolationError,
    ServerNotFoundError,
)
from mce.runtime.cache import CacheStore
from mce.runtime.executor import CodeExecutor
from mce.runtime.registry import Registry
from mce.utils.logging import get_logger

if TYPE_CHECKING:
    from mce.config import MCEConfig

logger = get_logger(__name__)


def create_server(
    config: MCEConfig,
    registry: Registry | None = None,
    cache: CacheStore | None = None,
) -> FastMCP:
    """Create and configure the MCE FastMCP server with all 5 tools and 1 prompt.

    Args:
        config: MCE configuration instance.
        registry: Pre-loaded Registry. If None, a new one is created from config.
        cache: Pre-initialized CacheStore. If None, a new one is created from config.

    Returns:
        Configured FastMCP server ready to run.
    """
    mcp: FastMCP = FastMCP(
        name="MCE — MCP Code Execution",
        instructions="""\
# MCE — MCP Code Execution: Usage Guide

## MANDATORY RULE

**`get_functions` BEFORE writing code** — You MUST call `get_functions` before
using any server function. Never write `from <server>.functions import <fn>`
without first calling `get_functions` in the same session.

## Workflow (follow in order)

1. **`list_servers`** — Discover available API servers and their function names.

2. **`get_functions`** — Fetch the signature, parameters, and return schema for
   1–5 functions at once. The response includes a ready-to-use `import_statement`.

3. **`execute_code`** — Run Python code in a sandboxed Docker container.
   - Use the exact `import_statement` from `get_functions`.
   - Every dynamic value (city, ID, date, name…) MUST be a top-level variable.
   - `main()` takes NO arguments — it reads those top-level variables as globals.
   - NEVER hardcode any entity or value inside `main()`.
   - The response includes a `cache_id` — you MUST remember it for reuse.

4. **`run_cached_code`** — Use this whenever the user asks for the same type of
   operation with a different value (different city, different ID, different date…).
   NEVER call `execute_code` again for the same operation type. Pass only the
   changed top-level variable(s) as `params`.

## Rules

- NEVER guess function signatures. Always call `get_functions` first.
- NEVER import a server module without the `import_statement` from `get_functions`.
- NEVER call `execute_code` when a `cache_id` for the same operation is in context.
- Keep `execute_code` payloads minimal — extract only the fields you need.
- If execution fails, re-read the `get_functions` output before retrying.
""",
    )

    if registry is None:
        registry = Registry(config.compiled_output_dir)
        registry.load()
    if cache is None:
        cache = CacheStore(config.cache_db_path, config.cache_ttl_seconds, config.cache_max_entries)
    executor = CodeExecutor(config, cache)

    try:
        _sandbox_libraries = [
            line.strip() for line in Path(config.sandbox_requirements_path).read_text().splitlines() if line.strip()
        ]
    except OSError:
        _sandbox_libraries = []

    @mcp.tool()
    async def list_servers() -> str:
        """List all available API servers and their functions.

        Returns a compact overview of each server with:
        - Server name and description
        - List of available functions with one-line summaries

        Use this to discover what APIs are available before getting function details.
        """
        try:
            servers = registry.list_servers()
            logger.info("tool_list_servers_called", server_count=len(servers))
            return str(
                _toon_encode(
                    {
                        "sandbox_libraries": _sandbox_libraries,
                        "servers": [
                            {
                                "name": s.name,
                                "description": s.description,
                                "functions": [
                                    {"name": fn, "summary": s.function_summaries.get(fn, "")} for fn in s.functions
                                ],
                            }
                            for s in servers
                        ],
                    }
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("list_servers_unexpected_error")
            return str(_toon_encode({"error": "Internal error loading servers", "detail": str(exc)}))

    @mcp.tool()
    async def get_functions(functions: list[dict[str, str]]) -> str:
        """Get detailed function signatures and return schemas for 1–5 functions at once.

        Args:
            functions: List of 1–5 items, each with:
                - server_name: Name of the server (from list_servers).
                - function_name: Name of the function to inspect.

        Returns each function's parameters, types, and response data structure
        so you can write Python code that calls them correctly.
        Requesting more than 5 functions at once returns a validation error.
        """
        if not functions:
            return str(_toon_encode({"error": "Provide at least 1 function.", "error_type": "validation"}))
        if len(functions) > 5:
            return str(
                _toon_encode({"error": "At most 5 functions can be requested at once.", "error_type": "validation"})
            )

        results = []
        for item in functions:
            server_name = item.get("server_name", "")
            function_name = item.get("function_name", "")
            try:
                fn = registry.get_function(server_name, function_name)
                logger.info("tool_get_function_called", server=server_name, function=function_name)
                results.append(
                    {
                        "server": server_name,
                        "function": fn.function_name,
                        "summary": fn.summary,
                        "method": fn.method,
                        "path": fn.path,
                        "parameters": [p.model_dump() for p in fn.parameters],
                        "return_type": fn.return_type,
                        "response_fields": [r.model_dump() for r in fn.response_fields],
                        "usage_example": fn.source_code,
                        "import_statement": f"from {server_name}.functions import {function_name}",
                    }
                )
            except ServerNotFoundError as exc:
                results.append(
                    {
                        "server": server_name,
                        "function": function_name,
                        "error": str(exc),
                        "error_type": "server_not_found",
                    }
                )
            except FunctionNotFoundError as exc:
                results.append(
                    {
                        "server": server_name,
                        "function": function_name,
                        "error": str(exc),
                        "error_type": "function_not_found",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("get_function_unexpected_error")
                results.append(
                    {
                        "server": server_name,
                        "function": function_name,
                        "error": "Internal error",
                        "error_type": "internal",
                        "detail": str(exc),
                    }
                )
        return str(_toon_encode({"functions": results}))

    @mcp.tool()
    async def execute_code(code: str, description: str) -> dict[str, Any]:
        """Execute Python code in a sandboxed environment.

        The code runs in an isolated Docker container with access to API server functions.
        Code MUST define either a `main()` function that returns a result,
        or a `result` variable containing the output.

        Available imports in sandbox:
        - Server functions: `from {server_name}.functions import {function_name}`
        - Standard: httpx, json, datetime, re, math, dataclasses, typing, collections

        Args:
            code: Valid Python code to execute. Must be self-contained.
            description: Brief description of what this code does (used for caching).

        Run multiple functions in single code block and return result.
        Returns execution result with data or error details.
        Keep responses minimal — extract only the fields you need.

        ## Reusable Code Guide

        WRONG — hardcoded value inside main(), not reusable:
            def main():
                return geocoding_search(name="Colombo, Sri Lanka")  # BAD

        CORRECT — top-level variable, reusable via run_cached_code:
            location_name = "Colombo, Sri Lanka"   # top-level param

            def main():
                return geocoding_search(name=location_name)  # reads global

            result = main()

        After execute_code succeeds, the response contains a `cache_id`.
        For the next request of the same type with a different value:
            run_cached_code(cache_id, params={"location_name": "Galle, Sri Lanka"})

        Rules:
        - ALL dynamic values (city, ID, date, name…) → top-level variables
        - main() NEVER takes arguments; it reads globals only
        - description: "action + entity + key param", no specific values or dates
        """
        try:
            result = await executor.execute(code, description)
            logger.info("tool_execute_code_called", success=result.success, description=description[:60])
            return result.model_dump()
        except SecurityViolationError as exc:
            return {"success": False, "error": f"Security violation: {exc}", "error_type": "security"}
        except LintError as exc:
            return {
                "success": False,
                "error": f"Code has issues: {exc}",
                "lint_output": exc.lint_output,
                "error_type": "lint",
            }
        except ExecutionTimeoutError:
            return {
                "success": False,
                "error": f"Execution timed out after {config.execution_timeout_seconds}s",
                "error_type": "timeout",
            }
        except ExecutionError as exc:
            return {"success": False, "error": str(exc), "stderr": exc.stderr, "error_type": "execution"}
        except Exception:  # noqa: BLE001
            logger.exception("execute_code_unexpected_error")
            return {"success": False, "error": "Internal error occurred", "error_type": "internal"}

    @mcp.tool()
    async def run_cached_code(cache_id: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Re-execute a cached code snippet, optionally injecting new parameter values.

        Fetches the original code by cache_id and re-runs it through the full
        execution pipeline (security scan → sandbox → cached on success).

        If params are provided, each key-value pair is injected as a top-level
        variable assignment AND into a `_params` dict BEFORE the cached code runs.
        This means any global variable in the cached code can be overridden:

            # Cached code references global `output_format`:
            result = getpublicip(format=output_format)

            # Call with: params={"output_format": "text"}
            # → injects `output_format = "text"` before the code executes

        Code can also read from `_params` directly for optional values:
            fmt = _params.get("output_format", "json")

        Args:
            cache_id: Cache entry ID from a previous execute_code response.
            params: Optional key→value overrides injected as top-level variables.

        Returns:
            Same structure as execute_code: success, data, error, execution_time_ms, cache_id.
        """
        try:
            entry = await cache.get(cache_id)
        except CacheError as exc:
            return {"success": False, "error": f"Cache unavailable: {exc}", "error_type": "cache"}

        if entry is None:
            return {
                "success": False,
                "error": f"Cache entry '{cache_id[:16]}…' not found or expired",
                "error_type": "cache_miss",
            }

        code = entry.code
        if params:
            # Append AFTER the cached code so param values override any
            # same-named variable the code sets at module level, then re-call
            # main() so it reads the updated globals and produces a fresh result.
            param_lines = "\n".join(f"{k} = {v!r}" for k, v in params.items())
            rerun = "try:\n    result = main()\nexcept NameError:\n    pass"
            code = f"{code}\n\n# --- injected parameter overrides ---\n_params = {params!r}\n{param_lines}\n{rerun}\n"

        logger.info("tool_run_cached_code_called", cache_id=cache_id[:16], has_params=bool(params))

        try:
            result = await executor.execute(code, entry.description)
            return result.model_dump()
        except SecurityViolationError as exc:
            return {"success": False, "error": f"Security violation: {exc}", "error_type": "security"}
        except LintError as exc:
            return {
                "success": False,
                "error": f"Code has issues: {exc}",
                "lint_output": exc.lint_output,
                "error_type": "lint",
            }
        except ExecutionTimeoutError:
            return {
                "success": False,
                "error": f"Execution timed out after {config.execution_timeout_seconds}s",
                "error_type": "timeout",
            }
        except ExecutionError as exc:
            return {"success": False, "error": str(exc), "stderr": exc.stderr, "error_type": "execution"}
        except Exception:  # noqa: BLE001
            logger.exception("run_cached_code_unexpected_error")
            return {"success": False, "error": "Internal error occurred", "error_type": "internal"}

    @mcp.prompt()
    def reusable_code_guide() -> str:
        """Guide for writing reusable, cacheable execute_code payloads."""
        return (
            "WRONG — hardcoded inside main():\n"
            "    def main(): return fn(name='Colombo')  # BAD\n\n"
            "CORRECT — top-level variable:\n"
            "    location_name = 'Colombo'\n"
            "    def main(): return fn(name=location_name)  # reads global\n"
            "    result = main()\n\n"
            "After execute_code, remember the cache_id. Next request of the same type:\n"
            "    run_cached_code(cache_id, params={'location_name': 'Galle'})\n\n"
            "Rules:\n"
            "- ALL dynamic values → top-level variables\n"
            "- main() NEVER takes arguments\n"
            "- description: 'action + entity + key param', no specific values"
        )

    return mcp


async def initialize_server(config: MCEConfig, mcp: FastMCP) -> None:
    """Run startup initialization: load registry and initialize cache.

    Args:
        config: MCE configuration.
        mcp: FastMCP server instance (used to access registry/cache via closure — handled elsewhere).
    """
    # Registry and cache are loaded via the create_server closure
    # This function can be used for pre-flight checks
    registry = Registry(config.compiled_output_dir)
    registry.load()

    cache = CacheStore(config.cache_db_path, config.cache_ttl_seconds, config.cache_max_entries)
    await cache.initialize()
    await cache.cleanup_expired()

    logger.info(
        "mce_server_initialized",
        compiled_dir=config.compiled_output_dir,
        cache_db=config.cache_db_path,
        log_level=config.log_level,
    )
