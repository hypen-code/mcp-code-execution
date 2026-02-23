"""Unit tests for the swagger parser module."""

from __future__ import annotations

from pathlib import Path

import pytest

from mfp.compiler.swagger_parser import SwaggerParser
from mfp.errors import SwaggerFetchError
from mfp.models import ServerSpec, SwaggerSource

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
