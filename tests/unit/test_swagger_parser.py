"""Unit tests for the swagger parser module."""

from __future__ import annotations

from pathlib import Path

import pytest

from mce.compiler.swagger_parser import SwaggerParser
from mce.errors import SwaggerFetchError
from mce.models import ServerSpec, SwaggerSource

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def weather_source() -> SwaggerSource:
    return SwaggerSource(
        name="weather",
        swagger_url=str(FIXTURES_DIR / "weather_api.yaml"),
        base_url="https://api.weather.example.com/v1",
        is_read_only=True,
    )


@pytest.fixture
def hotel_source() -> SwaggerSource:
    return SwaggerSource(
        name="hotel",
        swagger_url=str(FIXTURES_DIR / "hotel_api.yaml"),
        base_url="https://api.hotel.example.com/v2",
        is_read_only=False,
    )


@pytest.fixture
def petstore_source() -> SwaggerSource:
    return SwaggerSource(
        name="petstore",
        swagger_url=str(FIXTURES_DIR / "petstore.yaml"),
        base_url="https://petstore.example.com/v1",
        is_read_only=False,
    )


async def test_parse_weather_api_returns_server_spec(weather_source: SwaggerSource) -> None:
    """Parser returns a valid ServerSpec for the weather fixture."""
    parser = SwaggerParser(weather_source)
    spec = await parser.parse()

    assert isinstance(spec, ServerSpec)
    assert spec.name == "weather"
    assert spec.is_read_only is True
    assert len(spec.swagger_hash) == 64  # SHA256 hex digest


async def test_weather_api_has_expected_endpoints(weather_source: SwaggerSource) -> None:
    """Weather fixture has the expected endpoint operation IDs."""
    parser = SwaggerParser(weather_source)
    spec = await parser.parse()

    op_ids = {ep.operation_id for ep in spec.endpoints}
    assert "get_current_weather" in op_ids
    assert "get_weather_forecast" in op_ids


async def test_weather_api_parameters_parsed(weather_source: SwaggerSource) -> None:
    """Required and optional parameters are correctly classified."""
    parser = SwaggerParser(weather_source)
    spec = await parser.parse()

    current = next(ep for ep in spec.endpoints if ep.operation_id == "get_current_weather")
    param_map = {p.name: p for p in current.parameters}

    assert "city" in param_map
    assert param_map["city"].required is True
    assert param_map["city"].param_type == "string"

    assert "units" in param_map
    assert param_map["units"].required is False
    assert param_map["units"].enum == ["metric", "imperial", "kelvin"]


async def test_readonly_server_excludes_mutating_methods(hotel_source: SwaggerSource) -> None:
    """Read-only server should exclude POST/PUT/DELETE endpoints."""
    read_only_source = SwaggerSource(
        name="hotel_ro",
        swagger_url=hotel_source.swagger_url,
        base_url=hotel_source.base_url,
        is_read_only=True,
    )
    parser = SwaggerParser(read_only_source)
    spec = await parser.parse()

    methods = {ep.method for ep in spec.endpoints}
    assert "POST" not in methods
    assert "DELETE" not in methods
    # GET should still be present
    assert "GET" in methods


async def test_petstore_resolves_dollar_refs(petstore_source: SwaggerSource) -> None:
    """Parser resolves $ref pointers in response schemas."""
    parser = SwaggerParser(petstore_source)
    spec = await parser.parse()

    list_ep = next(ep for ep in spec.endpoints if ep.operation_id == "list_pets")
    # The response schema should be populated (from resolved $ref)
    assert isinstance(list_ep.response_schema, list)


async def test_missing_file_raises_swagger_fetch_error() -> None:
    """Non-existent file path raises SwaggerFetchError."""
    source = SwaggerSource(
        name="bad",
        swagger_url="/tmp/nonexistent_swagger_12345.yaml",
        base_url="https://example.com",
    )
    parser = SwaggerParser(source)
    with pytest.raises(SwaggerFetchError):
        await parser.parse()


async def test_swagger_hash_is_consistent(weather_source: SwaggerSource) -> None:
    """Same file produces the same swagger hash across parses."""
    parser1 = SwaggerParser(weather_source)
    parser2 = SwaggerParser(weather_source)

    spec1 = await parser1.parse()
    spec2 = await parser2.parse()

    assert spec1.swagger_hash == spec2.swagger_hash


async def test_response_schema_fields_populated(weather_source: SwaggerSource) -> None:
    """Response schema fields are extracted from 200 response."""
    parser = SwaggerParser(weather_source)
    spec = await parser.parse()

    current_ep = next(ep for ep in spec.endpoints if ep.operation_id == "get_current_weather")
    field_names = {f.name for f in current_ep.response_schema}

    assert "temperature" in field_names
    assert "humidity" in field_names
    assert "condition" in field_names


def test_sanitize_identifier_camel_case() -> None:
    """camelCase operationIds are converted to snake_case, not flattened."""
    source = SwaggerSource(name="test", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)

    assert parser._sanitize_identifier("getHealth") == "get_health"
    assert parser._sanitize_identifier("searchDashboards") == "search_dashboards"
    assert parser._sanitize_identifier("createDashboard") == "create_dashboard"
    assert parser._sanitize_identifier("updateUserPreferences") == "update_user_preferences"


def test_sanitize_identifier_pascal_case() -> None:
    """PascalCase operationIds are converted to snake_case."""
    source = SwaggerSource(name="test", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)

    assert parser._sanitize_identifier("GetHealth") == "get_health"
    assert parser._sanitize_identifier("ListDashboards") == "list_dashboards"


def test_sanitize_identifier_already_snake_case() -> None:
    """Already-snake_case identifiers pass through unchanged."""
    source = SwaggerSource(name="test", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)

    assert parser._sanitize_identifier("get_current_weather") == "get_current_weather"
    assert parser._sanitize_identifier("list_pets") == "list_pets"


def test_sanitize_identifier_abbreviations() -> None:
    """Abbreviations like 'getHTTPStatus' → 'get_http_status'."""
    source = SwaggerSource(name="test", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)

    assert parser._sanitize_identifier("getHTTPStatus") == "get_http_status"


async def test_declared_path_param_parsed(petstore_source: SwaggerSource) -> None:
    """Path parameter declared in swagger parameters array is parsed correctly."""
    parser = SwaggerParser(petstore_source)
    spec = await parser.parse()

    ep = next(ep for ep in spec.endpoints if ep.operation_id == "get_pet_by_id")
    path_params = [p for p in ep.parameters if p.location == "path"]

    assert len(path_params) == 1
    assert path_params[0].name == "petId"
    assert path_params[0].required is True
    assert path_params[0].param_type == "integer"


async def test_undeclared_path_param_auto_detected() -> None:
    """Path param in URL template but missing from parameters array is auto-added."""
    import tempfile  # noqa: PLC0415

    yaml_content = """
openapi: "3.0.3"
info:
  title: Test API
  version: "1.0.0"
paths:
  /services/{serviceName}/agent:
    get:
      operationId: get_service_agent
      summary: Get agent for a service
      responses:
        "200":
          description: OK
"""
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(yaml_content)
        path = f.name

    source = SwaggerSource(name="test", swagger_url=path, base_url="https://example.com")
    parser = SwaggerParser(source)
    spec = await parser.parse()

    ep = next(ep for ep in spec.endpoints if ep.operation_id == "get_service_agent")
    path_params = [p for p in ep.parameters if p.location == "path"]

    assert len(path_params) == 1
    assert path_params[0].name == "serviceName"
    assert path_params[0].required is True
    assert path_params[0].param_type == "string"


async def test_undeclared_path_param_appears_in_generated_code() -> None:
    """Auto-detected path param appears in function signature and f-string URL."""
    import ast  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from mce.compiler.codegen import CodeGenerator  # noqa: PLC0415

    yaml_content = """
openapi: "3.0.3"
info:
  title: Test API
  version: "1.0.0"
paths:
  /internal/apm/services/{serviceName}/agent:
    get:
      operationId: get_service_agent
      summary: Get APM agent for a service
      responses:
        "200":
          description: OK
"""
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(yaml_content)
        path = f.name

    source = SwaggerSource(name="apm", swagger_url=path, base_url="https://example.com")
    parser = SwaggerParser(source)
    spec = await parser.parse()

    gen = CodeGenerator()
    code = gen.generate(spec)

    # Valid Python
    ast.parse(code)

    # Param in function signature
    assert "service_name: str" in code

    # f-string with substituted param in URL
    assert 'f"/internal/apm/services/{service_name}/agent"' in code


# ---------------------------------------------------------------------------
# _fetch_remote — lines 107-113
# ---------------------------------------------------------------------------


async def test_fetch_remote_success() -> None:
    """Remote HTTP swagger fetch returns response body."""
    from unittest.mock import AsyncMock, MagicMock, patch  # noqa: PLC0415

    source = SwaggerSource(
        name="remote", swagger_url="https://example.com/swagger.yaml", base_url="https://example.com"
    )
    parser = SwaggerParser(source)

    mock_response = MagicMock()
    mock_response.text = (
        "openapi: '3.0.0'\ninfo:\n  title: T\n  version: '1'\npaths: {}\nservers:\n  - url: https://example.com"
    )
    mock_response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("mce.compiler.swagger_parser.httpx.AsyncClient", return_value=mock_client):
        spec = await parser.parse()

    assert spec.name == "remote"


async def test_fetch_remote_http_error_raises_swagger_fetch_error() -> None:
    """httpx.HTTPError from remote fetch is wrapped in SwaggerFetchError."""
    from unittest.mock import AsyncMock, patch  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    source = SwaggerSource(name="fail", swagger_url="https://example.com/swagger.yaml", base_url="https://example.com")
    parser = SwaggerParser(source)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("mce.compiler.swagger_parser.httpx.AsyncClient", return_value=mock_client),
        pytest.raises(SwaggerFetchError),
    ):
        await parser.parse()


# ---------------------------------------------------------------------------
# _load_document — lines 147-151
# ---------------------------------------------------------------------------


async def test_load_document_non_mapping_raises_compile_error(tmp_path: Path) -> None:
    """YAML that parses to a list rather than a dict raises CompileError."""
    from mce.errors import CompileError  # noqa: PLC0415

    yaml_file = tmp_path / "bad.yaml"
    yaml_file.write_text("- item1\n- item2\n", encoding="utf-8")
    source = SwaggerSource(name="bad", swagger_url=str(yaml_file), base_url="https://example.com")
    parser = SwaggerParser(source)
    with pytest.raises(CompileError):
        await parser.parse()


async def test_load_document_invalid_yaml_raises_compile_error(tmp_path: Path) -> None:
    """Malformed YAML raises CompileError."""
    from mce.errors import CompileError  # noqa: PLC0415

    yaml_file = tmp_path / "bad.yaml"
    yaml_file.write_text("key: [unclosed\n", encoding="utf-8")
    source = SwaggerSource(name="bad", swagger_url=str(yaml_file), base_url="https://example.com")
    parser = SwaggerParser(source)
    with pytest.raises(CompileError):
        await parser.parse()


# ---------------------------------------------------------------------------
# _resolve_base_url — lines 224-274
# ---------------------------------------------------------------------------


async def test_resolve_base_url_uses_source_base_url_over_spec(tmp_path: Path) -> None:
    """base_url from SwaggerSource takes precedence over spec servers block."""
    yaml_file = tmp_path / "spec.yaml"
    yaml_file.write_text(
        "openapi: '3.0.0'\ninfo:\n  title: T\n  version: '1'\nservers:\n  - url: https://spec.example.com\npaths: {}\n",
        encoding="utf-8",
    )
    source = SwaggerSource(name="s", swagger_url=str(yaml_file), base_url="https://override.example.com")
    spec = await SwaggerParser(source).parse()
    assert spec.base_url == "https://override.example.com"


async def test_resolve_base_url_falls_back_to_spec_single_server(tmp_path: Path) -> None:
    """Without source base_url, servers[0].url from spec is used."""
    yaml_file = tmp_path / "spec.yaml"
    yaml_file.write_text(
        "openapi: '3.0.0'\ninfo:\n  title: T\n  version: '1'\n"
        "servers:\n  - url: https://spec.example.com/v1\npaths: {}\n",
        encoding="utf-8",
    )
    source = SwaggerSource(name="s", swagger_url=str(yaml_file), base_url="")
    spec = await SwaggerParser(source).parse()
    assert spec.base_url == "https://spec.example.com/v1"


async def test_resolve_base_url_multiple_servers_logs_and_uses_first(tmp_path: Path) -> None:
    """Multiple global servers: uses servers[0] and logs an info message."""
    yaml_file = tmp_path / "spec.yaml"
    yaml_file.write_text(
        "openapi: '3.0.0'\ninfo:\n  title: T\n  version: '1'\n"
        "servers:\n  - url: https://primary.example.com\n  - url: https://secondary.example.com\npaths: {}\n",
        encoding="utf-8",
    )
    source = SwaggerSource(name="s", swagger_url=str(yaml_file), base_url="")
    spec = await SwaggerParser(source).parse()
    assert spec.base_url == "https://primary.example.com"


async def test_resolve_base_url_no_servers_raises_compile_error(tmp_path: Path) -> None:
    """No servers block and no source base_url raises CompileError."""
    from mce.errors import CompileError  # noqa: PLC0415

    yaml_file = tmp_path / "spec.yaml"
    yaml_file.write_text(
        "openapi: '3.0.0'\ninfo:\n  title: T\n  version: '1'\npaths: {}\n",
        encoding="utf-8",
    )
    source = SwaggerSource(name="s", swagger_url=str(yaml_file), base_url="")
    with pytest.raises(CompileError, match="No base URL"):
        await SwaggerParser(source).parse()


async def test_resolve_base_url_non_dict_server_raises_compile_error(tmp_path: Path) -> None:
    """servers[0] that is not a dict raises CompileError."""
    from mce.errors import CompileError  # noqa: PLC0415

    yaml_file = tmp_path / "spec.yaml"
    yaml_file.write_text(
        "openapi: '3.0.0'\ninfo:\n  title: T\n  version: '1'\nservers:\n  - just_a_string\npaths: {}\n",
        encoding="utf-8",
    )
    source = SwaggerSource(name="s", swagger_url=str(yaml_file), base_url="")
    with pytest.raises(CompileError):
        await SwaggerParser(source).parse()


async def test_resolve_base_url_empty_url_raises_compile_error(tmp_path: Path) -> None:
    """servers[0].url is empty → CompileError."""
    from mce.errors import CompileError  # noqa: PLC0415

    yaml_file = tmp_path / "spec.yaml"
    yaml_file.write_text(
        "openapi: '3.0.0'\ninfo:\n  title: T\n  version: '1'\nservers:\n  - url: ''\npaths: {}\n",
        encoding="utf-8",
    )
    source = SwaggerSource(name="s", swagger_url=str(yaml_file), base_url="")
    with pytest.raises(CompileError):
        await SwaggerParser(source).parse()


async def test_resolve_base_url_relative_url_raises_compile_error(tmp_path: Path) -> None:
    """servers[0].url that is a relative path raises CompileError."""
    from mce.errors import CompileError  # noqa: PLC0415

    yaml_file = tmp_path / "spec.yaml"
    yaml_file.write_text(
        "openapi: '3.0.0'\ninfo:\n  title: T\n  version: '1'\nservers:\n  - url: /v1/api\npaths: {}\n",
        encoding="utf-8",
    )
    source = SwaggerSource(name="s", swagger_url=str(yaml_file), base_url="")
    with pytest.raises(CompileError):
        await SwaggerParser(source).parse()


# ---------------------------------------------------------------------------
# _collect_extra_server_url_vars — lines 173-202
# ---------------------------------------------------------------------------


async def test_collect_extra_server_url_vars_multiple_global_servers(tmp_path: Path) -> None:
    """Secondary global servers are collected as _BASE_URL_N variables."""
    yaml_file = tmp_path / "spec.yaml"
    yaml_file.write_text(
        "openapi: '3.0.0'\ninfo:\n  title: T\n  version: '1'\n"
        "servers:\n  - url: https://primary.example.com\n  - url: https://cdn.example.com\npaths: {}\n",
        encoding="utf-8",
    )
    source = SwaggerSource(name="s", swagger_url=str(yaml_file), base_url="https://primary.example.com")
    spec = await SwaggerParser(source).parse()
    assert "https://cdn.example.com" in spec.server_url_vars


async def test_collect_extra_server_url_vars_operation_level(tmp_path: Path) -> None:
    """Operation-level server overrides are collected as extra URL vars."""
    yaml_file = tmp_path / "spec.yaml"
    yaml_file.write_text(
        "openapi: '3.0.0'\ninfo:\n  title: T\n  version: '1'\n"
        "servers:\n  - url: https://primary.example.com\n"
        "paths:\n  /thing:\n    get:\n      operationId: get_thing\n      summary: Get thing\n"
        "      servers:\n        - url: https://cdn.example.com\n"
        "      responses:\n        '200':\n          description: OK\n",
        encoding="utf-8",
    )
    source = SwaggerSource(name="s", swagger_url=str(yaml_file), base_url="https://primary.example.com")
    spec = await SwaggerParser(source).parse()
    assert "https://cdn.example.com" in spec.server_url_vars


# ---------------------------------------------------------------------------
# _resolve_endpoint_base_url — lines 347-351
# ---------------------------------------------------------------------------


def test_resolve_endpoint_base_url_operation_level_wins() -> None:
    """Operation-level server takes priority over path-level server."""
    operation = {"servers": [{"url": "https://op.example.com/v2"}]}
    path_servers = [{"url": "https://path.example.com/v1"}]
    result = SwaggerParser._resolve_endpoint_base_url(operation, path_servers)
    assert result == "https://op.example.com/v2"


def test_resolve_endpoint_base_url_path_level_fallback() -> None:
    """Path-level server is used when no operation-level server."""
    operation: dict[str, object] = {}
    path_servers = [{"url": "https://path.example.com/v1"}]
    result = SwaggerParser._resolve_endpoint_base_url(operation, path_servers)
    assert result == "https://path.example.com/v1"


def test_resolve_endpoint_base_url_empty_when_neither_set() -> None:
    """Empty string returned when no server override at either level."""
    result = SwaggerParser._resolve_endpoint_base_url({}, [])
    assert result == ""


# ---------------------------------------------------------------------------
# _generate_operation_id — lines 431-433
# ---------------------------------------------------------------------------


def test_generate_operation_id_basic() -> None:
    """Generates snake_case operation ID from method and path."""
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    assert parser._generate_operation_id("GET", "/users/{id}") == "get_users__id_"


def test_generate_operation_id_empty_path_fallback() -> None:
    """Falls back to method_endpoint for trivially empty path parts."""
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    # Path with only slashes becomes empty parts list
    result = parser._generate_operation_id("POST", "/")
    assert result.startswith("post")


# ---------------------------------------------------------------------------
# _sanitize_identifier — digit prefix — line 453-454
# ---------------------------------------------------------------------------


def test_sanitize_identifier_digit_prefix_gets_fn_prefix() -> None:
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    assert parser._sanitize_identifier("123abc") == "fn_123abc"


# ---------------------------------------------------------------------------
# _parse_parameters — $ref resolution, duplicate skip — lines 471-478
# ---------------------------------------------------------------------------


async def test_parse_parameters_resolves_ref(tmp_path: Path) -> None:
    """$ref parameters are resolved from components/parameters."""
    yaml_file = tmp_path / "spec.yaml"
    yaml_file.write_text(
        "openapi: '3.0.0'\ninfo:\n  title: T\n  version: '1'\n"
        "servers:\n  - url: https://example.com\n"
        "components:\n  parameters:\n    LimitParam:\n      name: limit\n      in: query\n"
        "      schema:\n        type: integer\n"
        "paths:\n  /items:\n    get:\n      operationId: list_items\n      summary: List\n"
        "      parameters:\n        - $ref: '#/components/parameters/LimitParam'\n"
        "      responses:\n        '200':\n          description: OK\n",
        encoding="utf-8",
    )
    source = SwaggerSource(name="s", swagger_url=str(yaml_file), base_url="https://example.com")
    spec = await SwaggerParser(source).parse()
    ep = next(e for e in spec.endpoints if e.operation_id == "list_items")
    assert any(p.name == "limit" for p in ep.parameters)


async def test_parse_parameters_skips_duplicates(tmp_path: Path) -> None:
    """Duplicate parameter names in the same operation are deduplicated."""
    yaml_file = tmp_path / "spec.yaml"
    yaml_file.write_text(
        "openapi: '3.0.0'\ninfo:\n  title: T\n  version: '1'\n"
        "servers:\n  - url: https://example.com\n"
        "paths:\n  /items:\n    get:\n      operationId: list_items\n      summary: List\n"
        "      parameters:\n"
        "        - name: q\n          in: query\n          schema:\n            type: string\n"
        "        - name: q\n          in: query\n          schema:\n            type: string\n"
        "      responses:\n        '200':\n          description: OK\n",
        encoding="utf-8",
    )
    source = SwaggerSource(name="s", swagger_url=str(yaml_file), base_url="https://example.com")
    spec = await SwaggerParser(source).parse()
    ep = next(e for e in spec.endpoints if e.operation_id == "list_items")
    q_params = [p for p in ep.parameters if p.name == "q"]
    assert len(q_params) == 1


# ---------------------------------------------------------------------------
# _parse_request_body — lines 510-527
# ---------------------------------------------------------------------------


def test_parse_request_body_returns_none_for_empty() -> None:
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {}
    assert parser._parse_request_body({}) is None


def test_parse_request_body_returns_none_without_json_content() -> None:
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {}
    body = {"content": {"text/plain": {"schema": {"type": "string"}}}}
    assert parser._parse_request_body(body) is None


def test_parse_request_body_resolves_ref() -> None:
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {
        "components": {"schemas": {"Body": {"type": "object", "properties": {"name": {"type": "string"}}}}}
    }
    body = {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Body"}}}}
    result = parser._parse_request_body(body)
    assert result is not None
    assert result.get("type") == "object"


def test_parse_request_body_returns_none_for_complex_keywords() -> None:
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {}
    body = {"content": {"application/json": {"schema": {"oneOf": [{"type": "string"}]}}}}
    assert parser._parse_request_body(body) is None


def test_parse_request_body_returns_schema_dict() -> None:
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {}
    body = {"content": {"application/json": {"schema": {"type": "object", "properties": {"x": {"type": "string"}}}}}}
    result = parser._parse_request_body(body)
    assert result is not None
    assert result["type"] == "object"


# ---------------------------------------------------------------------------
# _parse_response_schema — 201 status, no match — lines 538-545
# ---------------------------------------------------------------------------


def test_parse_response_schema_201_status() -> None:
    """201 Created response is used when 200 is absent."""
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {}
    responses = {
        "201": {
            "content": {"application/json": {"schema": {"type": "object", "properties": {"id": {"type": "integer"}}}}}
        }
    }
    fields = parser._parse_response_schema(responses)
    assert any(f.name == "id" for f in fields)


def test_parse_response_schema_no_matching_status_returns_empty() -> None:
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {}
    assert parser._parse_response_schema({"404": {"description": "Not found"}}) == []


# ---------------------------------------------------------------------------
# _schema_to_fields — depth limit, array, $ref in prop — lines 568-627
# ---------------------------------------------------------------------------


def test_schema_to_fields_depth_exceeded_returns_empty() -> None:
    from mce.compiler.swagger_parser import _MAX_SCHEMA_DEPTH  # noqa: PLC0415

    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {}
    result = parser._schema_to_fields(
        {"type": "object", "properties": {"x": {"type": "string"}}}, depth=_MAX_SCHEMA_DEPTH + 1
    )
    assert result == []


def test_schema_to_fields_complex_keyword_returns_empty() -> None:
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {}
    result = parser._extract_response_fields({"content": {"application/json": {"schema": {"anyOf": []}}}})
    assert result == []


def test_schema_to_fields_array_type_with_items() -> None:
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {}
    schema = {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}}}}
    fields = parser._schema_to_fields(schema, depth=0)
    assert len(fields) == 1
    assert fields[0].name == "items"
    assert fields[0].field_type == "array"


def test_schema_to_fields_array_ref_in_items() -> None:
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {
        "components": {"schemas": {"Item": {"type": "object", "properties": {"id": {"type": "string"}}}}}
    }
    schema = {"type": "array", "items": {"$ref": "#/components/schemas/Item"}}
    fields = parser._schema_to_fields(schema, depth=0)
    assert len(fields) == 1
    assert fields[0].name == "items"


def test_schema_to_fields_ref_in_property() -> None:
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {
        "components": {"schemas": {"Addr": {"type": "object", "properties": {"street": {"type": "string"}}}}}
    }
    schema = {
        "type": "object",
        "properties": {"address": {"$ref": "#/components/schemas/Addr"}},
    }
    fields = parser._schema_to_fields(schema, depth=0)
    assert any(f.name == "address" for f in fields)


def test_schema_to_fields_nested_object_recursion() -> None:
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {}
    schema = {
        "type": "object",
        "properties": {
            "meta": {
                "type": "object",
                "properties": {"created": {"type": "string"}},
            }
        },
    }
    fields = parser._schema_to_fields(schema, depth=0)
    meta = next((f for f in fields if f.name == "meta"), None)
    assert meta is not None
    assert meta.nested is not None


# ---------------------------------------------------------------------------
# _extract_type — nullable list — lines 639-644
# ---------------------------------------------------------------------------


def test_extract_type_nullable_list_returns_non_null() -> None:
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {}
    assert parser._extract_type({"type": ["string", "null"]}) == "string"


def test_extract_type_all_null_falls_back_to_string() -> None:
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {}
    assert parser._extract_type({"type": ["null"]}) == "string"


def test_extract_type_non_dict_returns_string() -> None:
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {}
    assert parser._extract_type("not-a-dict") == "string"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _resolve_ref — lines 656-666
# ---------------------------------------------------------------------------


def test_resolve_ref_external_ref_returns_none() -> None:
    """Non-local $ref (no leading #/) returns None."""
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {}
    assert parser._resolve_ref("https://external.com/schema.json#/Foo") is None


def test_resolve_ref_missing_path_returns_none() -> None:
    """$ref pointing to a missing key returns None."""
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {"components": {"schemas": {}}}
    assert parser._resolve_ref("#/components/schemas/Missing") is None


def test_resolve_ref_valid_path_returns_schema() -> None:
    source = SwaggerSource(name="t", swagger_url="", base_url="https://example.com")
    parser = SwaggerParser(source)
    parser._raw_doc = {"components": {"schemas": {"Foo": {"type": "object"}}}}
    result = parser._resolve_ref("#/components/schemas/Foo")
    assert result == {"type": "object"}
