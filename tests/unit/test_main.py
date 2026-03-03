"""Unit tests for the CLI entry point (__main__.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from mce.__main__ import _build_parser, _cmd_compile, _cmd_run, _cmd_serve, main

# ---------------------------------------------------------------------------
# _build_parser
# ---------------------------------------------------------------------------


def test_build_parser_returns_parser() -> None:
    import argparse  # noqa: PLC0415

    parser = _build_parser()
    assert isinstance(parser, argparse.ArgumentParser)


def test_build_parser_prog_name() -> None:
    parser = _build_parser()
    assert parser.prog == "mce"


def test_build_parser_no_subcommand_gives_none() -> None:
    parser = _build_parser()
    args = parser.parse_args([])
    assert args.command is None


def test_build_parser_compile_subcommand() -> None:
    parser = _build_parser()
    args = parser.parse_args(["compile"])
    assert args.command == "compile"
    assert args.llm_enhance is False
    assert args.dry_run is False


def test_build_parser_compile_with_flags() -> None:
    parser = _build_parser()
    args = parser.parse_args(["compile", "--llm-enhance", "--dry-run"])
    assert args.llm_enhance is True
    assert args.dry_run is True


def test_build_parser_serve_subcommand_defaults() -> None:
    parser = _build_parser()
    args = parser.parse_args(["serve"])
    assert args.command == "serve"
    assert args.transport == "stdio"
    assert args.host is None
    assert args.port is None


def test_build_parser_serve_http_transport() -> None:
    parser = _build_parser()
    args = parser.parse_args(["serve", "--transport", "http", "--host", "0.0.0.0", "--port", "9000"])
    assert args.transport == "http"
    assert args.host == "0.0.0.0"
    assert args.port == 9000


def test_build_parser_run_subcommand() -> None:
    parser = _build_parser()
    args = parser.parse_args(["run"])
    assert args.command == "run"
    assert args.transport == "stdio"


def test_build_parser_run_http_transport() -> None:
    parser = _build_parser()
    args = parser.parse_args(["run", "--transport", "http"])
    assert args.transport == "http"


# ---------------------------------------------------------------------------
# _cmd_compile
# ---------------------------------------------------------------------------


async def test_cmd_compile_success(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.compiled = ["weather"]
    mock_result.skipped = []
    mock_result.failed = []
    mock_result.total_endpoints = 5

    args = MagicMock()
    args.llm_enhance = False
    args.dry_run = False

    with (
        patch("mce.__main__.load_config") as mock_config,
        patch("mce.compiler.orchestrator.Orchestrator.compile_all", new=AsyncMock(return_value=mock_result)),
    ):
        mock_config.return_value = MagicMock()
        code = await _cmd_compile(args)

    assert code == 0


async def test_cmd_compile_with_failures_returns_1(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.compiled = []
    mock_result.skipped = []
    mock_result.failed = ["bad_server"]
    mock_result.total_endpoints = 0

    args = MagicMock()
    args.llm_enhance = False
    args.dry_run = False

    with (
        patch("mce.__main__.load_config"),
        patch("mce.compiler.orchestrator.Orchestrator.compile_all", new=AsyncMock(return_value=mock_result)),
    ):
        code = await _cmd_compile(args)

    assert code == 1


async def test_cmd_compile_llm_enhance_sets_config(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.compiled = []
    mock_result.skipped = ["weather"]
    mock_result.failed = []
    mock_result.total_endpoints = 0

    args = MagicMock()
    args.llm_enhance = True
    args.dry_run = False

    config_mock = MagicMock()
    with (
        patch("mce.__main__.load_config", return_value=config_mock),
        patch("mce.compiler.orchestrator.Orchestrator.compile_all", new=AsyncMock(return_value=mock_result)),
    ):
        await _cmd_compile(args)

    # llm_enhance should have been set on config
    assert config_mock.llm_enhance is True


async def test_cmd_compile_skipped_sources_exits_0(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.compiled = []
    mock_result.skipped = ["weather"]
    mock_result.failed = []
    mock_result.total_endpoints = 0

    args = MagicMock()
    args.llm_enhance = False
    args.dry_run = True

    with (
        patch("mce.__main__.load_config"),
        patch("mce.compiler.orchestrator.Orchestrator.compile_all", new=AsyncMock(return_value=mock_result)),
    ):
        code = await _cmd_compile(args)

    assert code == 0


# ---------------------------------------------------------------------------
# _cmd_serve
# ---------------------------------------------------------------------------


async def test_cmd_serve_stdio_transport(tmp_path: Path) -> None:
    args = MagicMock()
    args.host = None
    args.transport = "stdio"
    args.port = None

    mock_cache = AsyncMock()
    mock_cache.initialize = AsyncMock()
    mock_cache.cleanup_expired = AsyncMock()

    mock_registry = MagicMock()
    mock_registry.load = MagicMock()
    mock_registry.list_servers = MagicMock(return_value=[])

    mock_mcp = AsyncMock()
    mock_mcp.run_stdio_async = AsyncMock()

    with (
        patch("mce.__main__.load_config") as mock_cfg,
        patch("mce.runtime.cache.CacheStore", return_value=mock_cache),
        patch("mce.runtime.registry.Registry", return_value=mock_registry),
        patch("mce.server.create_server", return_value=mock_mcp),
    ):
        cfg = MagicMock()
        cfg.cache_db_path = str(tmp_path / "cache.db")
        cfg.cache_ttl_seconds = 3600
        cfg.cache_max_entries = 500
        cfg.compiled_output_dir = str(tmp_path / "compiled")
        cfg.host = "0.0.0.0"
        cfg.port = 8000
        mock_cfg.return_value = cfg
        code = await _cmd_serve(args)

    mock_mcp.run_stdio_async.assert_awaited_once()
    assert code == 0


async def test_cmd_serve_http_transport(tmp_path: Path) -> None:
    args = MagicMock()
    args.host = "127.0.0.1"
    args.transport = "http"
    args.port = 9000

    mock_cache = AsyncMock()
    mock_cache.initialize = AsyncMock()
    mock_cache.cleanup_expired = AsyncMock()

    mock_registry = MagicMock()
    mock_registry.load = MagicMock()
    mock_registry.list_servers = MagicMock(return_value=[])

    mock_mcp = AsyncMock()
    mock_mcp.run_http_async = AsyncMock()

    with (
        patch("mce.__main__.load_config") as mock_cfg,
        patch("mce.runtime.cache.CacheStore", return_value=mock_cache),
        patch("mce.runtime.registry.Registry", return_value=mock_registry),
        patch("mce.server.create_server", return_value=mock_mcp),
    ):
        cfg = MagicMock()
        cfg.cache_db_path = str(tmp_path / "cache.db")
        cfg.cache_ttl_seconds = 3600
        cfg.cache_max_entries = 500
        cfg.compiled_output_dir = str(tmp_path / "compiled")
        cfg.host = "0.0.0.0"
        cfg.port = 8000
        mock_cfg.return_value = cfg
        code = await _cmd_serve(args)

    mock_mcp.run_http_async.assert_awaited_once()
    assert code == 0


async def test_cmd_serve_overrides_host_and_port(tmp_path: Path) -> None:
    args = MagicMock()
    args.host = "custom-host"
    args.transport = "stdio"
    args.port = 1234

    mock_cache = AsyncMock()
    mock_cache.initialize = AsyncMock()
    mock_cache.cleanup_expired = AsyncMock()

    mock_registry = MagicMock()
    mock_registry.load = MagicMock()
    mock_registry.list_servers = MagicMock(return_value=[])

    mock_mcp = AsyncMock()
    mock_mcp.run_stdio_async = AsyncMock()

    with (
        patch("mce.__main__.load_config") as mock_cfg,
        patch("mce.runtime.cache.CacheStore", return_value=mock_cache),
        patch("mce.runtime.registry.Registry", return_value=mock_registry),
        patch("mce.server.create_server", return_value=mock_mcp),
    ):
        cfg = MagicMock()
        cfg.cache_db_path = str(tmp_path / "cache.db")
        cfg.cache_ttl_seconds = 3600
        cfg.cache_max_entries = 500
        cfg.compiled_output_dir = str(tmp_path / "compiled")
        cfg.host = "0.0.0.0"
        cfg.port = 8000
        mock_cfg.return_value = cfg
        await _cmd_serve(args)

    # Host and port should have been overridden on config
    assert cfg.host == "custom-host"
    assert cfg.port == 1234


# ---------------------------------------------------------------------------
# _cmd_run
# ---------------------------------------------------------------------------


async def test_cmd_run_compile_failure_returns_1() -> None:
    mock_result = MagicMock()
    mock_result.failed = ["bad_server"]

    args = MagicMock()
    args.transport = "stdio"

    with (
        patch("mce.__main__.load_config"),
        patch("mce.compiler.orchestrator.Orchestrator.compile_all", new=AsyncMock(return_value=mock_result)),
    ):
        code = await _cmd_run(args)

    assert code == 1


async def test_cmd_run_success_calls_serve() -> None:
    mock_result = MagicMock()
    mock_result.failed = []
    mock_result.compiled = ["weather"]
    mock_result.skipped = []
    mock_result.total_endpoints = 3

    args = MagicMock()
    args.transport = "stdio"
    args.host = None
    args.port = None

    with (
        patch("mce.__main__.load_config"),
        patch("mce.compiler.orchestrator.Orchestrator.compile_all", new=AsyncMock(return_value=mock_result)),
        patch("mce.__main__._cmd_serve", new=AsyncMock(return_value=0)) as mock_serve,
    ):
        code = await _cmd_run(args)

    mock_serve.assert_awaited_once()
    assert code == 0


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_no_args_exits_0(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["mce"])
    with (
        patch("mce.__main__.load_dotenv"),
        patch("mce.__main__.load_config", return_value=MagicMock(log_level="INFO")),
        patch("mce.__main__.setup_logging"),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()
    assert exc_info.value.code == 0


def test_main_compile_command_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["mce", "compile"])

    mock_result = MagicMock()
    mock_result.compiled = ["weather"]
    mock_result.skipped = []
    mock_result.failed = []
    mock_result.total_endpoints = 3

    with (
        patch("mce.__main__.load_dotenv"),
        patch("mce.__main__.load_config", return_value=MagicMock(log_level="INFO")),
        patch("mce.__main__.setup_logging"),
        patch("mce.compiler.orchestrator.Orchestrator.compile_all", new=AsyncMock(return_value=mock_result)),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 0


def test_main_config_load_exception_falls_back_to_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["mce"])
    with (
        patch("mce.__main__.load_dotenv"),
        patch("mce.__main__.load_config", side_effect=Exception("bad config")),
        patch("mce.__main__.setup_logging") as mock_setup,
        pytest.raises(SystemExit),
    ):
        main()
    # Falls back to INFO level
    mock_setup.assert_called_with("INFO")
