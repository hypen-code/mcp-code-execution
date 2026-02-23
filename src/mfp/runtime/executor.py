"""Docker-based sandboxed code executor — the critical execution pipeline."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import docker
import docker.errors
from docker import DockerClient

from mfp.config import MFPConfig
from mfp.errors import ExecutionError, ExecutionTimeoutError, LintError, SecurityViolationError
from mfp.models import ExecutionResult
from mfp.runtime.cache import CacheStore
from mfp.security.ast_guard import ASTGuard
from mfp.security.vault import build_all_server_env_vars
from mfp.utils.hashing import combine_hashes, hash_code
from mfp.utils.logging import get_logger

logger = get_logger(__name__)

# Pattern to detect server function imports in user code
_SERVER_IMPORT_RE = re.compile(r"from\s+(\w+)\.functions\s+import|import\s+(\w+)\.functions")

# Maximum bytes of container output to read
_MAX_OUTPUT_BYTES = 1_048_576  # 1MB


def _detect_servers_used(code: str) -> list[str]:
    """Detect which server function modules are imported in the code.

    Args:
        code: Python source code.

    Returns:
        List of server names referenced by from <name>.functions import.
    """
    servers: set[str] = set()
    for match in _SERVER_IMPORT_RE.finditer(code):
        name = match.group(1) or match.group(2)
        if name:
            servers.add(name)
    return sorted(servers)


class CodeExecutor:
    """Manages the full execution pipeline: validate → lint → sandbox → cache."""

    def __init__(self, config: MFPConfig, cache: CacheStore) -> None:
        """Initialize the code executor.

        Args:
            config: MFP configuration.
            cache: Cache store for persisting successful executions.
        """
        self._config = config
        self._cache = cache
        self._ast_guard = ASTGuard()
        self._docker_client: DockerClient | None = None
        self._compiled_dir = Path(config.compiled_output_dir)

    def _get_docker_client(self) -> DockerClient:
        """Get or create the Docker client (lazy initialization).

        Returns:
            Connected Docker client.

        Raises:
            ExecutionError: If Docker is not available.
        """
        if self._docker_client is None:
            try:
                self._docker_client = docker.from_env()
                self._docker_client.ping()
            except docker.errors.DockerException as exc:
                raise ExecutionError(f"Docker is not available: {exc}") from exc
        return self._docker_client

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
        start_ms = int(time.time() * 1000)

        # Enforce code size limit
        if len(code.encode()) > self._config.max_code_size_bytes:
            raise SecurityViolationError(
                f"Code size {len(code.encode())} bytes exceeds limit of {self._config.max_code_size_bytes}"
            )

        # Security scan
        self._ast_guard.validate(code, context=description[:100])

        # Lint check
        self._lint_code(code)

        # Detect which servers are used for credential injection
        servers_used = _detect_servers_used(code)

        # Build complete execution code with compiled dir in sys.path
        execution_code = self._build_execution_code(code, servers_used)

        # Run in Docker sandbox
        raw_output = self._run_in_docker(execution_code, servers_used)

        elapsed_ms = int(time.time() * 1000) - start_ms

        # Parse output
        result = self._parse_output(raw_output, elapsed_ms)

        # Cache on success
        if result.success and self._config.cache_enabled:
            swagger_hash = self._compute_swagger_hash(servers_used)
            cache_id = await self._cache.store(code, description, servers_used, swagger_hash)
            result.cache_id = cache_id

        logger.info(
            "code_executed",
            success=result.success,
            elapsed_ms=elapsed_ms,
            servers=servers_used,
            description=description[:60],
        )

        return result

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
                    f"Code has lint issues",
                    lint_output=result.stdout[:2000],
                )
        except subprocess.TimeoutExpired:
            logger.warning("lint_timeout_skipped")
        except FileNotFoundError:
            logger.warning("ruff_not_found_skipped")

    def _build_execution_code(self, user_code: str, servers_used: list[str]) -> str:
        """Build complete execution payload with sys.path injection.

        Args:
            user_code: User-provided Python code.
            servers_used: Server names detected in imports.

        Returns:
            Complete Python code ready for execution in sandbox.
        """
        compiled_path = str(self._compiled_dir.resolve())
        path_injection = f"""
import sys as _sys
_sys.path.insert(0, {compiled_path!r})
"""
        return f"{path_injection}\n{user_code}"

    def _run_in_docker(self, code: str, servers_used: list[str]) -> str:
        """Execute code in an isolated Docker container.

        Args:
            code: Complete Python code to execute.
            servers_used: Server names for credential injection.

        Returns:
            Raw stdout output from the container.

        Raises:
            ExecutionTimeoutError: If execution exceeds configured timeout.
            ExecutionError: On Docker errors or non-zero exit code.
        """
        client = self._get_docker_client()
        env_vars = build_all_server_env_vars(servers_used)

        logger.debug("docker_execute_start", image=self._config.docker_image)
        container = None

        try:
            container = client.containers.run(  # type: ignore[union-attr]
                image=self._config.docker_image,
                command=None,
                stdin_open=True,
                detach=True,
                environment=env_vars,
                network_mode=self._config.network_mode,
                mem_limit="256m",
                memswap_limit="256m",
                cpu_period=100_000,
                cpu_quota=50_000,  # 50% of one CPU
                security_opt=["no-new-privileges:true"],
                read_only=True,
                tmpfs={"/tmp": "size=64m,mode=1777"},  # noqa: S108
                remove=False,  # We remove manually after output capture
                stdout=True,
                stderr=True,
            )

            # Send code via stdin
            sock = container.attach_socket(params={"stdin": 1, "stream": 1})
            sock._sock.sendall(code.encode("utf-8"))  # noqa: SLF001
            sock._sock.close()  # noqa: SLF001

            # Wait with timeout
            try:
                exit_result = container.wait(timeout=self._config.execution_timeout_seconds)
            except Exception as exc:  # noqa: BLE001
                container.kill()
                raise ExecutionTimeoutError(
                    f"Execution timed out after {self._config.execution_timeout_seconds}s",
                    exit_code=124,
                ) from exc

            exit_code = exit_result.get("StatusCode", 1)
            stdout_bytes = container.logs(stdout=True, stderr=False)
            stderr_bytes = container.logs(stdout=False, stderr=True)

            stdout = stdout_bytes.decode("utf-8", errors="replace")[: _MAX_OUTPUT_BYTES]
            stderr = stderr_bytes.decode("utf-8", errors="replace")[:4096]

            if exit_code != 0:
                raise ExecutionError(
                    f"Sandbox exited with code {exit_code}",
                    stderr=stderr,
                    exit_code=exit_code,
                )

            return stdout

        except (ExecutionTimeoutError, ExecutionError, SecurityViolationError):
            raise
        except docker.errors.ImageNotFound:
            raise ExecutionError(
                f"Docker image '{self._config.docker_image}' not found. Run: docker build -t {self._config.docker_image} sandbox/"
            )
        except docker.errors.DockerException as exc:
            raise ExecutionError(f"Docker execution failed: {exc}") from exc
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except docker.errors.DockerException:
                    pass
                logger.debug("docker_container_removed")

    def _parse_output(self, raw_output: str, elapsed_ms: int) -> ExecutionResult:
        """Parse Docker container stdout into an ExecutionResult.

        Expects JSON on stdout per sandbox protocol. Falls back to raw text.

        Args:
            raw_output: Raw stdout from container.
            elapsed_ms: Execution time in milliseconds.

        Returns:
            Structured ExecutionResult.
        """
        import json  # noqa: PLC0415

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
                execution_time_ms=elapsed_ms,
            )
        except (json.JSONDecodeError, KeyError):
            # Not JSON — return truncated raw text
            truncated = raw_output[:4096]
            return ExecutionResult(
                success=True,
                data=truncated,
                execution_time_ms=elapsed_ms,
            )

    def _compute_swagger_hash(self, servers_used: list[str]) -> str:
        """Compute combined swagger hash for servers used by code.

        Args:
            servers_used: List of server names.

        Returns:
            Combined hash string, or "unknown" if registry unavailable.
        """
        # Import here to avoid circular imports
        try:
            from mfp.runtime.registry import Registry  # noqa: PLC0415

            registry = Registry(self._config.compiled_output_dir)
            registry.load()
            hashes = [registry.get_swagger_hash(name) for name in servers_used if name]
            return combine_hashes(*hashes) if hashes else "no-servers"
        except Exception:  # noqa: BLE001
            return "unknown"
