"""Swagger/OpenAPI parser — converts specs into normalized ServerSpec models."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from mfp.errors import CompileError, SwaggerFetchError
from mfp.models import EndpointSpec, ParamSchema, ResponseField, ServerSpec, SwaggerSource
from mfp.utils.hashing import hash_content
from mfp.utils.logging import get_logger

logger = get_logger(__name__)

# Maximum nesting depth before we skip a schema
_MAX_SCHEMA_DEPTH = 2

# Methods that mutate state
_MUTATING_METHODS = {"post", "put", "patch", "delete"}

# Unsupported discriminator keywords
_COMPLEX_KEYWORDS = {"oneOf", "anyOf", "allOf", "discriminator", "not"}


class SwaggerParser:
    """Parse OpenAPI 3.x / Swagger 2.0 documents into normalized ServerSpec models."""

    def __init__(self, source: SwaggerSource) -> None:
        """Initialize parser for the given swagger source.

        Args:
            source: Swagger source configuration.
        """
        self._source = source
        self._raw_doc: dict[str, Any] = {}
        self._components: dict[str, Any] = {}

    async def parse(self) -> ServerSpec:
        """Fetch and parse the swagger document.

        Returns:
            Normalized ServerSpec for this swagger source.

        Raises:
            SwaggerFetchError: When document cannot be loaded.
            CompileError: When document format is unsupported.
        """
        raw_content = await self._fetch_document()
        self._raw_doc = self._load_document(raw_content)
        self._components = self._raw_doc.get("components", {}).get("schemas", {})

        doc_hash = hash_content(raw_content)
        description = self._extract_description()
        endpoints = self._parse_paths()

        logger.info(
            "swagger_parsed",
            server=self._source.name,
            total_endpoints=len(endpoints),
            swagger_hash=doc_hash[:12],
        )

        return ServerSpec(
            name=self._source.name,
            description=description,
            base_url=self._source.base_url,
            is_read_only=self._source.is_read_only,
            endpoints=endpoints,
            swagger_hash=doc_hash,
        )

    async def _fetch_document(self) -> str:
        """Fetch swagger document from URL or local file path.

        Returns:
            Raw document content as string.

        Raises:
            SwaggerFetchError: When fetch fails.
        """
        url = self._source.swagger_url
        parsed = urlparse(url)

        if parsed.scheme in ("http", "https"):
            return await self._fetch_remote(url)
        return self._fetch_local(url)

    async def _fetch_remote(self, url: str) -> str:
        """Fetch swagger document from a remote URL.

        Args:
            url: HTTP/HTTPS URL.

        Returns:
            Response body as string.

        Raises:
            SwaggerFetchError: On network or HTTP errors.
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                return response.text
        except httpx.HTTPError as exc:
            raise SwaggerFetchError(f"Failed to fetch swagger from {url}: {exc}") from exc

    def _fetch_local(self, path: str) -> str:
        """Read swagger document from local filesystem.

        Args:
            path: Filesystem path (absolute or relative).

        Returns:
            File contents as string.

        Raises:
            SwaggerFetchError: When file cannot be read.
        """
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError as exc:
            raise SwaggerFetchError(f"Failed to read swagger file {path}: {exc}") from exc

    def _load_document(self, content: str) -> dict[str, Any]:
        """Parse YAML or JSON swagger document string.

        Args:
            content: Raw document content.

        Returns:
            Parsed document as a dict.

        Raises:
            CompileError: When document parsing fails.
        """
        try:
            doc = yaml.safe_load(content)
            if not isinstance(doc, dict):
                raise CompileError(f"Swagger document for {self._source.name} is not a mapping")
            return doc
        except yaml.YAMLError as exc:
            raise CompileError(f"Failed to parse swagger YAML/JSON for {self._source.name}: {exc}") from exc

    def _extract_description(self) -> str:
        """Extract server description from swagger info block.

        Returns:
            Description string, or server name if not present.
        """
        info = self._raw_doc.get("info", {})
        return str(info.get("description", info.get("title", self._source.name)))

    def _parse_paths(self) -> list[EndpointSpec]:
        """Parse all paths in the swagger document.

        Returns:
            List of normalized EndpointSpec objects.
        """
        paths: dict[str, Any] = self._raw_doc.get("paths", {})
        endpoints: list[EndpointSpec] = []
        skipped = 0

        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue

            path_level_params: list[Any] = path_item.get("parameters", [])

            for method, operation in path_item.items():
                if method.lower() not in ("get", "post", "put", "patch", "delete", "head", "options"):
                    continue
                if not isinstance(operation, dict):
                    continue

                try:
                    endpoint = self._parse_operation(path, method.upper(), operation, path_level_params)
                    if endpoint:
                        endpoints.append(endpoint)
                    else:
                        skipped += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "endpoint_skipped",
                        path=path,
                        method=method,
                        reason=str(exc),
                    )
                    skipped += 1

        if skipped:
            logger.info("endpoints_skipped", server=self._source.name, count=skipped)

        return endpoints

    def _parse_operation(
        self,
        path: str,
        method: str,
        operation: dict[str, Any],
        path_level_params: list[Any],
    ) -> EndpointSpec | None:
        """Parse a single API operation into an EndpointSpec.

        Args:
            path: URL path string.
            method: HTTP method (uppercase).
            operation: Operation object dict from swagger.
            path_level_params: Parameters defined at path level.

        Returns:
            EndpointSpec if parseable, None to skip.
        """
        # Skip read-only violations
        if self._source.is_read_only and method.lower() in _MUTATING_METHODS:
            logger.debug("skipped_readonly_method", path=path, method=method)
            return None

        operation_id = operation.get("operationId") or self._generate_operation_id(method, path)
        operation_id = self._sanitize_identifier(operation_id)
        summary = str(operation.get("summary", operation.get("description", f"{method} {path}")))
        description = str(operation.get("description", ""))

        all_params = list(path_level_params) + list(operation.get("parameters", []))
        parameters = self._parse_parameters(all_params)

        request_body_schema: dict[str, Any] | None = None
        if method in ("POST", "PUT", "PATCH"):
            request_body_schema = self._parse_request_body(operation.get("requestBody", {}))

        response_schema = self._parse_response_schema(operation.get("responses", {}))

        return EndpointSpec(
            path=path,
            method=method,
            operation_id=operation_id,
            summary=summary[:200],
            description=description[:1000],
            parameters=parameters,
            request_body_schema=request_body_schema,
            response_schema=response_schema,
            tags=operation.get("tags", []),
        )

    def _generate_operation_id(self, method: str, path: str) -> str:
        """Generate an operation ID from method and path.

        Args:
            method: HTTP method.
            path: URL path.

        Returns:
            Snake-case operation ID string.
        """
        sanitized = re.sub(r"[^a-zA-Z0-9_/]", "_", path)
        parts = [p for p in sanitized.split("/") if p and p != "_"]
        return f"{method.lower()}_{'_'.join(parts)}" or f"{method.lower()}_endpoint"

    def _sanitize_identifier(self, name: str) -> str:
        """Convert a string to a valid Python identifier.

        Args:
            name: Raw identifier string.

        Returns:
            Clean snake_case Python identifier.
        """
        name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
        name = re.sub(r"_+", "_", name).strip("_")
        if name and name[0].isdigit():
            name = f"fn_{name}"
        return name.lower() or "endpoint"

    def _parse_parameters(self, raw_params: list[Any]) -> list[ParamSchema]:
        """Parse parameter list from swagger params array.

        Args:
            raw_params: Raw parameter objects list.

        Returns:
            List of normalized ParamSchema objects.
        """
        params: list[ParamSchema] = []
        seen: set[str] = set()

        for raw in raw_params:
            if not isinstance(raw, dict):
                continue
            # Resolve $ref at top level
            if "$ref" in raw:
                raw = self._resolve_ref(raw["$ref"]) or {}  # noqa: PLW2901

            name = raw.get("name", "")
            if not name or name in seen:
                continue
            seen.add(name)

            location = raw.get("in", "query")
            schema = raw.get("schema", {})
            param_type = self._extract_type(schema)
            required = bool(raw.get("required", location == "path"))
            enum_values = schema.get("enum") if isinstance(schema.get("enum"), list) else None

            params.append(
                ParamSchema(
                    name=name,
                    location=location,
                    param_type=param_type,
                    required=required,
                    description=str(raw.get("description", "")),
                    default=str(schema.get("default")) if schema.get("default") is not None else None,
                    enum=[str(v) for v in enum_values] if enum_values else None,
                )
            )

        return params

    def _parse_request_body(self, body: dict[str, Any]) -> dict[str, Any] | None:
        """Extract simplified JSON schema from request body definition.

        Args:
            body: requestBody object from swagger.

        Returns:
            Simplified schema dict or None.
        """
        if not body:
            return None

        content = body.get("content", {})
        json_content = content.get("application/json", {})
        schema = json_content.get("schema")
        if not schema:
            return None

        if "$ref" in schema:
            resolved = self._resolve_ref(schema["$ref"])
            return resolved if resolved else None

        # Check complexity — skip if unsupported keywords present
        if any(k in schema for k in _COMPLEX_KEYWORDS):
            return None

        return schema

    def _parse_response_schema(self, responses: dict[str, Any]) -> list[ResponseField]:
        """Extract response fields from 200/201 response schema.

        Args:
            responses: Responses object from swagger operation.

        Returns:
            List of ResponseField objects.
        """
        for status_code in ("200", "201", "200-299"):
            if status_code in responses:
                resp = responses[status_code]
                if not isinstance(resp, dict):
                    continue
                return self._extract_response_fields(resp)

        return []

    def _extract_response_fields(self, response: dict[str, Any]) -> list[ResponseField]:
        """Extract field list from a single response object.

        Args:
            response: Single response object from swagger.

        Returns:
            List of ResponseField objects.
        """
        content = response.get("content", {})
        json_schema = content.get("application/json", {}).get("schema", {})

        if "$ref" in json_schema:
            resolved = self._resolve_ref(json_schema["$ref"])
            json_schema = resolved if resolved else {}

        if any(k in json_schema for k in _COMPLEX_KEYWORDS):
            return []

        return self._schema_to_fields(json_schema, depth=0)

    def _schema_to_fields(self, schema: dict[str, Any], depth: int) -> list[ResponseField]:
        """Recursively convert schema to ResponseField list.

        Args:
            schema: JSON schema dict.
            depth: Current recursion depth.

        Returns:
            List of ResponseField objects.
        """
        if depth > _MAX_SCHEMA_DEPTH or not isinstance(schema, dict):
            return []

        schema_type = schema.get("type", "object")
        fields: list[ResponseField] = []

        if schema_type == "object" or "properties" in schema:
            props: dict[str, Any] = schema.get("properties", {})
            for prop_name, prop_schema in props.items():
                if "$ref" in prop_schema:
                    resolved = self._resolve_ref(prop_schema["$ref"])
                    prop_schema = resolved if resolved else {}  # noqa: PLW2901

                field_type = self._extract_type(prop_schema)
                nested: list[ResponseField] | None = None

                if field_type == "object" and depth < _MAX_SCHEMA_DEPTH:
                    nested = self._schema_to_fields(prop_schema, depth + 1) or None

                fields.append(
                    ResponseField(
                        name=prop_name,
                        field_type=field_type,
                        description=str(prop_schema.get("description", "")),
                        nested=nested,
                    )
                )

        elif schema_type == "array":
            items = schema.get("items", {})
            if "$ref" in items:
                resolved = self._resolve_ref(items["$ref"])
                items = resolved if resolved else {}
            item_fields = self._schema_to_fields(items, depth + 1)
            if item_fields:
                fields.append(
                    ResponseField(
                        name="items",
                        field_type="array",
                        nested=item_fields if depth < _MAX_SCHEMA_DEPTH else None,
                    )
                )

        return fields

    def _extract_type(self, schema: dict[str, Any]) -> str:
        """Extract the primary type string from a schema dict.

        Args:
            schema: JSON schema object.

        Returns:
            Type string: string | integer | number | boolean | object | array.
        """
        if not isinstance(schema, dict):
            return "string"
        raw_type = schema.get("type", "string")
        if isinstance(raw_type, list):
            # Handle nullable types like ["string", "null"]
            non_null = [t for t in raw_type if t != "null"]
            return str(non_null[0]) if non_null else "string"
        return str(raw_type)

    def _resolve_ref(self, ref: str) -> dict[str, Any] | None:
        """Resolve a $ref pointer within local components/schemas.

        Args:
            ref: Reference string like #/components/schemas/Foo.

        Returns:
            Resolved schema dict or None if not found.
        """
        if not ref.startswith("#/"):
            return None  # External $ref not supported

        parts = ref.lstrip("#/").split("/")
        node: Any = self._raw_doc
        try:
            for part in parts:
                node = node[part]
            return dict(node) if isinstance(node, dict) else None
        except (KeyError, TypeError):
            return None
