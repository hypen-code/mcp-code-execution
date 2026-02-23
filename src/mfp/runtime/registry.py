"""Runtime registry — loads compiled manifests and provides fast function lookup."""

from __future__ import annotations

import json
from pathlib import Path

from mfp.errors import FunctionNotFoundError, ServerNotFoundError
from mfp.models import FunctionInfo, ParamSchema, ResponseField, ServerInfo, ServerManifest
from mfp.utils.logging import get_logger

logger = get_logger(__name__)


class Registry:
    """Loads and indexes compiled server manifests for fast LLM tool lookups."""

    def __init__(self, compiled_dir: str) -> None:
        """Initialize the registry from the compiled output directory.

        Args:
            compiled_dir: Path to directory containing compiled server subdirectories.
        """
        self._compiled_dir = Path(compiled_dir)
        self._servers: dict[str, ServerManifest] = {}
        self._function_source_cache: dict[str, str] = {}

    def load(self) -> None:
        """Load all compiled server manifests from the compiled directory.

        Scans compiled_dir for subdirectories with manifest.json files
        and loads each into memory for fast lookup.
        """
        self._servers.clear()
        self._function_source_cache.clear()

        if not self._compiled_dir.exists():
            logger.warning("compiled_dir_not_found", path=str(self._compiled_dir))
            return

        manifest_files = list(self._compiled_dir.glob("*/manifest.json"))
        for manifest_path in manifest_files:
            try:
                self._load_manifest(manifest_path)
            except Exception as exc:  # noqa: BLE001
                logger.error("manifest_load_failed", path=str(manifest_path), error=str(exc))

        logger.info(
            "registry_loaded",
            servers=len(self._servers),
            total_functions=sum(len(s.endpoints) for s in self._servers.values()),
        )

    def _load_manifest(self, manifest_path: Path) -> None:
        """Load and index a single server manifest file.

        Args:
            manifest_path: Path to manifest.json file.
        """
        with open(manifest_path, encoding="utf-8") as f:
            raw = json.load(f)

        manifest = ServerManifest(**raw)
        self._servers[manifest.server_name] = manifest
        logger.debug("manifest_loaded", server=manifest.server_name, endpoints=len(manifest.endpoints))

    def list_servers(self) -> list[ServerInfo]:
        """Return summary information about all compiled servers.

        Returns:
            List of ServerInfo objects with function name/summary lists.
        """
        result: list[ServerInfo] = []
        for name, manifest in self._servers.items():
            result.append(
                ServerInfo(
                    name=name,
                    description=manifest.description,
                    functions=[ep.function_name for ep in manifest.endpoints],
                    function_summaries={ep.function_name: ep.summary for ep in manifest.endpoints},
                )
            )
        return result

    def get_function(self, server_name: str, function_name: str) -> FunctionInfo:
        """Get detailed function information by server and function name.

        Args:
            server_name: Name of the server.
            function_name: Name of the function.

        Returns:
            FunctionInfo with full parameter and response schema.

        Raises:
            ServerNotFoundError: If server doesn't exist in registry.
            FunctionNotFoundError: If function doesn't exist in server.
        """
        manifest = self._get_server_manifest(server_name)
        endpoint = self._find_endpoint(manifest, server_name, function_name)

        source_code = self._get_function_source(server_name, function_name)
        parameters = self._parse_parameters_summary(endpoint.parameters_summary)
        response_fields = self._parse_response_summary(endpoint.response_summary)

        return FunctionInfo(
            server_name=server_name,
            function_name=function_name,
            summary=endpoint.summary,
            parameters=parameters,
            response_fields=response_fields,
            source_code=source_code,
            method=endpoint.method,
            path=endpoint.path,
        )

    def get_function_source(self, server_name: str, function_name: str) -> str:
        """Get the Python source code for a specific function.

        Args:
            server_name: Server name.
            function_name: Function name.

        Returns:
            Python source code string for the function.

        Raises:
            ServerNotFoundError: If server not found.
            FunctionNotFoundError: If function not found.
        """
        self._get_server_manifest(server_name)  # Validate server exists
        return self._get_function_source(server_name, function_name)

    def get_swagger_hash(self, server_name: str) -> str:
        """Get the swagger hash for a compiled server.

        Args:
            server_name: Server name.

        Returns:
            Swagger hash string.

        Raises:
            ServerNotFoundError: If server not found.
        """
        manifest = self._get_server_manifest(server_name)
        return manifest.swagger_hash

    def _get_server_manifest(self, server_name: str) -> ServerManifest:
        """Look up a server manifest by name.

        Args:
            server_name: Server name to look up.

        Returns:
            ServerManifest.

        Raises:
            ServerNotFoundError: If server is not compiled.
        """
        if server_name not in self._servers:
            available = list(self._servers.keys())
            raise ServerNotFoundError(
                f"Server '{server_name}' not found. Available: {available}"
            )
        return self._servers[server_name]

    def _find_endpoint(self, manifest: ServerManifest, server_name: str, function_name: str) -> "EndpointManifestRef":  # noqa: F821
        """Find an endpoint entry in a manifest.

        Args:
            manifest: Server manifest to search.
            server_name: Name for error context.
            function_name: Function name to find.

        Returns:
            Endpoint manifest entry.

        Raises:
            FunctionNotFoundError: If function not in manifest.
        """
        from mfp.models import EndpointManifest  # noqa: PLC0415

        for ep in manifest.endpoints:
            if ep.function_name == function_name:
                return ep  # type: ignore[return-value]

        available = [ep.function_name for ep in manifest.endpoints]
        raise FunctionNotFoundError(
            f"Function '{function_name}' not found in server '{server_name}'. Available: {available}"
        )

    def _get_function_source(self, server_name: str, function_name: str) -> str:
        """Extract the Python source for a function from functions.py.

        Args:
            server_name: Server name.
            function_name: Function name.

        Returns:
            Source code of the specific function, or full file if extraction fails.
        """
        cache_key = f"{server_name}.{function_name}"
        if cache_key in self._function_source_cache:
            return self._function_source_cache[cache_key]

        functions_file = self._compiled_dir / server_name / "functions.py"
        if not functions_file.exists():
            return f"# Source not found for {server_name}.{function_name}"

        full_source = functions_file.read_text(encoding="utf-8")
        snippet = self._extract_function_snippet(full_source, function_name)
        self._function_source_cache[cache_key] = snippet
        return snippet

    def _extract_function_snippet(self, source: str, function_name: str) -> str:
        """Extract a single function definition from a Python source file.

        Args:
            source: Full Python source file content.
            function_name: Name of function to extract.

        Returns:
            Extracted function source, or full source on failure.
        """
        import ast  # noqa: PLC0415

        try:
            tree = ast.parse(source)
        except SyntaxError:
            return source

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == function_name:
                lines = source.splitlines()
                start = node.lineno - 1
                end = node.end_lineno or (start + 1)
                return "\n".join(lines[start:end])

        return source  # Fallback to full source

    def _parse_parameters_summary(self, summary: str) -> list[ParamSchema]:
        """Parse the human-readable parameters_summary string into ParamSchema objects.

        Args:
            summary: e.g. "city (string, required), date (string, optional)"

        Returns:
            List of ParamSchema objects.
        """
        if not summary.strip():
            return []

        params: list[ParamSchema] = []
        for part in summary.split(","):
            part = part.strip()  # noqa: PLW2901
            if not part:
                continue
            try:
                name, rest = part.split("(", 1)
                type_str, req_str = rest.rstrip(")").split(",")
                params.append(
                    ParamSchema(
                        name=name.strip(),
                        location="query",
                        param_type=type_str.strip(),
                        required="required" in req_str.lower(),
                    )
                )
            except ValueError:
                params.append(ParamSchema(name=part, location="query", param_type="string"))

        return params

    def _parse_response_summary(self, summary: str) -> list[ResponseField]:
        """Parse response_summary string into ResponseField list.

        Args:
            summary: e.g. "id, name, price"

        Returns:
            List of ResponseField objects.
        """
        if not summary or summary == "response data":
            return []
        return [ResponseField(name=field.strip(), field_type="string") for field in summary.split(",") if field.strip()]
