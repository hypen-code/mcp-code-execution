"""Unit tests for the FastMCP server tools (create_server and initialize_server)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

from toon_format import decode as _toon_decode_raw

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
from mce.models import ExecutionResult
from mce.runtime.cache import CacheStore
from mce.runtime.registry import Registry
from mce.server import _BASE_INSTRUCTIONS, _build_instructions, create_server, initialize_server

if TYPE_CHECKING:
    from pathlib import Path


def _toon_decode(s: str) -> dict[str, Any]:
    result = _toon_decode_raw(s)
    assert isinstance(result, dict)
    return result


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
    # Skills disabled by default so existing tests aren't affected by skills paths.
    registry.has_skills.return_value = False
    registry.skills_path.return_value = None

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

    result = _toon_decode(await _call_tool(mcp, "list_servers"))
    assert "servers" in result
    assert len(result["servers"]) == 1
    assert result["servers"][0]["name"] == "weather"


async def test_list_servers_returns_function_list(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    result = _toon_decode(await _call_tool(mcp, "list_servers"))
    functions = result["servers"][0]["functions"]
    assert any(f["name"] == "get_current_weather" for f in functions)


async def test_list_servers_handles_exception(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    registry.list_servers.side_effect = RuntimeError("registry broke")
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    result = _toon_decode(await _call_tool(mcp, "list_servers"))
    assert "error" in result


# ---------------------------------------------------------------------------
# get_functions tool
# ---------------------------------------------------------------------------


async def test_get_functions_returns_function_details(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    result = _toon_decode(
        await _call_tool(
            mcp,
            "get_functions",
            functions=[{"server_name": "weather", "function_name": "get_current_weather"}],
        )
    )
    assert "functions" in result
    fn = result["functions"][0]
    assert fn["function"] == "get_current_weather"
    assert fn["method"] == "GET"
    assert "import_statement" in fn


async def test_get_functions_batch_two_functions(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    result = _toon_decode(
        await _call_tool(
            mcp,
            "get_functions",
            functions=[
                {"server_name": "weather", "function_name": "get_current_weather"},
                {"server_name": "weather", "function_name": "get_current_weather"},
            ],
        )
    )
    assert len(result["functions"]) == 2


async def test_get_functions_rejects_more_than_five(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    result = _toon_decode(
        await _call_tool(
            mcp,
            "get_functions",
            functions=[{"server_name": "weather", "function_name": "f"}] * 6,
        )
    )
    assert result["error_type"] == "validation"


async def test_get_functions_rejects_empty_list(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    result = _toon_decode(await _call_tool(mcp, "get_functions", functions=[]))
    assert result["error_type"] == "validation"


async def test_get_functions_server_not_found(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    registry.get_function.side_effect = ServerNotFoundError("not found")
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    result = _toon_decode(
        await _call_tool(mcp, "get_functions", functions=[{"server_name": "ghost", "function_name": "fn"}])
    )
    assert result["functions"][0]["error_type"] == "server_not_found"


async def test_get_functions_function_not_found(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    registry.get_function.side_effect = FunctionNotFoundError("fn not found")
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    result = _toon_decode(
        await _call_tool(mcp, "get_functions", functions=[{"server_name": "weather", "function_name": "ghost"}])
    )
    assert result["functions"][0]["error_type"] == "function_not_found"


async def test_get_functions_unexpected_error(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    registry.get_function.side_effect = RuntimeError("unexpected")
    cache = _make_mock_cache()
    mcp = create_server(config, registry=registry, cache=cache)

    result = _toon_decode(
        await _call_tool(mcp, "get_functions", functions=[{"server_name": "weather", "function_name": "fn"}])
    )
    assert result["functions"][0]["error_type"] == "internal"


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


# ---------------------------------------------------------------------------
# _build_instructions — skills embedding
# ---------------------------------------------------------------------------


def test_build_instructions_no_skills_returns_base(tmp_path: Path) -> None:
    """No servers with skills → returns _BASE_INSTRUCTIONS unchanged."""
    registry = _make_mock_registry()
    result = _build_instructions(registry, [])
    assert result == _BASE_INSTRUCTIONS


def test_build_instructions_with_skills_embeds_content(tmp_path: Path) -> None:
    """Skills content is injected into the instructions string."""
    skills_file = tmp_path / "skills.md"
    skills_file.write_text("# Weather Skills\nUse param X.", encoding="utf-8")

    registry = _make_mock_registry()
    registry.skills_path.return_value = skills_file

    result = _build_instructions(registry, ["weather"])

    assert "Weather Skills" in result
    assert "Server Skills" in result
    assert "weather" in result


def test_build_instructions_skills_path_none_falls_back_to_base(tmp_path: Path) -> None:
    """If skills_path returns None for all servers, return base instructions."""
    registry = _make_mock_registry()
    registry.skills_path.return_value = None  # file removed after discovery

    result = _build_instructions(registry, ["weather"])
    assert result == _BASE_INSTRUCTIONS


def test_create_server_skills_discovery_failure_does_not_crash(tmp_path: Path) -> None:
    """A broken registry at startup falls back to base instructions gracefully."""
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    registry.list_servers.side_effect = RuntimeError("registry broke")
    cache = _make_mock_cache()

    # Must not raise — the guard in create_server catches the exception.
    mcp = create_server(config, registry=registry, cache=cache)
    assert mcp is not None


async def test_create_server_registers_skills_resource(tmp_path: Path) -> None:
    """When a server has skills, its resource is listed by the MCP server."""
    skills_file = tmp_path / "skills.md"
    skills_file.write_text("# Skills content", encoding="utf-8")

    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    registry.has_skills.return_value = True
    registry.skills_path.return_value = skills_file
    cache = _make_mock_cache()

    mcp = create_server(config, registry=registry, cache=cache)

    resources = await mcp.list_resources()
    assert any("weather" in str(r.uri) for r in resources)


async def test_skills_resource_returns_file_content(tmp_path: Path) -> None:
    """The registered skills resource handler reads and returns the skills file."""
    skills_content = "# Skills\nAlways pass latitude and longitude."
    skills_file = tmp_path / "skills.md"
    skills_file.write_text(skills_content, encoding="utf-8")

    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    registry.has_skills.return_value = True
    registry.skills_path.return_value = skills_file
    cache = _make_mock_cache()

    mcp = create_server(config, registry=registry, cache=cache)

    result = await mcp.read_resource("skills://weather")
    assert skills_content in result.contents[0].content


async def test_skills_resource_returns_message_when_file_missing(tmp_path: Path) -> None:
    """The skills handler returns a descriptive message when skills_path is None."""
    config = _make_config(tmp_path)
    registry = _make_mock_registry()
    registry.has_skills.return_value = True
    registry.skills_path.return_value = None  # File was removed after server started
    cache = _make_mock_cache()

    mcp = create_server(config, registry=registry, cache=cache)

    result = await mcp.read_resource("skills://weather")
    assert "No skills documentation" in result.contents[0].content
