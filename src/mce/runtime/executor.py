"""Docker-based sandboxed code executor — async, event-loop-safe execution pipeline.

Execution modes (configured via MCE_SANDBOX_MODE):

  warm (default)
    A pool of MCE_WARM_POOL_SIZE persistent containers is created at startup.
    Each request borrows a container from the pool, uses ``docker exec`` to run
    the entrypoint inside it, then returns the container for the next request.
    Container cold-start overhead is paid once at startup, not per request.

  cold
    A brand-new container is created for every request, started, waited on,
    then deleted.  Full isolation between requests at the cost of cold-start
    latency (~100–400 ms per call).

Both modes communicate with Docker exclusively through ``aiodocker`` — a fully
async client that never blocks the asyncio event loop.  Code is delivered to
the entrypoint via the ``MCE_EXEC_CODE`` environment variable (base64-encoded)
so neither mode needs an interactive stdin pipe.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import subprocess
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiodocker
import aiodocker.containers

from mce.errors import ExecutionError, ExecutionTimeoutError, LintError, SecurityViolationError
from mce.models import ExecutionResult
from mce.security.ast_guard import ASTGuard
from mce.security.vault import build_all_server_env_vars
from mce.utils.hashing import combine_hashes
from mce.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

    from mce.config import MCEConfig
    from mce.models import AuthConfig
    from mce.runtime.cache import CacheStore

logger = get_logger(__name__)

# Pattern to detect server function imports in user code
_SERVER_IMPORT_RE = re.compile(r"from\s+(\w+)\.functions\s+import|import\s+(\w+)\.functions")

# Maximum bytes of container output to capture
_MAX_OUTPUT_BYTES = 1_048_576  # 1 MB

# Mount point for compiled functions inside every sandbox container
_CONTAINER_COMPILED_PATH = "/mce_compiled"


def _detect_servers_used(code: str) -> list[str]:
    """Detect which server function modules are imported in the code.

    Args:
        code: Python source code.

    Returns:
        Sorted list of server names referenced by ``from <name>.functions import``.
    """
    servers: set[str] = set()
    for match in _SERVER_IMPORT_RE.finditer(code):
        name = match.group(1) or match.group(2)
        if name:
            servers.add(name)
    return sorted(servers)


# ---------------------------------------------------------------------------
# Warm container pool
# ---------------------------------------------------------------------------


class _WarmPool:
    """Thread-safe async pool of persistent Docker containers.

    Containers are borrowed via the :meth:`borrow` context manager.  On exit
    the container is returned to the pool so the next request can use it.

    If all containers are busy the borrow blocks until one is released or the
    timeout (= ``execution_timeout_seconds + 10 s``) expires.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[aiodocker.containers.DockerContainer] = asyncio.Queue()

    async def push(self, container: aiodocker.containers.DockerContainer) -> None:
        """Add a container to the pool.

        Args:
            container: Running Docker container to make available.
        """
        await self._queue.put(container)

    @asynccontextmanager
    async def borrow(self, timeout: float) -> AsyncGenerator[aiodocker.containers.DockerContainer, None]:
        """Borrow a container from the pool for the duration of the block.

        Args:
            timeout: Maximum seconds to wait for an available container.

        Yields:
            A running warm container.

        Raises:
            ExecutionError: If no container becomes available within *timeout*.
        """
        try:
            container = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError as exc:
            raise ExecutionError(
                "All warm containers are busy — retry in a moment or increase MCE_WARM_POOL_SIZE"
            ) from exc
        try:
            yield container
        finally:
            self._queue.put_nowait(container)

    async def drain(self) -> list[aiodocker.containers.DockerContainer]:
        """Remove and return every container currently in the pool.

        Returns:
            All containers that were waiting in the pool.
        """
        containers: list[aiodocker.containers.DockerContainer] = []
        while True:
            try:
                containers.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return containers


# ---------------------------------------------------------------------------
# Main executor
# ---------------------------------------------------------------------------


class CodeExecutor:
    """Manages the full execution pipeline: validate → lint → sandbox → cache.

    Call :meth:`startup` once before the first :meth:`execute` call, and
    :meth:`shutdown` when the server is stopping to clean up Docker resources.
    """

    def __init__(
        self, config: MCEConfig, cache: CacheStore, auth_configs: Mapping[str, AuthConfig | None] | None = None
    ) -> None:
        """Initialise the executor (does not connect to Docker yet).

        Args:
            config: MCE configuration.
            cache: Cache store for persisting successful executions.
            auth_configs: Optional mapping of server module name to AuthConfig for dynamic token fetching.
        """
        self._config = config
        self._cache = cache
        self._auth_configs: Mapping[str, AuthConfig | None] = auth_configs or {}
        self._ast_guard = ASTGuard()
        self._compiled_dir = Path(config.compiled_output_dir)
        self._docker: aiodocker.Docker | None = None
        self._warm_pool: _WarmPool | None = None
        # All warm containers ever created — used by shutdown() to guarantee full cleanup
        # even for containers currently borrowed from the pool mid-execution.
        self._warm_containers: list[aiodocker.containers.DockerContainer] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Open the Docker client and, in warm mode, pre-create the container pool.

        Raises:
            ExecutionError: If Docker is unreachable or image is missing.
        """
        docker_url = self._config.docker_host or None
        self._docker = aiodocker.Docker(url=docker_url)

        if self._config.sandbox_mode == "warm":
            self._warm_pool = _WarmPool()
            size = self._config.warm_pool_size
            # Remove stale mce-warm-* containers from any previous (possibly crashed) run
            # before creating fresh ones.  This prevents container accumulation across restarts.
            await self._cleanup_stale_warm_containers()
            logger.info("warm_pool_starting", size=size)
            for i in range(size):
                container = await self._create_warm_container(f"mce-warm-{i}-{uuid.uuid4().hex[:6]}")
                self._warm_containers.append(container)
                await self._warm_pool.push(container)
            logger.info("warm_pool_ready", size=size, image=self._config.docker_image)
        else:
            logger.info("cold_mode_active", image=self._config.docker_image)

    async def shutdown(self) -> None:
        """Remove all warm containers and close the Docker client.

        Uses ``delete(force=True)`` which is atomic — it sends SIGKILL and removes
        the container in a single Docker API call, eliminating the gap between
        ``stop()`` and ``delete()`` where a cancellation could leave a stopped-but-
        not-deleted container behind.

        ``asyncio.CancelledError`` is caught per-container so that a cancellation
        mid-loop does not skip the remaining containers.  The error is re-raised
        after all containers have been processed.

        Safe to call even if :meth:`startup` was never called.
        """
        cancelled = False

        if self._warm_containers and self._docker:
            logger.info("warm_pool_stopping", count=len(self._warm_containers))
            for container in self._warm_containers:
                try:
                    # force=True: SIGKILL + remove in one atomic Docker API call.
                    await container.delete(force=True)
                    logger.debug("warm_container_removed", id=container.id[:12])
                except asyncio.CancelledError:
                    # CancelledError is BaseException — not caught by `except Exception`.
                    # Swallow here so the loop continues to the remaining containers,
                    # then re-raise after all cleanup is done.
                    cancelled = True
                    logger.warning("warm_container_remove_interrupted", id=container.id[:12])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("warm_container_remove_failed", id=container.id[:12], error=str(exc))
            self._warm_containers.clear()

        if self._docker:
            with suppress(Exception):
                await self._docker.close()
            self._docker = None
            logger.info("docker_client_closed")

        if cancelled:
            raise asyncio.CancelledError()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(self, code: str, description: str) -> ExecutionResult:
        """Full execution pipeline: validate → lint → sandbox → cache.

        Args:
            code: Python code to execute in the sandbox.
            description: Brief description for caching and logging.

        Returns:
            ExecutionResult with success/failure and output data.

        Raises:
            SecurityViolationError: If code fails AST security scan.
            LintError: If code has syntax or lint issues.
            ExecutionTimeoutError: If execution exceeds timeout.
            ExecutionError: On Docker or runtime failure.
        """
        if self._docker is None:
            raise ExecutionError("CodeExecutor.startup() has not been called")

        start_ms = int(time.time() * 1000)

        # Enforce code size limit
        if len(code.encode()) > self._config.max_code_size_bytes:
            raise SecurityViolationError(
                f"Code size {len(code.encode())} bytes exceeds limit of {self._config.max_code_size_bytes}"
            )

        # Security scan
        self._ast_guard.validate(code, context=description[:100])

        # Optional lint check
        if self._config.lint_enabled:
            self._lint_code(code)

        # Detect servers used for credential injection
        servers_used = _detect_servers_used(code)

        # Prepend sys.path injection so compiled server modules are importable
        execution_code = self._build_execution_code(code, servers_used)

        # Run in Docker sandbox (warm or cold)
        if self._config.sandbox_mode == "warm":
            raw_output = await self._run_warm(execution_code, servers_used)
        else:
            raw_output = await self._run_cold(execution_code, servers_used)

        elapsed_ms = int(time.time() * 1000) - start_ms
        result = self._parse_output(raw_output, elapsed_ms)

        # Cache on success
        if result.success and self._config.cache_enabled:
            swagger_hash = self._compute_swagger_hash(servers_used)
            cache_id = await self._cache.store(code, description, servers_used, swagger_hash)
            result.cache_id = cache_id

        logger.info(
            "code_executed",
            mode=self._config.sandbox_mode,
            success=result.success,
            elapsed_ms=elapsed_ms,
            servers=servers_used,
            description=description[:60],
        )

        return result

    # ------------------------------------------------------------------
    # Lint
    # ------------------------------------------------------------------

    def _lint_code(self, code: str) -> None:
        """Run ruff linting on the code string.

        Args:
            code: Python source code to lint.

        Raises:
            LintError: If ruff finds issues.
        """
        try:
            result = subprocess.run(  # noqa: S603
                ["ruff", "check", "--select=E,F,W", "--stdin-filename", "code.py", "-"],  # noqa: S607
                input=code,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                raise LintError(
                    "Code has lint issues",
                    lint_output=result.stdout[:2000],
                )
        except subprocess.TimeoutExpired:
            logger.warning("lint_timeout_skipped")
        except FileNotFoundError:
            logger.warning("ruff_not_found_skipped")

    # ------------------------------------------------------------------
    # Code preparation
    # ------------------------------------------------------------------

    def _build_execution_code(self, user_code: str, servers_used: list[str]) -> str:  # noqa: ARG002
        """Prepend sys.path injection so compiled server modules resolve inside sandbox.

        Args:
            user_code: User-provided Python code.
            servers_used: Server names detected in imports (unused; kept for signature symmetry).

        Returns:
            Complete Python code ready for execution in sandbox.
        """
        path_injection = f"""
import sys as _sys
_sys.path.insert(0, {_CONTAINER_COMPILED_PATH!r})
"""
        return f"{path_injection}\n{user_code}"

    # ------------------------------------------------------------------
    # Docker container config helpers
    # ------------------------------------------------------------------

    def _base_host_config(self) -> dict[str, Any]:
        """Build the HostConfig dict shared by both warm and cold containers.

        Returns:
            Docker API HostConfig dict with resource limits and mounts.
        """
        compiled_host_path = str(self._compiled_dir.resolve())
        return {
            "Memory": 256 * 1_048_576,
            "MemorySwap": 256 * 1_048_576,
            "CpuPeriod": 100_000,
            "CpuQuota": 50_000,
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": "size=64m,mode=1777"},  # noqa: S108
            "Binds": [f"{compiled_host_path}:{_CONTAINER_COMPILED_PATH}:ro"],
            "NetworkMode": self._config.network_mode,
        }

    # ------------------------------------------------------------------
    # Warm mode
    # ------------------------------------------------------------------

    async def _cleanup_stale_warm_containers(self) -> None:
        """Remove any leftover mce-warm-* containers from previous server runs.

        Called at the start of warm-mode startup so Docker never accumulates
        orphaned containers across restarts or crashes.
        """
        assert self._docker is not None
        try:
            containers = await self._docker.containers.list(
                all=True,
                filters={"name": ["mce-warm-"]},
            )
        except aiodocker.exceptions.DockerError as exc:
            logger.warning("stale_container_list_failed", error=str(exc))
            return

        for container in containers:
            try:
                await container.delete(force=True)
                logger.debug("stale_warm_container_removed", id=container.id[:12])
            except Exception as exc:  # noqa: BLE001
                logger.warning("stale_warm_container_remove_failed", id=container.id[:12], error=str(exc))

        if containers:
            logger.info("stale_warm_containers_removed", count=len(containers))

    async def _create_warm_container(self, name: str) -> aiodocker.containers.DockerContainer:
        """Create and start a persistent warm container that stays alive between requests.

        The ENTRYPOINT is overridden to ``tail -f /dev/null`` so the container
        idles without consuming CPU.  Per-request code is delivered via
        ``docker exec`` + the ``MCE_EXEC_CODE`` env var.

        Args:
            name: Unique name for the container.

        Returns:
            Running Docker container ready to accept exec calls.

        Raises:
            ExecutionError: If the container cannot be created or started.
        """
        assert self._docker is not None
        config: dict[str, Any] = {
            "Image": self._config.docker_image,
            # Override Dockerfile ENTRYPOINT — container idles, code runs via exec
            "Entrypoint": ["tail", "-f", "/dev/null"],
            "Cmd": [],
            "HostConfig": self._base_host_config(),
        }
        try:
            container = await self._docker.containers.create(config=config, name=name)
            await container.start()
            logger.debug("warm_container_created", name=name, id=container.id[:12])
            return container
        except aiodocker.exceptions.DockerError as exc:
            raise ExecutionError(f"Failed to create warm container '{name}': {exc.message}") from exc

    async def _run_warm(self, code: str, servers_used: list[str]) -> str:
        """Execute code in a borrowed warm container via ``docker exec``.

        Code is base64-encoded and passed as the ``MCE_EXEC_CODE`` env var so
        the entrypoint can read it without an interactive stdin pipe.

        Args:
            code: Complete Python code (with sys.path injection) to execute.
            servers_used: Server names for credential injection.

        Returns:
            Raw stdout from the entrypoint (JSON string).

        Raises:
            ExecutionTimeoutError: If execution exceeds configured timeout.
            ExecutionError: On Docker exec failure.
        """
        assert self._warm_pool is not None

        env_vars = build_all_server_env_vars(servers_used, {n: self._auth_configs.get(n) for n in servers_used} or None)
        env_vars["MCE_EXEC_CODE"] = base64.b64encode(code.encode("utf-8")).decode("ascii")
        env_vars["MCE_EXEC_TIMEOUT"] = str(self._config.execution_timeout_seconds)

        borrow_timeout = float(self._config.execution_timeout_seconds + 10)

        async with self._warm_pool.borrow(timeout=borrow_timeout) as container:
            try:
                exec_obj = await container.exec(
                    cmd=["python", "/workspace/entrypoint.py"],
                    environment=env_vars,
                    stdout=True,
                    stderr=False,
                    stdin=False,
                    tty=False,
                )
            except aiodocker.exceptions.DockerError as exc:
                raise ExecutionError(f"docker exec create failed: {exc.message}") from exc

            output_parts: list[bytes] = []
            try:
                async with asyncio.timeout(float(self._config.execution_timeout_seconds)):
                    async with exec_obj.start(detach=False) as stream:
                        while True:
                            msg = await stream.read_out()
                            if msg is None:
                                break
                            # stream type 1 = stdout, 2 = stderr; capture stdout only
                            if msg.stream == 1:
                                output_parts.append(msg.data)
            except TimeoutError as exc:
                raise ExecutionTimeoutError(
                    f"Execution timed out after {self._config.execution_timeout_seconds}s",
                    exit_code=124,
                ) from exc
            except aiodocker.exceptions.DockerError as exc:
                raise ExecutionError(f"docker exec failed: {exc.message}") from exc

        raw = b"".join(output_parts).decode("utf-8", errors="replace")
        return raw[: self._config.max_output_size_bytes]

    # ------------------------------------------------------------------
    # Cold mode
    # ------------------------------------------------------------------

    async def _run_cold(self, code: str, servers_used: list[str]) -> str:
        """Create a fresh container per request, run code, then delete it.

        Args:
            code: Complete Python code (with sys.path injection) to execute.
            servers_used: Server names for credential injection.

        Returns:
            Raw stdout from the entrypoint (JSON string).

        Raises:
            ExecutionTimeoutError: If execution exceeds configured timeout.
            ExecutionError: On Docker API failure or non-zero exit code.
        """
        assert self._docker is not None

        env_vars = build_all_server_env_vars(servers_used, {n: self._auth_configs.get(n) for n in servers_used} or None)
        env_vars["MCE_EXEC_CODE"] = base64.b64encode(code.encode("utf-8")).decode("ascii")
        env_vars["MCE_EXEC_TIMEOUT"] = str(self._config.execution_timeout_seconds)

        container_name = f"mce-cold-{uuid.uuid4().hex[:12]}"
        config: dict[str, Any] = {
            "Image": self._config.docker_image,
            "Env": [f"{k}={v}" for k, v in env_vars.items()],
            "HostConfig": self._base_host_config(),
        }

        try:
            container = await self._docker.containers.create(config=config, name=container_name)
        except aiodocker.exceptions.DockerError as exc:
            _hint = (
                f" Run: docker build -t {self._config.docker_image} sandbox/"
                if "No such image" in str(exc.message)
                else ""
            )
            raise ExecutionError(f"Failed to create container: {exc.message}.{_hint}") from exc

        try:
            await container.start()

            try:
                async with asyncio.timeout(float(self._config.execution_timeout_seconds)):
                    result = await container.wait()
            except TimeoutError as exc:
                raise ExecutionTimeoutError(
                    f"Execution timed out after {self._config.execution_timeout_seconds}s",
                    exit_code=124,
                ) from exc

            exit_code: int = result.get("StatusCode", 1)
            if exit_code not in (0, 1):
                # exit 1 = user code error (handled by _parse_output); other codes = container failure
                stderr_lines = await container.log(stdout=False, stderr=True)
                stderr = "".join(stderr_lines)[:2048]
                raise ExecutionError(
                    f"Sandbox container exited with code {exit_code}",
                    stderr=stderr,
                    exit_code=exit_code,
                )

            stdout_lines = await container.log(stdout=True, stderr=False)
            raw = "".join(stdout_lines)
            return raw[: self._config.max_output_size_bytes]

        finally:
            try:
                await container.delete(force=True)
                logger.debug("cold_container_deleted", name=container_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("cold_container_delete_failed", name=container_name, error=str(exc))

    # ------------------------------------------------------------------
    # Output parsing
    # ------------------------------------------------------------------

    def _parse_output(self, raw_output: str, elapsed_ms: int) -> ExecutionResult:
        """Parse sandbox stdout into an ExecutionResult.

        Expects a single JSON line on stdout from ``entrypoint.py``.
        Falls back to returning raw text as ``data`` if JSON is absent.

        Args:
            raw_output: Raw stdout string from the container.
            elapsed_ms: Wall-clock execution time in milliseconds.

        Returns:
            Structured ExecutionResult.
        """
        raw_output = raw_output.strip()

        if not raw_output:
            return ExecutionResult(
                success=False,
                error="No output from execution",
                execution_time_ms=elapsed_ms,
            )

        try:
            parsed = json.loads(raw_output)
            success = bool(parsed.get("success", False))
            return ExecutionResult(
                success=success,
                data=parsed.get("data") if success else None,
                error=parsed.get("error") if not success else None,
                traceback=parsed.get("traceback") if self._config.debug else None,
                prints=parsed.get("prints"),
                execution_time_ms=elapsed_ms,
            )
        except (json.JSONDecodeError, KeyError):
            return ExecutionResult(
                success=True,
                data=raw_output[:4096],
                execution_time_ms=elapsed_ms,
            )

    # ------------------------------------------------------------------
    # Swagger hash helper
    # ------------------------------------------------------------------

    def _compute_swagger_hash(self, servers_used: list[str]) -> str:
        """Compute combined swagger hash for cache key disambiguation.

        Args:
            servers_used: List of server names.

        Returns:
            Combined SHA256 hash string, or ``"unknown"`` on failure.
        """
        try:
            from mce.runtime.registry import Registry  # noqa: PLC0415

            registry = Registry(self._config.compiled_output_dir)
            registry.load()
            hashes = [registry.get_swagger_hash(name) for name in servers_used if name]
            return combine_hashes(*hashes) if hashes else "no-servers"
        except Exception:  # noqa: BLE001
            return "unknown"
