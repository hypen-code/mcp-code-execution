"""MCE FastMCP3 server — registers the 4 MCP tools exposed to LLMs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

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
    """Create and configure the MCE FastMCP server with all 4 tools.

    Args:
        config: MCE configuration instance.
        registry: Pre-loaded Registry. If None, a new one is created from config.
        cache: Pre-initialized CacheStore. If None, a new one is created from config.

    Returns:
        Configured FastMCP server ready to run.
    """
    mcp: FastMCP = FastMCP(
        name="MCE — MCP Code Execution",
        instructions=(
            "MCE allows you to discover, inspect, and execute API server functions "
            "through 4 meta-tools. Workflow: 1) list_servers to see what's available, "
            "2) get_function to get function signature and examples, "
            "3) execute_code to run Python code using those functions, "
            "4) get_cached_code to find and reuse previously successful code."
        ),
    )

    if registry is None:
        registry = Registry(config.compiled_output_dir)
        registry.load()
    if cache is None:
        cache = CacheStore(config.cache_db_path, config.cache_ttl_seconds, config.cache_max_entries)
    executor = CodeExecutor(config, cache)

    @mcp.resource(
        "mce://usage-guide",
        name="MCE Usage Guide",
        description="Mandatory rules and workflow for using MCE tools correctly.",
        mime_type="text/plain",
    )
    def usage_guide() -> str:
        """Return the MCE usage guide with mandatory rules for the LLM."""
        return """\
# MCE — MCP Code Execution: Usage Guide

## MANDATORY RULE
**You MUST call `get_function` before using any server function in code.**
Never write `from <server>.functions import <fn>` without first calling
`get_function` for that function in the same session. Skipping this step
will produce incorrect or broken code because you will not know the exact
parameter names, types, or return structure.

## Workflow (follow in order)

1. **`list_servers`** — Discover available API servers and their function names.
   Call this once at the start to see what is available.

2. **`get_function`** — Fetch the signature, parameters, and return schema for
   1–5 functions at once. You MUST do this before writing any code that calls
   those functions. The response includes a ready-to-use `import_statement`.

3. **`execute_code`** — Run Python code in a sandboxed Docker container.
   - Use the exact `import_statement` from `get_function`.
   - Code must define a `main()` function OR set a `result` variable.
   - Only use parameters and fields you confirmed via `get_function`.

4. **`get_cached_code`** / **`run_cached_code`** — Find and re-run previously
   successful executions. Use this to avoid repeating work.

## Rules

- NEVER guess function signatures. Always call `get_function` first.
- NEVER import a server module without the `import_statement` from `get_function`.
- Keep `execute_code` payloads minimal — extract only the fields you need.
- If execution fails, re-read the `get_function` output before retrying.
"""

    @mcp.tool()
    async def list_servers() -> dict[str, Any]:
        """List all available API servers and their functions.

        Returns a compact overview of each server with:
        - Server name and description
        - List of available functions with one-line summaries

        Use this to discover what APIs are available before getting function details.
        """
        try:
            servers = registry.list_servers()
            logger.info("tool_list_servers_called", server_count=len(servers))
            return {
                "servers": [
                    {
                        "name": s.name,
                        "description": s.description,
                        "functions": [{"name": fn, "summary": s.function_summaries.get(fn, "")} for fn in s.functions],
                    }
                    for s in servers
                ]
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("list_servers_unexpected_error")
            return {"error": "Internal error loading servers", "detail": str(exc)}

    @mcp.tool()
    async def get_function(functions: list[dict[str, str]]) -> dict[str, Any]:
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
            return {"error": "Provide at least 1 function.", "error_type": "validation"}
        if len(functions) > 5:
            return {"error": "At most 5 functions can be requested at once.", "error_type": "validation"}

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
        return {"functions": results}

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

        Returns execution result with data or error details.
        Keep responses minimal — extract only the fields you need.
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
    async def get_cached_code(search: str | None = None) -> dict[str, Any]:
        """List previously executed code that succeeded and is cached for reuse.

        Args:
            search: Optional search term to filter by description.

        Returns list of cached code entries with ID, description, and usage count.
        You can re-execute cached code by passing its ID to execute_code.
        """
        try:
            entries = await cache.search(search)
            logger.info("tool_get_cached_code_called", search=search, results=len(entries))
            return {
                "cached_entries": [
                    {
                        "id": e.id,
                        "description": e.description,
                        "servers_used": e.servers_used,
                        "use_count": e.use_count,
                        "created_at": e.created_at,
                    }
                    for e in entries
                ]
            }
        except CacheError as exc:
            return {"error": f"Cache unavailable: {exc}", "error_type": "cache"}
        except Exception:  # noqa: BLE001
            logger.exception("get_cached_code_unexpected_error")
            return {"error": "Internal error", "error_type": "internal"}

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
            cache_id: Cache entry ID from get_cached_code.
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
            # same-named variable the code sets at module level. Functions
            # defined in the code read globals, so they pick up these values.
            param_lines = "\n".join(f"{k} = {v!r}" for k, v in params.items())
            code = f"{code}\n\n# --- injected parameter overrides ---\n_params = {params!r}\n{param_lines}\n"

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
