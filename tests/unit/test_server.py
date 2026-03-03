"""Unit tests for the FastMCP server tools (create_server and initialize_server)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

from mce.config import MCEConfig
from mce.errors import (
    CacheError,
    ExecutionError,
    ExecutionTimeoutError,
    FunctionNotFoundError,
    LintError,
    SecurityViolationError,
    ServerNotFoundError,
)
from mce.models import CacheSummary, ExecutionResult
from mce.runtime.cache import CacheStore
from mce.runtime.registry import Registry
from mce.server import create_server, initialize_server

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path) -> MCEConfig:
    return MCEConfig(
        compiled_output_dir=str(tmp_path / "compiled"),
        cache_db_path=str(tmp_path / "data" / "cache.db"),
        cache_enabled=True,
        cache_ttl_seconds=3600,
        execution_timeout_seconds=30,
        log_level="DEBUG",
        debug=True,
    )


def _make_mock_registry() -> MagicMock:
    registry = MagicMock(spec=Registry)
    server_info = MagicMock()
    server_info.name = "weather"
    server_info.description = "Weather API"
    server_info.functions = ["get_current_weather"]
    server_info.function_summaries = {"get_current_weather": "Get current weather"}
    registry.list_servers.return_value = [server_info]

    fn_info = MagicMock()
    fn_info.function_name = "get_current_weather"
    fn_info.summary = "Get current weather"
    fn_info.method = "GET"
    fn_info.path = "/weather/current"
    fn_info.parameters = []
    fn_info.response_fields = []
    fn_info.source_code = "def get_current_weather(): pass"
    registry.get_function.return_value = fn_info
    return registry


def _make_mock_cache() -> MagicMock:
    cache = MagicMock(spec=CacheStore)
    cache.search = AsyncMock(return_value=[])
    cache.get = AsyncMock(return_value=None)
    cache.store = AsyncMock(return_value="cache-id-123")
    return cache


async def _call_tool(mcp, tool_name: str, **kwargs):  # type: ignore[no-untyped-def]
    """Helper to call a tool registered on the FastMCP app."""
    tool = await mcp.get_tool(tool_name)
    return await tool.fn(**kwargs)


# ---------------------------------------------------------------------------
# create_server — basic
# ---------------------------------------------------------------------------


def test_create_server_returns_fastmcp_instance(tmp_path: Path) -> None:
    from fastmcp import FastMCP  # noqa: PLC0415

    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)
    assert isinstance(mcp, FastMCP)


def test_create_server_auto_creates_registry_when_none(tmp_path: Path) -> None:
    """create_server with registry=None should auto-create a Registry."""
    config = _make_config(tmp_path)
    (tmp_path / "compiled").mkdir(parents=True)
    cache = _make_mock_cache()
    # Should not raise even with empty compiled dir
    mcp = create_server(config, registry=None, cache=cache)
    assert mcp is not None


def test_create_server_auto_creates_cache_when_none(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    mcp = create_server(config, registry=registry, cache=None)
    assert mcp is not None


# ---------------------------------------------------------------------------
# list_servers tool
# ---------------------------------------------------------------------------


async def test_list_servers_returns_server_info(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    result = await _call_tool(mcp, "list_servers")
    assert "servers" in result
    assert len(result["servers"]) == 1
    assert result["servers"][0]["name"] == "weather"


async def test_list_servers_returns_function_list(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    result = await _call_tool(mcp, "list_servers")
    functions = result["servers"][0]["functions"]
    assert any(f["name"] == "get_current_weather" for f in functions)


async def test_list_servers_handles_exception(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    registry.list_servers.side_effect = RuntimeError("registry broke")
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    result = await _call_tool(mcp, "list_servers")
    assert "error" in result


# ---------------------------------------------------------------------------
# get_function tool
# ---------------------------------------------------------------------------


async def test_get_function_returns_function_details(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    result = await _call_tool(mcp, "get_function", server_name="weather", function_name="get_current_weather")
    assert result["function"] == "get_current_weather"
    assert result["method"] == "GET"
    assert "import_statement" in result


async def test_get_function_server_not_found(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    registry.get_function.side_effect = ServerNotFoundError("not found")
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    result = await _call_tool(mcp, "get_function", server_name="ghost", function_name="fn")
    assert result["error_type"] == "server_not_found"


async def test_get_function_function_not_found(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    registry.get_function.side_effect = FunctionNotFoundError("fn not found")
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    result = await _call_tool(mcp, "get_function", server_name="weather", function_name="ghost")
    assert result["error_type"] == "function_not_found"


async def test_get_function_unexpected_error(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    registry.get_function.side_effect = RuntimeError("unexpected")
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    result = await _call_tool(mcp, "get_function", server_name="weather", function_name="fn")
    assert result["error_type"] == "internal"


# ---------------------------------------------------------------------------
# execute_code tool
# ---------------------------------------------------------------------------


def _make_success_execution() -> ExecutionResult:
    return ExecutionResult(success=True, data={"result": 42}, execution_time_ms=100)


async def test_execute_code_success(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    with patch("mce.runtime.executor.CodeExecutor.execute", new=AsyncMock(return_value=_make_success_execution())):
        result = await _call_tool(mcp, "execute_code", code="result = 42", description="compute 42")

    assert result["success"] is True


async def test_execute_code_security_violation(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    with patch(
        "mce.runtime.executor.CodeExecutor.execute",
        new=AsyncMock(side_effect=SecurityViolationError("dangerous import")),
    ):
        result = await _call_tool(mcp, "execute_code", code="import os", description="bad")

    assert result["success"] is False
    assert result["error_type"] == "security"


async def test_execute_code_lint_error(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    with patch(
        "mce.runtime.executor.CodeExecutor.execute",
        new=AsyncMock(side_effect=LintError("lint fail", lint_output="E501")),
    ):
        result = await _call_tool(mcp, "execute_code", code="x=1", description="linted")

    assert result["success"] is False
    assert result["error_type"] == "lint"
    assert "lint_output" in result


async def test_execute_code_timeout_error(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    with patch(
        "mce.runtime.executor.CodeExecutor.execute",
        new=AsyncMock(side_effect=ExecutionTimeoutError("timeout", exit_code=124)),
    ):
        result = await _call_tool(mcp, "execute_code", code="import time; time.sleep(999)", description="slow")

    assert result["success"] is False
    assert result["error_type"] == "timeout"


async def test_execute_code_execution_error(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    with patch(
        "mce.runtime.executor.CodeExecutor.execute",
        new=AsyncMock(side_effect=ExecutionError("docker error", stderr="OOM", exit_code=137)),
    ):
        result = await _call_tool(mcp, "execute_code", code="x = 1/0", description="crash")

    assert result["success"] is False
    assert result["error_type"] == "execution"
    assert "stderr" in result


async def test_execute_code_unexpected_error(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    with patch(
        "mce.runtime.executor.CodeExecutor.execute",
        new=AsyncMock(side_effect=RuntimeError("unexpected")),
    ):
        result = await _call_tool(mcp, "execute_code", code="x = 1", description="oops")

    assert result["success"] is False
    assert result["error_type"] == "internal"


# ---------------------------------------------------------------------------
# get_cached_code tool
# ---------------------------------------------------------------------------


async def test_get_cached_code_returns_entries(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    cache.search = AsyncMock(
        return_value=[
            CacheSummary(
                id="abc123",
                description="weather query",
                servers_used=["weather"],
                use_count=2,
                created_at=1000000.0,
            )
        ]
    )
    mcp = create_server(config, registry=registry, cache=cache)

    result = await _call_tool(mcp, "get_cached_code", search=None)
    assert "cached_entries" in result
    assert len(result["cached_entries"]) == 1
    assert result["cached_entries"][0]["id"] == "abc123"


async def test_get_cached_code_with_search_term(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    cache.search = AsyncMock(return_value=[])
    mcp = create_server(config, registry=registry, cache=cache)

    result = await _call_tool(mcp, "get_cached_code", search="hotel")
    cache.search.assert_awaited_once_with("hotel")
    assert result["cached_entries"] == []


async def test_get_cached_code_cache_error(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    cache.search = AsyncMock(side_effect=CacheError("DB error"))
    mcp = create_server(config, registry=registry, cache=cache)

    result = await _call_tool(mcp, "get_cached_code", search=None)
    assert "error" in result
    assert result["error_type"] == "cache"


async def test_get_cached_code_unexpected_error(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    cache.search = AsyncMock(side_effect=RuntimeError("unexpected"))
    mcp = create_server(config, registry=registry, cache=cache)

    result = await _call_tool(mcp, "get_cached_code", search=None)
    assert "error" in result
    assert result["error_type"] == "internal"


# ---------------------------------------------------------------------------
# run_cached_code tool
# ---------------------------------------------------------------------------


def _make_cache_entry(code: str = "result = 1", description: str = "cached code") -> MagicMock:
    entry = MagicMock()
    entry.code = code
    entry.description = description
    return entry


async def test_run_cached_code_not_found(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    cache.get = AsyncMock(return_value=None)
    mcp = create_server(config, registry=registry, cache=cache)

    result = await _call_tool(mcp, "run_cached_code", cache_id="nonexistent_id_here", params=None)
    assert result["success"] is False
    assert result["error_type"] == "cache_miss"


async def test_run_cached_code_cache_error_on_get(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    cache.get = AsyncMock(side_effect=CacheError("DB unavailable"))
    mcp = create_server(config, registry=registry, cache=cache)

    result = await _call_tool(mcp, "run_cached_code", cache_id="some_id", params=None)
    assert result["success"] is False
    assert result["error_type"] == "cache"


async def test_run_cached_code_success(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    cache.get = AsyncMock(return_value=_make_cache_entry("result = 42", "compute 42"))
    mcp = create_server(config, registry=registry, cache=cache)

    with patch(
        "mce.runtime.executor.CodeExecutor.execute",
        new=AsyncMock(return_value=ExecutionResult(success=True, data={"result": 42}, execution_time_ms=50)),
    ):
        result = await _call_tool(mcp, "run_cached_code", cache_id="abc123", params=None)

    assert result["success"] is True


async def test_run_cached_code_with_params_injects_variables(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    cache.get = AsyncMock(return_value=_make_cache_entry("result = output_format", "format code"))

    captured_code: list[str] = []

    async def fake_execute(code: str, description: str) -> ExecutionResult:
        captured_code.append(code)
        return ExecutionResult(success=True, data="json", execution_time_ms=50)

    mcp = create_server(config, registry=registry, cache=cache)

    with patch("mce.runtime.executor.CodeExecutor.execute", new=AsyncMock(side_effect=fake_execute)):
        await _call_tool(mcp, "run_cached_code", cache_id="abc123", params={"output_format": "json"})

    assert len(captured_code) == 1
    assert "output_format" in captured_code[0]
    assert "json" in captured_code[0]


async def test_run_cached_code_security_violation(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    cache.get = AsyncMock(return_value=_make_cache_entry())
    mcp = create_server(config, registry=registry, cache=cache)

    with patch(
        "mce.runtime.executor.CodeExecutor.execute",
        new=AsyncMock(side_effect=SecurityViolationError("bad")),
    ):
        result = await _call_tool(mcp, "run_cached_code", cache_id="abc123", params=None)

    assert result["error_type"] == "security"


async def test_run_cached_code_lint_error(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    cache.get = AsyncMock(return_value=_make_cache_entry())
    mcp = create_server(config, registry=registry, cache=cache)

    with patch(
        "mce.runtime.executor.CodeExecutor.execute",
        new=AsyncMock(side_effect=LintError("lint fail", lint_output="E501")),
    ):
        result = await _call_tool(mcp, "run_cached_code", cache_id="abc123", params=None)

    assert result["error_type"] == "lint"


async def test_run_cached_code_timeout(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    cache.get = AsyncMock(return_value=_make_cache_entry())
    mcp = create_server(config, registry=registry, cache=cache)

    with patch(
        "mce.runtime.executor.CodeExecutor.execute",
        new=AsyncMock(side_effect=ExecutionTimeoutError("timeout", exit_code=124)),
    ):
        result = await _call_tool(mcp, "run_cached_code", cache_id="abc123", params=None)

    assert result["error_type"] == "timeout"


async def test_run_cached_code_execution_error(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    cache.get = AsyncMock(return_value=_make_cache_entry())
    mcp = create_server(config, registry=registry, cache=cache)

    with patch(
        "mce.runtime.executor.CodeExecutor.execute",
        new=AsyncMock(side_effect=ExecutionError("crash", stderr="oom", exit_code=137)),
    ):
        result = await _call_tool(mcp, "run_cached_code", cache_id="abc123", params=None)

    assert result["error_type"] == "execution"


async def test_run_cached_code_unexpected_error(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    cache.get = AsyncMock(return_value=_make_cache_entry())
    mcp = create_server(config, registry=registry, cache=cache)

    with patch(
        "mce.runtime.executor.CodeExecutor.execute",
        new=AsyncMock(side_effect=RuntimeError("unexpected")),
    ):
        result = await _call_tool(mcp, "run_cached_code", cache_id="abc123", params=None)

    assert result["error_type"] == "internal"


# ---------------------------------------------------------------------------
# initialize_server
# ---------------------------------------------------------------------------


async def test_initialize_server_logs_and_returns(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    (tmp_path / "compiled").mkdir(parents=True)
    from fastmcp import FastMCP  # noqa: PLC0415

    mcp = FastMCP(name="test")

    with (
        patch("mce.runtime.cache.CacheStore.initialize", new=AsyncMock()),
        patch("mce.runtime.cache.CacheStore.cleanup_expired", new=AsyncMock(return_value=0)),
    ):
        await initialize_server(config, mcp)  # must not raise
