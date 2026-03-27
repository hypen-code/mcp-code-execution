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
