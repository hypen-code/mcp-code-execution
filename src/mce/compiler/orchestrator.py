"""Compile phase orchestrator — coordinates swagger parsing and code generation."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from mce.compiler.codegen import CodeGenerator
from mce.compiler.swagger_parser import SwaggerParser
from mce.errors import CompileError
from mce.models import EndpointManifest, ServerManifest, ServerSpec, SwaggerSource
from mce.utils.logging import get_logger

if TYPE_CHECKING:
    from mce.config import MCEConfig

logger = get_logger(__name__)


def _to_module_name(name: str) -> str:
    """Convert a server name to a valid Python module identifier.

    Args:
        name: Human-readable server name (e.g. "Open-Meteo Weather API").

    Returns:
        Valid Python identifier usable as a module/directory name (e.g. "open_meteo_weather_api").
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_").lower()
    if sanitized and sanitized[0].isdigit():
        sanitized = f"m_{sanitized}"
    return sanitized or "module"


class CompileResult:
    """Result summary from a compile pass."""

    def __init__(self) -> None:
        self.compiled: list[str] = []
        self.skipped: list[str] = []
        self.failed: list[str] = []
        self.total_endpoints: int = 0
        self.mcp_json: str | None = None


class Orchestrator:
    """Manages the full compile pipeline for all configured swagger sources."""

    def __init__(self, config: MCEConfig) -> None:
        """Initialize orchestrator with project config.

        Args:
            config: MCE configuration instance.
        """
        self._config = config
        self._codegen = CodeGenerator()
        self._output_dir = Path(config.compiled_output_dir)

    def load_swagger_sources(self) -> list[SwaggerSource]:
        """Load swagger source configurations from YAML config file.

        Returns:
            List of SwaggerSource configurations.

        Raises:
            CompileError: When config file cannot be loaded or is invalid.
        """
        config_path = Path(self._config.swagger_config_file)
        if not config_path.exists():
            logger.warning("swagger_config_not_found", path=str(config_path))
            return []

        try:
            with open(config_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as exc:
            raise CompileError(f"Failed to load swagger config {config_path}: {exc}") from exc

        servers: list[dict[str, Any]] = raw.get("servers", []) if isinstance(raw, dict) else []
        sources: list[SwaggerSource] = []
        for srv in servers:
            try:
                sources.append(SwaggerSource(**srv))
            except Exception as exc:  # noqa: BLE001
                logger.warning("invalid_swagger_source", server=srv.get("name", "?"), error=str(exc))

        logger.info("swagger_sources_loaded", count=len(sources))
        return sources

    async def compile_all(self, dry_run: bool = False) -> CompileResult:
        """Run the compile pipeline for all configured swagger sources.

        Args:
            dry_run: If True, parse and validate but do not write output.

        Returns:
            CompileResult with summary of what happened.
        """
        sources = self.load_swagger_sources()
        result = CompileResult()

        if not sources:
            logger.warning("no_swagger_sources_configured")
            return result

        self._output_dir.mkdir(parents=True, exist_ok=True)

        for source in sources:
            try:
                compiled = await self._compile_source(source, dry_run=dry_run)
                if compiled:
                    result.compiled.append(source.name)
                    result.total_endpoints += compiled
                else:
                    result.skipped.append(source.name)
            except Exception as exc:  # noqa: BLE001
                logger.error("compile_failed", server=source.name, error=str(exc))
                result.failed.append(source.name)

        if not dry_run:
            self._lint_all_generated_code()
            result.mcp_json = self._generate_mcp_json(sources)

        logger.info(
            "compile_complete",
            compiled=len(result.compiled),
            skipped=len(result.skipped),
            failed=len(result.failed),
            total_endpoints=result.total_endpoints,
        )
        return result

    async def _compile_source(self, source: SwaggerSource, dry_run: bool) -> int:
        """Compile a single swagger source.

        Args:
            source: Swagger source configuration.
            dry_run: Skip writing files.

        Returns:
            Number of endpoints compiled, or 0 if skipped.

        Raises:
            CompileError: On parsing or generation failure.
        """
        module_name = _to_module_name(source.name)
        server_dir = self._output_dir / module_name
        manifest_path = server_dir / "manifest.json"

        # Parse the swagger document
        parser = SwaggerParser(source)
        spec = await parser.parse()

        # Check if recompile needed
        if not dry_run and self._is_up_to_date(manifest_path, spec.swagger_hash):
            logger.info("server_up_to_date", server=source.name)
            return 0

        if dry_run:
            logger.info("dry_run_parsed", server=source.name, endpoints=len(spec.endpoints))
            return len(spec.endpoints)

        # Generate code
        code = self._codegen.generate(spec)

        # Write output
        server_dir.mkdir(parents=True, exist_ok=True)
        self._write_functions(server_dir, spec, code)
        self._write_manifest(server_dir, spec)

        logger.info("server_compiled", server=source.name, endpoints=len(spec.endpoints))
        return len(spec.endpoints)

    def _is_up_to_date(self, manifest_path: Path, current_hash: str) -> bool:
        """Check if existing compiled output matches the current swagger hash.

        Args:
            manifest_path: Path to existing manifest.json.
            current_hash: Hash of current swagger document.

        Returns:
            True if the compiled output is current.
        """
        if not manifest_path.exists():
            return False
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
            return str(manifest.get("swagger_hash")) == current_hash
        except (OSError, json.JSONDecodeError, KeyError):
            return False

    def _write_functions(self, server_dir: Path, spec: ServerSpec, code: str) -> None:
        """Write generated functions.py to the server output directory.

        Args:
            server_dir: Output directory for this server.
            spec: Parsed spec (for __init__.py generation).
            code: Generated Python source code.
        """
        functions_path = server_dir / "functions.py"
        functions_path.write_text(code, encoding="utf-8")

        init_path = server_dir / "__init__.py"
        init_path.write_text(
            f'"""Auto-generated MCE module for {spec.name}."""\n',
            encoding="utf-8",
        )
        logger.debug("functions_written", path=str(functions_path))

    def _write_manifest(self, server_dir: Path, spec: ServerSpec) -> None:
        """Write manifest.json with metadata for server endpoints.

        Args:
            server_dir: Output directory for this server.
            spec: Parsed server spec.
        """
        endpoint_manifests = [
            EndpointManifest(
                function_name=ep.operation_id,
                summary=ep.summary,
                method=ep.method,
                path=ep.path,
                parameters_summary=", ".join(
                    f"{p.name} ({p.param_type}, {'required' if p.required else 'optional'})" for p in ep.parameters
                ),
                response_summary=", ".join(r.name for r in ep.response_schema) or "response data",
            )
            for ep in spec.endpoints
        ]

        manifest = ServerManifest(
            server_name=server_dir.name,
            description=spec.description,
            swagger_hash=spec.swagger_hash,
            compiled_at=datetime.now(tz=UTC).isoformat(),
            base_url=spec.base_url,
            is_read_only=spec.is_read_only,
            endpoints=endpoint_manifests,
        )

        manifest_path = server_dir / "manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest.model_dump(), f, indent=2)

        logger.debug("manifest_written", path=str(manifest_path))

    def _find_latest_server_dir(self) -> Path | None:
        """Return the compiled server directory with the most recently written manifest.

        Returns:
            Path to the latest server directory, or None if no servers are compiled.
        """
        if not self._output_dir.exists():
            return None
        server_dirs = [p for p in self._output_dir.iterdir() if p.is_dir()]
        candidates = [(d, (d / "manifest.json").stat().st_mtime) for d in server_dirs if (d / "manifest.json").exists()]
        if not candidates:
            return None
        return max(candidates, key=lambda x: x[1])[0]

    @staticmethod
    def _resolve_mce_command(compiled_output_dir: Path) -> str:
        """Resolve the absolute path to the `mce` executable for this project.

        Walks up from *compiled_output_dir* looking for a `.venv/bin/mce` binary.
        Falls back to the directory of the current Python interpreter if not found.

        Args:
            compiled_output_dir: Absolute path to the compiled output directory.

        Returns:
            Absolute path string to the `mce` executable.
        """
        candidate = compiled_output_dir.resolve()
        for _ in range(6):
            mce_path = candidate / ".venv" / "bin" / "mce"
            if mce_path.exists():
                return str(mce_path)
            candidate = candidate.parent
        # Fallback: same bin directory as the running Python interpreter
        return str(Path(sys.executable).parent / "mce")

    def _generate_mcp_json(self, sources: list[SwaggerSource]) -> str | None:
        """Generate an MCP JSON config snippet for the latest compiled server.

        The JSON follows the standard MCP client ``mcpServers`` shape and includes
        all environment variables required to run ``mce serve`` for the server.

        Args:
            sources: All swagger sources from the config file.

        Returns:
            Formatted JSON string, or None when no compiled server exists.
        """
        latest_dir = self._find_latest_server_dir()
        if latest_dir is None:
            return None

        mce_cmd = self._resolve_mce_command(self._output_dir)

        abs_compiled_dir = self._output_dir.resolve()
        abs_cache_db = Path(self._config.cache_db_path).resolve()

        env: dict[str, str] = {
            "MCE_COMPILED_OUTPUT_DIR": str(abs_compiled_dir),
            "MCE_DOCKER_IMAGE": self._config.docker_image,
            "MCE_NETWORK_MODE": self._config.network_mode,
            "MCE_CACHE_DB_PATH": str(abs_cache_db),
        }

        for src in sources:
            env_prefix = src.name.upper()
            env[f"MCE_{env_prefix}_BASE_URL"] = src.base_url
            if src.auth_header:
                env[f"MCE_{env_prefix}_AUTH"] = src.auth_header

        mcp_config = {
            "mcpServers": {
                "mcp-code-execution": {
                    "command": mce_cmd,
                    "args": ["serve"],
                    "env": env,
                }
            }
        }

        return json.dumps(mcp_config, indent=2)

    def _lint_all_generated_code(self) -> None:
        """Run ruff check on all generated Python files."""
        generated_files = list(self._output_dir.glob("**/functions.py"))
        if not generated_files:
            return

        try:
            result = subprocess.run(  # noqa: S603
                ["ruff", "check", "--quiet", *[str(f) for f in generated_files]],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning("generated_code_lint_warnings", output=result.stdout[:2000])
            else:
                logger.info("generated_code_lint_passed", files=len(generated_files))
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.warning("lint_skipped", reason=str(exc))
