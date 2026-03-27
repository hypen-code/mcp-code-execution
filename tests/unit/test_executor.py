"""Unit tests for CodeExecutor — mocks aiodocker so no real Docker daemon is needed."""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from mce.config import MCEConfig
from mce.errors import ExecutionError, ExecutionTimeoutError, LintError, SecurityViolationError
from mce.runtime.executor import CodeExecutor, _detect_servers_used, _WarmPool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, *, sandbox_mode: str = "cold") -> MCEConfig:
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
        sandbox_mode=sandbox_mode,
        warm_pool_size=1,
    )


def _make_mock_cache() -> AsyncMock:
    cache = AsyncMock()
    cache.store = AsyncMock(return_value="fake-cache-id-abc123")
    return cache


def _make_docker_mock() -> MagicMock:
    """Return a MagicMock that mimics an aiodocker.Docker client."""
    docker = MagicMock()
    docker.close = AsyncMock()
    docker.version = AsyncMock(return_value={"Version": "24.0.0"})
    return docker


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
    assert sorted(_detect_servers_used(code)) == ["hotel", "weather"]


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
    assert executor._docker is None


# ---------------------------------------------------------------------------
# startup / shutdown
# ---------------------------------------------------------------------------


async def test_startup_cold_mode_opens_docker(tmp_path: Path) -> None:
    config = _make_config(tmp_path, sandbox_mode="cold")
    executor = CodeExecutor(config, _make_mock_cache())

    with patch("aiodocker.Docker") as mock_docker_cls:
        mock_client = _make_docker_mock()
        mock_docker_cls.return_value = mock_client
        await executor.startup()

    assert executor._docker is mock_client
    assert executor._warm_pool is None


async def test_startup_warm_mode_creates_pool(tmp_path: Path) -> None:
    config = _make_config(tmp_path, sandbox_mode="warm")
    executor = CodeExecutor(config, _make_mock_cache())

    mock_container = AsyncMock()
    mock_container.id = "abc123def456"
    mock_container.start = AsyncMock()

    mock_containers_api = AsyncMock()
    mock_containers_api.create = AsyncMock(return_value=mock_container)
    mock_containers_api.list = AsyncMock(return_value=[])  # no stale containers

    mock_client = _make_docker_mock()
    mock_client.containers = mock_containers_api

    with patch("aiodocker.Docker", return_value=mock_client):
        await executor.startup()

    assert executor._warm_pool is not None
    mock_containers_api.create.assert_awaited_once()


async def test_startup_warm_mode_removes_stale_containers(tmp_path: Path) -> None:
    config = _make_config(tmp_path, sandbox_mode="warm")
    executor = CodeExecutor(config, _make_mock_cache())

    stale = AsyncMock()
    stale.id = "stale000dead"
    stale.delete = AsyncMock()

    fresh = AsyncMock()
    fresh.id = "fresh111alive"
    fresh.start = AsyncMock()

    mock_containers_api = AsyncMock()
    mock_containers_api.list = AsyncMock(return_value=[stale])
    mock_containers_api.create = AsyncMock(return_value=fresh)

    mock_client = _make_docker_mock()
    mock_client.containers = mock_containers_api

    with patch("aiodocker.Docker", return_value=mock_client):
        await executor.startup()

    stale.delete.assert_awaited_once_with(force=True)


async def test_shutdown_closes_docker_client(tmp_path: Path) -> None:
    config = _make_config(tmp_path, sandbox_mode="cold")
    executor = CodeExecutor(config, _make_mock_cache())

    mock_client = _make_docker_mock()
    executor._docker = mock_client

    await executor.shutdown()

    mock_client.close.assert_awaited_once()
    assert executor._docker is None


async def test_shutdown_stops_warm_containers(tmp_path: Path) -> None:
    config = _make_config(tmp_path, sandbox_mode="warm")
    executor = CodeExecutor(config, _make_mock_cache())

    mock_container = AsyncMock()
    mock_container.id = "abc123def456"
    mock_container.delete = AsyncMock()

    # shutdown() iterates _warm_containers (all created), not _warm_pool (idle only)
    executor._warm_containers = [mock_container]
    executor._docker = _make_docker_mock()

    await executor.shutdown()

    mock_container.delete.assert_awaited_once_with(force=True)
    assert executor._warm_containers == []


async def test_shutdown_safe_when_startup_not_called(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    await executor.shutdown()  # must not raise


# ---------------------------------------------------------------------------
# _lint_code
# ---------------------------------------------------------------------------


def test_lint_code_skips_when_ruff_not_found(tmp_path: Path) -> None:
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
# _run_cold — mocked aiodocker
# ---------------------------------------------------------------------------


def _make_cold_container_mock(stdout_output: str, exit_code: int = 0) -> AsyncMock:
    """Build a mock aiodocker container for cold-mode tests."""
    container = AsyncMock()
    container.start = AsyncMock()
    container.wait = AsyncMock(return_value={"StatusCode": exit_code})
    container.log = AsyncMock(side_effect=lambda stdout, stderr: [stdout_output] if stdout else [])
    container.delete = AsyncMock()
    return container


async def test_run_cold_success(tmp_path: Path) -> None:
    config = _make_config(tmp_path, sandbox_mode="cold")
    executor = CodeExecutor(config, _make_mock_cache())

    payload = json.dumps({"success": True, "data": "ok"})
    mock_container = _make_cold_container_mock(payload)

    mock_docker = _make_docker_mock()
    mock_docker.containers = AsyncMock()
    mock_docker.containers.create = AsyncMock(return_value=mock_container)
    executor._docker = mock_docker

    output = await executor._run_cold("result = 'ok'", [])
    assert "ok" in output


async def test_run_cold_timeout_raises(tmp_path: Path) -> None:
    config = _make_config(tmp_path, sandbox_mode="cold")
    executor = CodeExecutor(config, _make_mock_cache())

    mock_container = AsyncMock()
    mock_container.start = AsyncMock()
    mock_container.wait = AsyncMock(side_effect=TimeoutError())
    mock_container.delete = AsyncMock()

    mock_docker = _make_docker_mock()
    mock_docker.containers = AsyncMock()
    mock_docker.containers.create = AsyncMock(return_value=mock_container)
    executor._docker = mock_docker

    with pytest.raises(ExecutionTimeoutError):
        await executor._run_cold("import time; time.sleep(999)", [])


async def test_run_cold_docker_create_error_raises(tmp_path: Path) -> None:
    import aiodocker.exceptions  # noqa: PLC0415

    config = _make_config(tmp_path, sandbox_mode="cold")
    executor = CodeExecutor(config, _make_mock_cache())

    mock_docker = _make_docker_mock()
    mock_docker.containers = AsyncMock()
    mock_docker.containers.create = AsyncMock(
        side_effect=aiodocker.exceptions.DockerError(status=500, message="server error")
    )
    executor._docker = mock_docker

    with pytest.raises(ExecutionError):
        await executor._run_cold("result = 1", [])


async def test_run_cold_nonzero_exit_raises_execution_error(tmp_path: Path) -> None:
    config = _make_config(tmp_path, sandbox_mode="cold")
    executor = CodeExecutor(config, _make_mock_cache())

    mock_container = AsyncMock()
    mock_container.start = AsyncMock()
    mock_container.wait = AsyncMock(return_value={"StatusCode": 137})
    mock_container.log = AsyncMock(side_effect=lambda stdout, stderr: ["OOM killed"] if stderr else [])
    mock_container.delete = AsyncMock()

    mock_docker = _make_docker_mock()
    mock_docker.containers = AsyncMock()
    mock_docker.containers.create = AsyncMock(return_value=mock_container)
    executor._docker = mock_docker

    with pytest.raises(ExecutionError):
        await executor._run_cold("result = 1", [])


async def test_run_cold_container_deleted_on_success(tmp_path: Path) -> None:
    config = _make_config(tmp_path, sandbox_mode="cold")
    executor = CodeExecutor(config, _make_mock_cache())

    payload = json.dumps({"success": True, "data": None})
    mock_container = _make_cold_container_mock(payload)

    mock_docker = _make_docker_mock()
    mock_docker.containers = AsyncMock()
    mock_docker.containers.create = AsyncMock(return_value=mock_container)
    executor._docker = mock_docker

    await executor._run_cold("result = None", [])
    mock_container.delete.assert_awaited_once()


# ---------------------------------------------------------------------------
# _run_warm — mocked aiodocker exec
# ---------------------------------------------------------------------------


def _make_exec_stream_mock(stdout_bytes: bytes) -> AsyncMock:
    """Return a mock async context manager that yields one stdout frame then EOF."""

    class _Msg:
        def __init__(self, data: bytes) -> None:
            self.stream = 1  # stdout
            self.data = data

    stream = AsyncMock()
    stream.read_out = AsyncMock(side_effect=[_Msg(stdout_bytes), None])
    stream.__aenter__ = AsyncMock(return_value=stream)
    stream.__aexit__ = AsyncMock(return_value=None)
    return stream


async def test_run_warm_success(tmp_path: Path) -> None:
    config = _make_config(tmp_path, sandbox_mode="warm")
    executor = CodeExecutor(config, _make_mock_cache())

    payload = json.dumps({"success": True, "data": {"result": 42}})
    stream = _make_exec_stream_mock(payload.encode())

    exec_obj = AsyncMock()
    exec_obj.start = MagicMock(return_value=stream)

    mock_container = AsyncMock()
    mock_container.id = "warmcontainerid"
    mock_container.exec = AsyncMock(return_value=exec_obj)

    warm_pool = _WarmPool()
    await warm_pool.push(mock_container)
    executor._warm_pool = warm_pool
    executor._docker = _make_docker_mock()

    output = await executor._run_warm("result = 42", [])
    assert "42" in output


async def test_run_warm_container_returned_to_pool_after_exec(tmp_path: Path) -> None:
    """Container must be returned to the pool even on success."""
    config = _make_config(tmp_path, sandbox_mode="warm")
    executor = CodeExecutor(config, _make_mock_cache())

    payload = json.dumps({"success": True, "data": None})
    stream = _make_exec_stream_mock(payload.encode())
    exec_obj = AsyncMock()
    exec_obj.start = MagicMock(return_value=stream)

    mock_container = AsyncMock()
    mock_container.id = "warmid"
    mock_container.exec = AsyncMock(return_value=exec_obj)

    warm_pool = _WarmPool()
    await warm_pool.push(mock_container)
    executor._warm_pool = warm_pool
    executor._docker = _make_docker_mock()

    await executor._run_warm("result = None", [])

    assert warm_pool._queue.qsize() == 1


async def test_run_warm_all_busy_raises(tmp_path: Path) -> None:
    config = _make_config(tmp_path, sandbox_mode="warm")
    executor = CodeExecutor(config, _make_mock_cache())

    executor._warm_pool = _WarmPool()  # empty pool
    executor._docker = _make_docker_mock()

    with pytest.raises(ExecutionError, match="busy"), patch("asyncio.wait_for", side_effect=TimeoutError()):
        await executor._run_warm("result = 1", [])


# ---------------------------------------------------------------------------
# execute() — full pipeline mocked
# ---------------------------------------------------------------------------


async def test_execute_raises_if_startup_not_called(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    with pytest.raises(ExecutionError, match="startup"):
        await executor.execute("result = 1", "test")


async def test_execute_raises_security_error_for_oversized_code(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.max_code_size_bytes = 10
    executor = CodeExecutor(config, _make_mock_cache())
    executor._docker = _make_docker_mock()
    with pytest.raises(SecurityViolationError, match="exceeds limit"):
        await executor.execute("x = 1" * 100, "big code")


async def test_execute_raises_security_error_for_dangerous_imports(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    executor._docker = _make_docker_mock()
    dangerous = "import subprocess\nresult = subprocess.run(['rm', '-rf', '/'])"
    with pytest.raises(SecurityViolationError):
        await executor.execute(dangerous, "dangerous code")


async def test_execute_cold_happy_path(tmp_path: Path) -> None:
    config = _make_config(tmp_path, sandbox_mode="cold")
    config.lint_enabled = True
    cache = _make_mock_cache()
    executor = CodeExecutor(config, cache)

    docker_output = json.dumps({"success": True, "data": {"result": 42}})
    mock_container = _make_cold_container_mock(docker_output)

    mock_docker = _make_docker_mock()
    mock_docker.containers = AsyncMock()
    mock_docker.containers.create = AsyncMock(return_value=mock_container)
    executor._docker = mock_docker

    ruff_result = MagicMock()
    ruff_result.returncode = 0

    with patch("subprocess.run", return_value=ruff_result):
        result = await executor.execute("result = 42", "compute 42")

    assert result.success is True
    assert result.data == {"result": 42}
    assert result.cache_id is not None
    cache.store.assert_awaited_once()


async def test_execute_cold_cache_disabled_no_store(tmp_path: Path) -> None:
    config = _make_config(tmp_path, sandbox_mode="cold")
    config.cache_enabled = False
    cache = _make_mock_cache()
    executor = CodeExecutor(config, cache)

    payload = json.dumps({"success": True, "data": "ok"})
    mock_container = _make_cold_container_mock(payload)

    mock_docker = _make_docker_mock()
    mock_docker.containers = AsyncMock()
    mock_docker.containers.create = AsyncMock(return_value=mock_container)
    executor._docker = mock_docker

    result = await executor.execute("result = 'ok'", "ok code")

    cache.store.assert_not_awaited()
    assert result.cache_id is None


# ---------------------------------------------------------------------------
# _compute_swagger_hash
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _WarmPool.drain
# ---------------------------------------------------------------------------


async def test_warm_pool_drain_returns_all_containers() -> None:
    pool = _WarmPool()
    c1, c2 = AsyncMock(), AsyncMock()
    await pool.push(c1)
    await pool.push(c2)
    containers = await pool.drain()
    assert len(containers) == 2
    assert pool._queue.empty()


async def test_warm_pool_drain_empty_pool() -> None:
    pool = _WarmPool()
    assert await pool.drain() == []


# ---------------------------------------------------------------------------
# shutdown — CancelledError handling
# ---------------------------------------------------------------------------


async def test_shutdown_reraises_cancelled_error_after_cleanup(tmp_path: Path) -> None:
    config = _make_config(tmp_path, sandbox_mode="warm")
    executor = CodeExecutor(config, _make_mock_cache())

    mock_container = AsyncMock()
    mock_container.id = "abc123def456"
    mock_container.delete = AsyncMock(side_effect=asyncio.CancelledError())

    executor._warm_containers = [mock_container]
    executor._docker = _make_docker_mock()

    with pytest.raises(asyncio.CancelledError):
        await executor.shutdown()

    # Containers list is still cleared despite the error
    assert executor._warm_containers == []


# ---------------------------------------------------------------------------
# execute() — warm mode path
# ---------------------------------------------------------------------------


async def test_execute_warm_happy_path(tmp_path: Path) -> None:
    import json as _json  # noqa: PLC0415

    config = _make_config(tmp_path, sandbox_mode="warm")
    cache = _make_mock_cache()
    executor = CodeExecutor(config, cache)

    payload = _json.dumps({"success": True, "data": {"value": 7}})
    stream = _make_exec_stream_mock(payload.encode())
    exec_obj = AsyncMock()
    exec_obj.start = MagicMock(return_value=stream)

    mock_container = AsyncMock()
    mock_container.id = "warmexecid123"
    mock_container.exec = AsyncMock(return_value=exec_obj)

    warm_pool = _WarmPool()
    await warm_pool.push(mock_container)
    executor._warm_pool = warm_pool
    executor._docker = _make_docker_mock()

    result = await executor.execute("result = 7", "compute 7")
    assert result.success is True


# ---------------------------------------------------------------------------
# _run_cold — container delete failure is swallowed
# ---------------------------------------------------------------------------


async def test_run_cold_container_delete_failure_is_swallowed(tmp_path: Path) -> None:
    config = _make_config(tmp_path, sandbox_mode="cold")
    executor = CodeExecutor(config, _make_mock_cache())

    payload = json.dumps({"success": True, "data": None})
    mock_container = _make_cold_container_mock(payload)
    mock_container.delete = AsyncMock(side_effect=Exception("delete failed"))

    mock_docker = _make_docker_mock()
    mock_docker.containers = AsyncMock()
    mock_docker.containers.create = AsyncMock(return_value=mock_container)
    executor._docker = mock_docker

    # Should not raise despite delete failure
    output = await executor._run_cold("result = None", [])
    assert output


# ---------------------------------------------------------------------------
# _run_warm — Docker exec create failure
# ---------------------------------------------------------------------------


async def test_run_warm_exec_create_docker_error_raises(tmp_path: Path) -> None:
    import aiodocker.exceptions  # noqa: PLC0415

    config = _make_config(tmp_path, sandbox_mode="warm")
    executor = CodeExecutor(config, _make_mock_cache())

    mock_container = AsyncMock()
    mock_container.id = "warmcontainerid"
    mock_container.exec = AsyncMock(side_effect=aiodocker.exceptions.DockerError(status=500, message="exec failed"))

    warm_pool = _WarmPool()
    await warm_pool.push(mock_container)
    executor._warm_pool = warm_pool
    executor._docker = _make_docker_mock()

    with pytest.raises(ExecutionError, match="docker exec create failed"):
        await executor._run_warm("result = 1", [])


# ---------------------------------------------------------------------------
# _run_warm — stream timeout
# ---------------------------------------------------------------------------


async def test_run_warm_stream_timeout_raises(tmp_path: Path) -> None:
    config = _make_config(tmp_path, sandbox_mode="warm")
    executor = CodeExecutor(config, _make_mock_cache())

    stream = AsyncMock()
    stream.read_out = AsyncMock(side_effect=TimeoutError())
    stream.__aenter__ = AsyncMock(return_value=stream)
    stream.__aexit__ = AsyncMock(return_value=None)

    exec_obj = AsyncMock()
    exec_obj.start = MagicMock(return_value=stream)

    mock_container = AsyncMock()
    mock_container.id = "warmtimeoutid"
    mock_container.exec = AsyncMock(return_value=exec_obj)

    warm_pool = _WarmPool()
    await warm_pool.push(mock_container)
    executor._warm_pool = warm_pool
    executor._docker = _make_docker_mock()

    with pytest.raises(ExecutionTimeoutError):
        await executor._run_warm("import time; time.sleep(999)", [])


# ---------------------------------------------------------------------------
# _compute_swagger_hash
# ---------------------------------------------------------------------------


def test_compute_swagger_hash_returns_unknown_on_missing_dir(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    result = executor._compute_swagger_hash(["nonexistent_server"])
    assert result in ("unknown", "no-servers")


def test_compute_swagger_hash_no_servers_returns_no_servers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executor = CodeExecutor(config, _make_mock_cache())
    assert executor._compute_swagger_hash([]) == "no-servers"
