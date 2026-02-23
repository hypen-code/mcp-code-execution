"""Compile phase orchestrator — coordinates swagger parsing and code generation."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from mfp.compiler.codegen import CodeGenerator
from mfp.compiler.swagger_parser import SwaggerParser
from mfp.config import MFPConfig
from mfp.errors import CompileError
from mfp.models import EndpointManifest, ServerManifest, ServerSpec, SwaggerSource
from mfp.utils.logging import get_logger

logger = get_logger(__name__)


class CompileResult:
    """Result summary from a compile pass."""

    def __init__(self) -> None:
        self.compiled: list[str] = []
        self.skipped: list[str] = []
        self.failed: list[str] = []
        self.total_endpoints: int = 0


class Orchestrator:
    """Manages the full compile pipeline for all configured swagger sources."""

    def __init__(self, config: MFPConfig) -> None:
        """Initialize orchestrator with project config.

        Args:
            config: MFP configuration instance.
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
        server_dir = self._output_dir / source.name
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
            f'"""Auto-generated MFP module for {spec.name}."""\n',
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
                    f"{p.name} ({p.param_type}, {'required' if p.required else 'optional'})"
                    for p in ep.parameters
                ),
                response_summary=", ".join(r.name for r in ep.response_schema) or "response data",
            )
            for ep in spec.endpoints
        ]

        manifest = ServerManifest(
            server_name=spec.name,
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
