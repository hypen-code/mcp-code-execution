"""Unit tests for the runtime Registry."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from mce.errors import FunctionNotFoundError, ServerNotFoundError
from mce.models import EndpointManifest, ServerManifest
from mce.runtime.registry import Registry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(
    tmp_path: Path,
    server_name: str = "weather",
    endpoints: list[dict[str, Any]] | None = None,
    swagger_hash: str = "abc123",
) -> Path:
    """Write a minimal manifest.json under tmp_path/<server_name>/manifest.json."""
    if endpoints is None:
        endpoints = [
            {
                "function_name": "get_current_weather",
                "summary": "Get current weather",
                "method": "GET",
                "path": "/weather/current",
                "parameters_summary": "city (string, required), units (string, optional)",
                "response_summary": "temperature, humidity, condition",
            }
        ]

    server_dir = tmp_path / server_name
    server_dir.mkdir(parents=True)
    manifest = ServerManifest(
        server_name=server_name,
        description=f"{server_name} API",
        swagger_hash=swagger_hash,
        compiled_at="2024-01-01T00:00:00+00:00",
        base_url=f"https://api.{server_name}.example.com",
        is_read_only=True,
        endpoints=[EndpointManifest(**ep) for ep in endpoints],
    )
    manifest_path = server_dir / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest.model_dump(), f)
    return manifest_path


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


def test_load_missing_dir_returns_empty(tmp_path: Path) -> None:
    """Registry.load() on a non-existent dir returns empty registry."""
    registry = Registry(str(tmp_path / "nonexistent"))
    registry.load()
    assert registry.list_servers() == []


def test_load_empty_dir_returns_empty(tmp_path: Path) -> None:
    """Registry.load() on an empty dir returns empty registry."""
    registry = Registry(str(tmp_path))
    registry.load()
    assert registry.list_servers() == []


def test_load_single_manifest(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="weather")
    registry = Registry(str(tmp_path))
    registry.load()
    servers = registry.list_servers()
    assert len(servers) == 1
    assert servers[0].name == "weather"


def test_load_multiple_manifests(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="weather")
    _make_manifest(tmp_path, server_name="hotel")
    registry = Registry(str(tmp_path))
    registry.load()
    names = {s.name for s in registry.list_servers()}
    assert names == {"weather", "hotel"}


def test_load_clears_previous_state(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="weather")
    registry = Registry(str(tmp_path))
    registry.load()
    assert len(registry.list_servers()) == 1
    # Remove manifest and reload
    import shutil  # noqa: PLC0415

    shutil.rmtree(tmp_path / "weather")
    registry.load()
    assert registry.list_servers() == []


def test_load_skips_invalid_manifest(tmp_path: Path) -> None:
    """A corrupted manifest.json should be skipped without crashing."""
    bad_dir = tmp_path / "bad_server"
    bad_dir.mkdir()
    (bad_dir / "manifest.json").write_text("not valid json", encoding="utf-8")
    registry = Registry(str(tmp_path))
    registry.load()  # must not raise
    assert registry.list_servers() == []


# ---------------------------------------------------------------------------
# list_servers()
# ---------------------------------------------------------------------------


def test_list_servers_returns_function_names(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="weather")
    registry = Registry(str(tmp_path))
    registry.load()
    server = registry.list_servers()[0]
    assert "get_current_weather" in server.functions


def test_list_servers_returns_function_summaries(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="weather")
    registry = Registry(str(tmp_path))
    registry.load()
    server = registry.list_servers()[0]
    assert server.function_summaries.get("get_current_weather") == "Get current weather"


# ---------------------------------------------------------------------------
# get_function()
# ---------------------------------------------------------------------------


def test_get_function_returns_function_info(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="weather")
    registry = Registry(str(tmp_path))
    registry.load()
    fn = registry.get_function("weather", "get_current_weather")
    assert fn.function_name == "get_current_weather"
    assert fn.method == "GET"
    assert fn.path == "/weather/current"


def test_get_function_parses_parameters(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="weather")
    registry = Registry(str(tmp_path))
    registry.load()
    fn = registry.get_function("weather", "get_current_weather")
    param_names = {p.name for p in fn.parameters}
    assert "city" in param_names
    assert "units" in param_names


def test_get_function_required_param(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="weather")
    registry = Registry(str(tmp_path))
    registry.load()
    fn = registry.get_function("weather", "get_current_weather")
    city_param = next(p for p in fn.parameters if p.name == "city")
    assert city_param.required is True


def test_get_function_optional_param(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="weather")
    registry = Registry(str(tmp_path))
    registry.load()
    fn = registry.get_function("weather", "get_current_weather")
    units_param = next(p for p in fn.parameters if p.name == "units")
    assert units_param.required is False


def test_get_function_response_fields(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="weather")
    registry = Registry(str(tmp_path))
    registry.load()
    fn = registry.get_function("weather", "get_current_weather")
    field_names = {r.name for r in fn.response_fields}
    assert "temperature" in field_names
    assert "humidity" in field_names
    assert "condition" in field_names


def test_get_function_server_not_found_raises(tmp_path: Path) -> None:
    registry = Registry(str(tmp_path))
    registry.load()
    with pytest.raises(ServerNotFoundError):
        registry.get_function("nonexistent", "some_fn")


def test_get_function_function_not_found_raises(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="weather")
    registry = Registry(str(tmp_path))
    registry.load()
    with pytest.raises(FunctionNotFoundError):
        registry.get_function("weather", "nonexistent_fn")


# ---------------------------------------------------------------------------
# get_function_source() and _get_function_source()
# ---------------------------------------------------------------------------


def test_get_function_source_missing_functions_file(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="weather")
    registry = Registry(str(tmp_path))
    registry.load()
    # No functions.py exists → fallback to comment
    fn = registry.get_function("weather", "get_current_weather")
    assert "get_current_weather" in fn.source_code or "Source not found" in fn.source_code


def test_get_function_source_from_functions_file(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="weather")
    # Write a functions.py
    functions_py = tmp_path / "weather" / "functions.py"
    functions_py.write_text(
        "def get_current_weather(city: str):\n    '''Get weather.'''\n    return {}\n",
        encoding="utf-8",
    )
    registry = Registry(str(tmp_path))
    registry.load()
    fn = registry.get_function("weather", "get_current_weather")
    assert "def get_current_weather" in fn.source_code


def test_get_function_source_caches_result(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="weather")
    functions_py = tmp_path / "weather" / "functions.py"
    functions_py.write_text("def get_current_weather():\n    return {}\n", encoding="utf-8")
    registry = Registry(str(tmp_path))
    registry.load()
    # First call populates cache
    fn1 = registry.get_function("weather", "get_current_weather")
    # Overwrite the file — cached result should still be returned
    functions_py.write_text("def other_fn():\n    pass\n", encoding="utf-8")
    fn2 = registry.get_function("weather", "get_current_weather")
    assert fn1.source_code == fn2.source_code


def test_get_function_source_public_method_server_not_found(tmp_path: Path) -> None:
    registry = Registry(str(tmp_path))
    registry.load()
    with pytest.raises(ServerNotFoundError):
        registry.get_function_source("ghost", "fn")


# ---------------------------------------------------------------------------
# get_swagger_hash()
# ---------------------------------------------------------------------------


def test_get_swagger_hash_returns_hash(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="weather", swagger_hash="deadbeef")
    registry = Registry(str(tmp_path))
    registry.load()
    assert registry.get_swagger_hash("weather") == "deadbeef"


def test_get_swagger_hash_server_not_found_raises(tmp_path: Path) -> None:
    registry = Registry(str(tmp_path))
    registry.load()
    with pytest.raises(ServerNotFoundError):
        registry.get_swagger_hash("ghost")


# ---------------------------------------------------------------------------
# _parse_parameters_summary
# ---------------------------------------------------------------------------


def test_parse_parameters_summary_empty(tmp_path: Path) -> None:
    _make_manifest(
        tmp_path,
        server_name="noparams",
        endpoints=[
            {
                "function_name": "no_params_fn",
                "summary": "No params",
                "method": "GET",
                "path": "/no-params",
                "parameters_summary": "",
                "response_summary": "",
            }
        ],
    )
    registry = Registry(str(tmp_path))
    registry.load()
    fn = registry.get_function("noparams", "no_params_fn")
    assert fn.parameters == []


def test_parse_parameters_summary_malformed_part(tmp_path: Path) -> None:
    """A malformed param part falls back to ParamSchema(name=part)."""
    _make_manifest(
        tmp_path,
        server_name="malformed",
        endpoints=[
            {
                "function_name": "fn",
                "summary": "s",
                "method": "GET",
                "path": "/p",
                "parameters_summary": "foo, bar(int",
                "response_summary": "",
            }
        ],
    )
    registry = Registry(str(tmp_path))
    registry.load()
    fn = registry.get_function("malformed", "fn")
    # Should not raise; params parsed as fallback
    assert len(fn.parameters) >= 1


# ---------------------------------------------------------------------------
# _parse_response_summary
# ---------------------------------------------------------------------------


def test_parse_response_summary_returns_empty_for_generic(tmp_path: Path) -> None:
    """The literal string 'response data' should yield an empty list."""
    _make_manifest(
        tmp_path,
        server_name="generic",
        endpoints=[
            {
                "function_name": "fn",
                "summary": "s",
                "method": "GET",
                "path": "/p",
                "parameters_summary": "",
                "response_summary": "response data",
            }
        ],
    )
    registry = Registry(str(tmp_path))
    registry.load()
    fn = registry.get_function("generic", "fn")
    assert fn.response_fields == []


def test_parse_response_summary_empty_string(tmp_path: Path) -> None:
    _make_manifest(
        tmp_path,
        server_name="empty_resp",
        endpoints=[
            {
                "function_name": "fn",
                "summary": "s",
                "method": "GET",
                "path": "/p",
                "parameters_summary": "",
                "response_summary": "",
            }
        ],
    )
    registry = Registry(str(tmp_path))
    registry.load()
    fn = registry.get_function("empty_resp", "fn")
    assert fn.response_fields == []


# ---------------------------------------------------------------------------
# _extract_function_snippet — syntax error fallback
# ---------------------------------------------------------------------------


def test_extract_function_snippet_syntax_error_returns_full_source(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="synerr")
    bad_code = "def broken(: pass"
    (tmp_path / "synerr" / "functions.py").write_text(bad_code, encoding="utf-8")
    registry = Registry(str(tmp_path))
    registry.load()
    fn = registry.get_function("synerr", "get_current_weather")
    # Falls back to full source on SyntaxError
    assert fn.source_code == bad_code


def test_extract_function_snippet_function_not_in_file_returns_full(tmp_path: Path) -> None:
    _make_manifest(tmp_path, server_name="partial")
    source = "def some_other_fn():\n    return 1\n"
    (tmp_path / "partial" / "functions.py").write_text(source, encoding="utf-8")
    registry = Registry(str(tmp_path))
    registry.load()
    fn = registry.get_function("partial", "get_current_weather")
    # Function not found in file → full source
    assert fn.source_code == source
