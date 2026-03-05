"""Docker-based sandboxed code executor — the critical execution pipeline."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from mce.errors import ExecutionError, ExecutionTimeoutError, LintError, SecurityViolationError
from mce.models import ExecutionResult
from mce.security.ast_guard import ASTGuard
from mce.security.vault import build_all_server_env_vars
from mce.utils.hashing import combine_hashes
from mce.utils.logging import get_logger

if TYPE_CHECKING:
    from mce.config import MCEConfig
    from mce.runtime.cache import CacheStore

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

    def __init__(self, config: MCEConfig, cache: CacheStore) -> None:
        """Initialize the code executor.

        Args:
            config: MCE configuration.
            cache: Cache store for persisting successful executions.
        """
        self._config = config
        self._cache = cache
        self._ast_guard = ASTGuard()
        self._compiled_dir = Path(config.compiled_output_dir)

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

        # Lint check (skipped by default; enable with MCE_LINT_ENABLED=true)
        if self._config.lint_enabled:
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
                    "Code has lint issues",
                    lint_output=result.stdout[:2000],
                )
        except subprocess.TimeoutExpired:
            logger.warning("lint_timeout_skipped")
        except FileNotFoundError:
            logger.warning("ruff_not_found_skipped")

    # Container-side mount point for the compiled server functions
    _CONTAINER_COMPILED_PATH = "/mce_compiled"

    def _build_execution_code(self, user_code: str, servers_used: list[str]) -> str:
        """Build complete execution payload with sys.path injection.

        Args:
            user_code: User-provided Python code.
            servers_used: Server names detected in imports.

        Returns:
            Complete Python code ready for execution in sandbox.
        """
        path_injection = f"""
import sys as _sys
_sys.path.insert(0, {self._CONTAINER_COMPILED_PATH!r})
"""
        return f"{path_injection}\n{user_code}"

    def _run_in_docker(self, code: str, servers_used: list[str]) -> str:
        """Execute code in an isolated Docker container via CLI stdin pipe.

        Args:
            code: Complete Python code to execute.
            servers_used: Server names for credential injection.

        Returns:
            Raw stdout output from the container.

        Raises:
            ExecutionTimeoutError: If execution exceeds configured timeout.
            ExecutionError: On Docker errors or non-zero exit code.
        """
        env_vars = build_all_server_env_vars(servers_used)
        logger.debug("docker_execute_start", image=self._config.docker_image)

        cmd = ["docker"]
        if self._config.docker_host:
            cmd += ["-H", self._config.docker_host]

        compiled_host_path = str(self._compiled_dir.resolve())
        cmd += [
            "run",
            "--rm",
            "-i",
            "--network",
            self._config.network_mode,
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            "--cpu-period",
            "100000",
            "--cpu-quota",
            "50000",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--tmpfs",
            "/tmp:size=64m,mode=1777",  # noqa: S108
            "-v",
            f"{compiled_host_path}:{self._CONTAINER_COMPILED_PATH}:ro",
        ]
        for key, val in env_vars.items():
            cmd += ["-e", f"{key}={val}"]
        cmd.append(self._config.docker_image)

        try:
            result = subprocess.run(  # noqa: S603
                cmd,
                input=code.encode("utf-8"),
                capture_output=True,
                timeout=self._config.execution_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecutionTimeoutError(
                f"Execution timed out after {self._config.execution_timeout_seconds}s",
                exit_code=124,
            ) from exc
        except FileNotFoundError as exc:
            raise ExecutionError("docker CLI not found in PATH") from exc

        stdout = result.stdout.decode("utf-8", errors="replace")[:_MAX_OUTPUT_BYTES]
        stderr = result.stderr.decode("utf-8", errors="replace")[:4096]

        if result.returncode != 0:
            if "Unable to find image" in stderr or "No such image" in stderr:
                raise ExecutionError(
                    f"Docker image '{self._config.docker_image}' not found. "
                    f"Run: docker build -t {self._config.docker_image} sandbox/"
                )
            raise ExecutionError(
                f"Sandbox exited with code {result.returncode}",
                stderr=stderr,
                exit_code=result.returncode,
            )

        return stdout

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
                prints=parsed.get("prints"),
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
            from mce.runtime.registry import Registry  # noqa: PLC0415

            registry = Registry(self._config.compiled_output_dir)
            registry.load()
            hashes = [registry.get_swagger_hash(name) for name in servers_used if name]
            return combine_hashes(*hashes) if hashes else "no-servers"
        except Exception:  # noqa: BLE001
            return "unknown"
