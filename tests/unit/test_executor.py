"""Unit tests for the CodeExecutor — uses mocks to avoid real Docker."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from mce.config import MCEConfig
from mce.errors import ExecutionError, ExecutionTimeoutError, LintError, SecurityViolationError
from mce.runtime.executor import CodeExecutor, _detect_servers_used

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path) -> MCEConfig:
    return MCEConfig(
        compiled_output_dir=str(tmp_path / "compiled"),
        cache_db_path=str(tmp_path / "data" / "cache.db"),
        cache_enabled=True,
        cache_ttl_seconds=3600,
        execution_timeout_seconds=10,
        log_level="DEBUG",
        debug=True,
        docker_image="mce-sandbox:latest",
        max_code_size_bytes=65_536,
    )


def _make_mock_cache() -> AsyncMock:
    cache = AsyncMock()
    cache.store = AsyncMock(return_value="fake-cache-id-abc123")
    return cache


# ---------------------------------------------------------------------------
# _detect_servers_used
# ---------------------------------------------------------------------------


def test_detect_servers_used_from_import() -> None:
    code = "from weather.functions import get_current_weather"
    assert _detect_servers_used(code) == ["weather"]


def test_detect_servers_used_import_style() -> None:
    code = "import hotel.functions"
    assert _detect_servers_used(code) == ["hotel"]


def test_detect_servers_used_multiple_servers() -> None:
    code = "from weather.functions import fn\nfrom hotel.functions import book"
    servers = _detect_servers_used(code)
    assert sorted(servers) == ["hotel", "weather"]


def test_detect_servers_used_no_server_imports() -> None:
    code = "import os\nimport json\nresult = 42"
    assert _detect_servers_used(code) == []


def test_detect_servers_used_deduplicates() -> None:
    code = "from weather.functions import fn1\nfrom weather.functions import fn2"
    assert _detect_servers_used(code) == ["weather"]


# ---------------------------------------------------------------------------
# CodeExecutor.__init__
# ---------------------------------------------------------------------------


def test_executor_init(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    cache = _make_mock_cache()
    executor = CodeExecutor(config, cache)
    assert executor._config is config
    assert executor._cache is cache


# ---------------------------------------------------------------------------
# _lint_code
# ---------------------------------------------------------------------------


def test_lint_code_skips_when_ruff_not_found(tmp_path: Path) -> None:
    """If ruff is not in PATH, linting is skipped (no LintError)."""
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    with patch("subprocess.run", side_effect=FileNotFoundError("ruff not found")):
        executor._lint_code("result = 42")  # must not raise


def test_lint_code_timeout_is_skipped(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["ruff"], timeout=10)):
        executor._lint_code("result = 42")  # must not raise


def test_lint_code_raises_lint_error_on_failure(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "E501 line too long"
    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(LintError) as exc_info:
            executor._lint_code("x = 1")
        assert exc_info.value.lint_output == "E501 line too long"


def test_lint_code_passes_on_zero_returncode(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    mock_result = MagicMock()
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result):
        executor._lint_code("result = 42")  # must not raise


# ---------------------------------------------------------------------------
# _build_execution_code
# ---------------------------------------------------------------------------


def test_build_execution_code_injects_sys_path(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    code = executor._build_execution_code("result = 1", ["weather"])
    assert "sys.path.insert" in code
    assert "/mce_compiled" in code
    assert "result = 1" in code


# ---------------------------------------------------------------------------
# _parse_output
# ---------------------------------------------------------------------------


def test_parse_output_empty_returns_error_result(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    result = executor._parse_output("", 100)
    assert result.success is False
    assert "No output" in (result.error or "")


def test_parse_output_valid_success_json(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    payload = json.dumps({"success": True, "data": {"temp": 22}})
    result = executor._parse_output(payload, 50)
    assert result.success is True
    assert result.data == {"temp": 22}
    assert result.execution_time_ms == 50


def test_parse_output_valid_failure_json(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    payload = json.dumps({"success": False, "error": "something went wrong"})
    result = executor._parse_output(payload, 75)
    assert result.success is False
    assert result.error is not None
    assert "something went wrong" in result.error


def test_parse_output_non_json_returns_raw_as_data(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    result = executor._parse_output("raw text output", 10)
    assert result.success is True
    assert isinstance(result.data, str) and "raw text output" in result.data


def test_parse_output_traceback_only_in_debug_mode(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.debug = True
    executor = CodeExecutor(config, _make_mock_cache())
    payload = json.dumps({"success": False, "error": "err", "traceback": "Traceback..."})
    result = executor._parse_output(payload, 10)
    assert result.traceback == "Traceback..."


def test_parse_output_traceback_hidden_in_non_debug_mode(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.debug = False
    executor = CodeExecutor(config, _make_mock_cache())
    payload = json.dumps({"success": False, "error": "err", "traceback": "Traceback..."})
    result = executor._parse_output(payload, 10)
    assert result.traceback is None


# ---------------------------------------------------------------------------
# _run_in_docker — mocked subprocess
# ---------------------------------------------------------------------------


def _make_successful_docker_result(output: str) -> MagicMock:
    mock = MagicMock()
    mock.returncode = 0
    mock.stdout = output.encode("utf-8")
    mock.stderr = b""
    return mock


def test_run_in_docker_success(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    payload = json.dumps({"success": True, "data": "ok"})
    with patch("subprocess.run", return_value=_make_successful_docker_result(payload)):
        output = executor._run_in_docker("print('hello')", [])
    assert "ok" in output


def test_run_in_docker_timeout_raises(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    with (
        patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["docker"], timeout=10)),
        pytest.raises(ExecutionTimeoutError),
    ):
        executor._run_in_docker("import time; time.sleep(999)", [])


def test_run_in_docker_file_not_found_raises(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    with (
        patch("subprocess.run", side_effect=FileNotFoundError("docker not found")),
        pytest.raises(ExecutionError, match="docker CLI not found"),
    ):
        executor._run_in_docker("result = 1", [])


def test_run_in_docker_nonzero_exit_raises_execution_error(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = b""
    mock_result.stderr = b"runtime error"
    with patch("subprocess.run", return_value=mock_result), pytest.raises(ExecutionError):
        executor._run_in_docker("result = 1", [])


def test_run_in_docker_image_not_found_raises(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    mock_result = MagicMock()
    mock_result.returncode = 125
    mock_result.stdout = b""
    mock_result.stderr = b"Unable to find image 'mce-sandbox:latest'"
    with patch("subprocess.run", return_value=mock_result), pytest.raises(ExecutionError, match="not found"):
        executor._run_in_docker("result = 1", [])


def test_run_in_docker_with_docker_host(tmp_path: Path) -> None:
    """docker_host config causes -H flag in command."""
    config = _make_config(tmp_path)
    config.docker_host = "unix:///var/run/docker.sock"
    executor = CodeExecutor(config, _make_mock_cache())
    payload = json.dumps({"success": True, "data": None})
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured_cmd.extend(cmd)
        m = MagicMock()
        m.returncode = 0
        m.stdout = payload.encode()
        m.stderr = b""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        executor._run_in_docker("result = 1", [])

    assert "-H" in captured_cmd
    assert "unix:///var/run/docker.sock" in captured_cmd


# ---------------------------------------------------------------------------
# execute() — full pipeline mocked
# ---------------------------------------------------------------------------


async def test_execute_raises_security_error_for_oversized_code(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.max_code_size_bytes = 10
    executor = CodeExecutor(config, _make_mock_cache())
    big_code = "x = 1" * 100
    with pytest.raises(SecurityViolationError, match="exceeds limit"):
        await executor.execute(big_code, "big code")


async def test_execute_raises_security_error_for_dangerous_imports(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    dangerous_code = "import subprocess\nresult = subprocess.run(['rm', '-rf', '/'])"
    with pytest.raises(SecurityViolationError):
        await executor.execute(dangerous_code, "dangerous code")


async def test_execute_full_happy_path(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.lint_enabled = True
    cache = _make_mock_cache()
    executor = CodeExecutor(config, cache)

    ruff_result = MagicMock()
    ruff_result.returncode = 0

    docker_output = json.dumps({"success": True, "data": {"result": 42}})
    docker_result = MagicMock()
    docker_result.returncode = 0
    docker_result.stdout = docker_output.encode()
    docker_result.stderr = b""

    with patch("subprocess.run") as mock_run:
        # First call = ruff lint, second call = docker
        mock_run.side_effect = [ruff_result, docker_result]
        result = await executor.execute("result = 42", "compute 42")

    assert result.success is True
    assert result.data == {"result": 42}
    assert result.cache_id is not None


async def test_execute_cache_disabled_no_store(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.cache_enabled = False
    config.lint_enabled = True
    cache = _make_mock_cache()
    executor = CodeExecutor(config, cache)

    ruff_result = MagicMock()
    ruff_result.returncode = 0
    docker_output = json.dumps({"success": True, "data": "ok"})
    docker_result = MagicMock()
    docker_result.returncode = 0
    docker_result.stdout = docker_output.encode()
    docker_result.stderr = b""

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [ruff_result, docker_result]
        result = await executor.execute("result = 'ok'", "ok code")

    # cache.store should NOT have been called
    cache.store.assert_not_awaited()
    assert result.cache_id is None


# ---------------------------------------------------------------------------
# _compute_swagger_hash
# ---------------------------------------------------------------------------


def test_compute_swagger_hash_returns_unknown_on_missing_dir(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    # compiled dir doesn't exist → fallback to "unknown"
    result = executor._compute_swagger_hash(["nonexistent_server"])
    assert result in ("unknown", "no-servers")


def test_compute_swagger_hash_no_servers_returns_no_servers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    result = executor._compute_swagger_hash([])
    assert result == "no-servers"
