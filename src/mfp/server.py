"""MFP FastMCP3 server — registers the 4 MCP tools exposed to LLMs."""

from __future__ import annotations

from fastmcp import FastMCP

from mfp.config import MFPConfig
from mfp.errors import (
    CacheError,
    ExecutionError,
    ExecutionTimeoutError,
    FunctionNotFoundError,
    LintError,
    SecurityViolationError,
    ServerNotFoundError,
)
from mfp.runtime.cache import CacheStore
from mfp.runtime.executor import CodeExecutor
from mfp.runtime.registry import Registry
from mfp.utils.logging import get_logger

logger = get_logger(__name__)


def create_server(config: MFPConfig) -> FastMCP:
    """Create and configure the MFP FastMCP server with all 4 tools.

    Args:
        config: MFP configuration instance.

    Returns:
        Configured FastMCP server ready to run.
    """
    mcp: FastMCP = FastMCP(
        name="MFP — ModelFunctionProtocol",
        instructions=(
            "MFP allows you to discover, inspect, and execute API server functions "
            "through 4 meta-tools. Workflow: 1) list_servers to see what's available, "
            "2) get_function to get function signature and examples, "
            "3) execute_code to run Python code using those functions, "
            "4) get_cached_code to find and reuse previously successful code."
        ),
    )

    registry = Registry(config.compiled_output_dir)
    cache = CacheStore(config.cache_db_path, config.cache_ttl_seconds, config.cache_max_entries)
    executor = CodeExecutor(config, cache)

    @mcp.tool()
    async def list_servers() -> dict:  # type: ignore[return]
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
                        "functions": [
                            {"name": fn, "summary": s.function_summaries.get(fn, "")}
                            for fn in s.functions
                        ],
                    }
                    for s in servers
                ]
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("list_servers_unexpected_error")
            return {"error": "Internal error loading servers", "detail": str(exc)}

    @mcp.tool()
    async def get_function(server_name: str, function_name: str) -> dict:  # type: ignore[return]
        """Get detailed function signature and return schema.

        Args:
            server_name: Name of the server (from list_servers).
            function_name: Name of the function to inspect.

        Returns the function's parameters, types, and response data structure
        so you can write Python code that calls it correctly.
        """
        try:
            fn = registry.get_function(server_name, function_name)
            logger.info("tool_get_function_called", server=server_name, function=function_name)
            return {
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
        except ServerNotFoundError as exc:
            return {"error": str(exc), "error_type": "server_not_found"}
        except FunctionNotFoundError as exc:
            return {"error": str(exc), "error_type": "function_not_found"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("get_function_unexpected_error")
            return {"error": "Internal error", "error_type": "internal", "detail": str(exc)}

    @mcp.tool()
    async def execute_code(code: str, description: str) -> dict:  # type: ignore[return]
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
            return {"success": False, "error": f"Code has issues: {exc}", "lint_output": exc.lint_output, "error_type": "lint"}
        except ExecutionTimeoutError:
            return {
                "success": False,
                "error": f"Execution timed out after {config.execution_timeout_seconds}s",
                "error_type": "timeout",
            }
        except ExecutionError as exc:
            return {"success": False, "error": str(exc), "stderr": exc.stderr, "error_type": "execution"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("execute_code_unexpected_error")
            return {"success": False, "error": "Internal error occurred", "error_type": "internal"}

    @mcp.tool()
    async def get_cached_code(search: str | None = None) -> dict:  # type: ignore[return]
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
        except Exception as exc:  # noqa: BLE001
            logger.exception("get_cached_code_unexpected_error")
            return {"error": "Internal error", "error_type": "internal"}

    return mcp


async def initialize_server(config: MFPConfig, mcp: FastMCP) -> None:
    """Run startup initialization: load registry and initialize cache.

    Args:
        config: MFP configuration.
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
        "mfp_server_initialized",
        compiled_dir=config.compiled_output_dir,
        cache_db=config.cache_db_path,
        log_level=config.log_level,
    )
