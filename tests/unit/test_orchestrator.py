"""Unit tests for the compile Orchestrator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from mce.compiler.orchestrator import CompileResult, Orchestrator, _to_module_name
from mce.config import MCEConfig
from mce.errors import CompileError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, swagger_config: str | None = None) -> MCEConfig:
    return MCEConfig(
        compiled_output_dir=str(tmp_path / "compiled"),
        cache_db_path=str(tmp_path / "data" / "cache.db"),
        swagger_config_file=swagger_config or str(tmp_path / "swaggers.yaml"),
        log_level="DEBUG",
    )


FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _write_swagger_yaml(tmp_path: Path, sources: list[dict[str, Any]]) -> Path:
    config_path = tmp_path / "swaggers.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"servers": sources}, f)
    return config_path


# ---------------------------------------------------------------------------
# _to_module_name()
# ---------------------------------------------------------------------------


def test_to_module_name_simple() -> None:
    assert _to_module_name("weather") == "weather"


def test_to_module_name_spaces_and_hyphens() -> None:
    assert _to_module_name("Open-Meteo Weather API") == "open_meteo_weather_api"


def test_to_module_name_lowercases() -> None:
    assert _to_module_name("MyAPI") == "myapi"


def test_to_module_name_collapses_underscores() -> None:
    assert _to_module_name("foo--bar") == "foo_bar"


def test_to_module_name_leading_digit() -> None:
    assert _to_module_name("1api") == "m_1api"


# ---------------------------------------------------------------------------
# CompileResult
# ---------------------------------------------------------------------------


def test_compile_result_defaults() -> None:
    r = CompileResult()
    assert r.compiled == []
    assert r.skipped == []
    assert r.failed == []
    assert r.total_endpoints == 0


# ---------------------------------------------------------------------------
# load_swagger_sources()
# ---------------------------------------------------------------------------


def test_load_swagger_sources_no_config_file_returns_empty(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    sources = orchestrator.load_swagger_sources()
    assert sources == []


def test_load_swagger_sources_empty_yaml(tmp_path: Path) -> None:
    _write_swagger_yaml(tmp_path, [])
    config = _make_config(tmp_path, str(tmp_path / "swaggers.yaml"))
    orchestrator = Orchestrator(config)
    sources = orchestrator.load_swagger_sources()
    assert sources == []


def test_load_swagger_sources_valid_yaml(tmp_path: Path) -> None:
    servers = [
        {
            "name": "weather",
            "swagger_url": str(FIXTURES_DIR / "weather_api.yaml"),
            "base_url": "https://api.weather.example.com/v1",
            "is_read_only": True,
        }
    ]
    _write_swagger_yaml(tmp_path, servers)
    config = _make_config(tmp_path, str(tmp_path / "swaggers.yaml"))
    orchestrator = Orchestrator(config)
    sources = orchestrator.load_swagger_sources()
    assert len(sources) == 1
    assert sources[0].name == "weather"


def test_load_swagger_sources_invalid_yaml_raises_compile_error(tmp_path: Path) -> None:
    yaml_path = tmp_path / "swaggers.yaml"
    yaml_path.write_text("key: [unclosed", encoding="utf-8")
    config = _make_config(tmp_path, str(yaml_path))
    orchestrator = Orchestrator(config)
    with pytest.raises(CompileError):
        orchestrator.load_swagger_sources()


def test_load_swagger_sources_skips_invalid_server_entries(tmp_path: Path) -> None:
    """Invalid server dicts (missing required fields) are skipped with warnings."""
    servers = [
        {"name": "ok", "swagger_url": str(FIXTURES_DIR / "weather_api.yaml"), "base_url": "https://ok.com"},
        {"bad_key": "no_name"},  # missing required fields
    ]
    _write_swagger_yaml(tmp_path, servers)
    config = _make_config(tmp_path, str(tmp_path / "swaggers.yaml"))
    orchestrator = Orchestrator(config)
    sources = orchestrator.load_swagger_sources()
    # invalid entry is skipped
    assert len(sources) == 1
    assert sources[0].name == "ok"


# ---------------------------------------------------------------------------
# _is_up_to_date()
# ---------------------------------------------------------------------------


def test_is_up_to_date_returns_false_when_no_manifest(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    result = orchestrator._is_up_to_date(tmp_path / "nonexistent" / "manifest.json", "abc")
    assert result is False


def test_is_up_to_date_returns_true_when_hashes_match(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    manifest_path.write_text(
        json.dumps({"swagger_hash": "correct_hash", "template_hash": orchestrator._template_hash()}),
        encoding="utf-8",
    )
    assert orchestrator._is_up_to_date(manifest_path, "correct_hash") is True


def test_is_up_to_date_returns_false_when_hashes_differ(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"swagger_hash": "old_hash"}), encoding="utf-8")
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    assert orchestrator._is_up_to_date(manifest_path, "new_hash") is False


def test_is_up_to_date_returns_false_on_bad_json(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{not json}", encoding="utf-8")
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    assert orchestrator._is_up_to_date(manifest_path, "any") is False


# ---------------------------------------------------------------------------
# _write_functions() / _write_manifest()
# ---------------------------------------------------------------------------


def test_write_functions_creates_files(tmp_path: Path, sample_server_spec) -> None:  # type: ignore[no-untyped-def]
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    server_dir = tmp_path / "weather"
    server_dir.mkdir()
    orchestrator._write_functions(server_dir, sample_server_spec, "# generated code")
    assert (server_dir / "functions.py").read_text() == "# generated code"
    assert (server_dir / "__init__.py").exists()


def test_write_manifest_creates_manifest_json(tmp_path: Path, sample_server_spec) -> None:  # type: ignore[no-untyped-def]
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    server_dir = tmp_path / "weather"
    server_dir.mkdir()
    orchestrator._write_manifest(server_dir, sample_server_spec)
    manifest_path = server_dir / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["server_name"] == "weather"
    assert manifest["swagger_hash"] == sample_server_spec.swagger_hash


def test_write_manifest_contains_endpoints(tmp_path: Path, sample_server_spec) -> None:  # type: ignore[no-untyped-def]
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    server_dir = tmp_path / "weather"
    server_dir.mkdir()
    orchestrator._write_manifest(server_dir, sample_server_spec)
    manifest = json.loads((server_dir / "manifest.json").read_text())
    endpoints = manifest["endpoints"]
    assert len(endpoints) == 1
    assert endpoints[0]["function_name"] == "get_current_weather"


# ---------------------------------------------------------------------------
# _lint_all_generated_code()
# ---------------------------------------------------------------------------


def test_lint_all_generated_code_no_files(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    (tmp_path / "compiled").mkdir()
    # Should not raise even with no functions.py files
    orchestrator._lint_all_generated_code()


def test_lint_all_generated_code_ruff_not_found(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    compiled = tmp_path / "compiled" / "weather"
    compiled.mkdir(parents=True)
    (compiled / "functions.py").write_text("result = 1\n")
    with patch("subprocess.run", side_effect=FileNotFoundError("ruff not found")):
        orchestrator._lint_all_generated_code()  # must not raise


def test_lint_all_generated_code_lint_warnings(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    compiled = tmp_path / "compiled" / "weather"
    compiled.mkdir(parents=True)
    (compiled / "functions.py").write_text("result = 1\n")
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "W: warning"
    with patch("subprocess.run", return_value=mock_result):
        orchestrator._lint_all_generated_code()  # logs warning, does not raise


# ---------------------------------------------------------------------------
# compile_all() — with real fixture YAMLs
# ---------------------------------------------------------------------------


async def test_compile_all_no_sources(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    result = await orchestrator.compile_all()
    assert result.compiled == []
    assert result.failed == []


async def test_compile_all_dry_run(tmp_path: Path) -> None:
    servers = [
        {
            "name": "weather",
            "swagger_url": str(FIXTURES_DIR / "weather_api.yaml"),
            "base_url": "https://api.weather.example.com/v1",
        }
    ]
    _write_swagger_yaml(tmp_path, servers)
    config = _make_config(tmp_path, str(tmp_path / "swaggers.yaml"))
    orchestrator = Orchestrator(config)
    result = await orchestrator.compile_all(dry_run=True)
    assert "weather" in result.compiled
    assert result.total_endpoints > 0
    # No files written in dry-run mode
    assert not (tmp_path / "compiled" / "weather" / "functions.py").exists()


async def test_compile_all_writes_output_files(tmp_path: Path) -> None:
    servers = [
        {
            "name": "weather",
            "swagger_url": str(FIXTURES_DIR / "weather_api.yaml"),
            "base_url": "https://api.weather.example.com/v1",
        }
    ]
    _write_swagger_yaml(tmp_path, servers)
    config = _make_config(tmp_path, str(tmp_path / "swaggers.yaml"))
    orchestrator = Orchestrator(config)

    with patch("subprocess.run") as mock_ruff:
        mock_ruff.return_value = MagicMock(returncode=0, stdout="")
        result = await orchestrator.compile_all()

    assert "weather" in result.compiled
    assert (tmp_path / "compiled" / "weather" / "functions.py").exists()
    assert (tmp_path / "compiled" / "weather" / "manifest.json").exists()


async def test_compile_all_skips_up_to_date(tmp_path: Path) -> None:
    """Second compile of unchanged swagger should skip."""
    servers = [
        {
            "name": "weather",
            "swagger_url": str(FIXTURES_DIR / "weather_api.yaml"),
            "base_url": "https://api.weather.example.com/v1",
        }
    ]
    _write_swagger_yaml(tmp_path, servers)
    config = _make_config(tmp_path, str(tmp_path / "swaggers.yaml"))
    orchestrator = Orchestrator(config)

    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
        await orchestrator.compile_all()
        # Second run — manifest exists with same hash
        result = await orchestrator.compile_all()

    assert "weather" in result.skipped
    assert "weather" not in result.compiled


async def test_compile_all_uses_sanitized_directory_name(tmp_path: Path) -> None:
    """Server names with spaces/hyphens must produce valid Python module directories."""
    servers = [
        {
            "name": "My Weather API",
            "swagger_url": str(FIXTURES_DIR / "weather_api.yaml"),
            "base_url": "https://api.weather.example.com/v1",
        }
    ]
    _write_swagger_yaml(tmp_path, servers)
    config = _make_config(tmp_path, str(tmp_path / "swaggers.yaml"))
    orchestrator = Orchestrator(config)

    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
        result = await orchestrator.compile_all()

    assert "My Weather API" in result.compiled
    # Directory must be the sanitized module name, not the raw server name
    assert (tmp_path / "compiled" / "my_weather_api" / "functions.py").exists()
    assert not (tmp_path / "compiled" / "My Weather API").exists()


async def test_compile_all_records_failed_source(tmp_path: Path) -> None:
    servers = [
        {
            "name": "bad",
            "swagger_url": "/tmp/totally_nonexistent_123.yaml",
            "base_url": "https://bad.example.com",
        }
    ]
    _write_swagger_yaml(tmp_path, servers)
    config = _make_config(tmp_path, str(tmp_path / "swaggers.yaml"))
    orchestrator = Orchestrator(config)
    result = await orchestrator.compile_all()
    assert "bad" in result.failed


# ---------------------------------------------------------------------------
# _fetch_skills_content()
# ---------------------------------------------------------------------------


async def test_fetch_skills_content_local_file_success(tmp_path: Path) -> None:
    skills_file = tmp_path / "skills.md"
    skills_file.write_text("# Skills\nUse param X.", encoding="utf-8")
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    content = await orchestrator._fetch_skills_content(str(skills_file), "weather")
    assert content == "# Skills\nUse param X."


async def test_fetch_skills_content_local_file_not_found(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    content = await orchestrator._fetch_skills_content("/nonexistent/path/skills.md", "weather")
    assert content is None


async def test_fetch_skills_content_remote_success(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)

    mock_response = MagicMock()
    mock_response.text = "# Remote Skills"
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("mce.compiler.orchestrator.httpx.AsyncClient", return_value=mock_client):
        content = await orchestrator._fetch_skills_content("https://example.com/skills.md", "weather")

    assert content == "# Remote Skills"


async def test_fetch_skills_content_remote_failure(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("mce.compiler.orchestrator.httpx.AsyncClient", return_value=mock_client):
        content = await orchestrator._fetch_skills_content("https://example.com/skills.md", "weather")

    assert content is None


# ---------------------------------------------------------------------------
# _write_skills()
# ---------------------------------------------------------------------------


def test_write_skills_writes_file(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    server_dir = tmp_path / "weather"
    server_dir.mkdir()
    orchestrator._write_skills(server_dir, "# Skills content", "weather")
    assert (server_dir / "skills.md").read_text(encoding="utf-8") == "# Skills content"


def test_write_skills_noop_when_none(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    server_dir = tmp_path / "weather"
    server_dir.mkdir()
    orchestrator._write_skills(server_dir, None, "weather")
    assert not (server_dir / "skills.md").exists()


# ---------------------------------------------------------------------------
# _find_latest_server_dir — lines 384-390
# ---------------------------------------------------------------------------


def test_find_latest_server_dir_returns_none_when_compiled_dir_missing(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
    # compiled dir does not exist
    assert orchestrator._find_latest_server_dir() is None


def test_find_latest_server_dir_returns_none_when_no_manifests(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    compiled = tmp_path / "compiled"
    compiled.mkdir()
    orchestrator = Orchestrator(config)
    assert orchestrator._find_latest_server_dir() is None


def test_find_latest_server_dir_returns_dir_with_manifest(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    compiled = tmp_path / "compiled"
    server_dir = compiled / "weather"
    server_dir.mkdir(parents=True)
    (server_dir / "manifest.json").write_text("{}", encoding="utf-8")
    orchestrator = Orchestrator(config)
    result = orchestrator._find_latest_server_dir()
    assert result == server_dir


# ---------------------------------------------------------------------------
# _resolve_mce_command — lines 405-412
# ---------------------------------------------------------------------------


def test_resolve_mce_command_fallback_when_no_venv(tmp_path: Path) -> None:
    """Falls back to sys.executable bin dir when .venv is not found."""
    import sys  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    compiled = tmp_path / "compiled"
    result = Orchestrator._resolve_mce_command(compiled)
    expected_dir = str(Path(sys.executable).parent)
    assert result.startswith(expected_dir)


def test_resolve_mce_command_finds_venv_mce(tmp_path: Path) -> None:
    """Returns .venv/bin/mce when found within parent dirs."""
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    mce_bin = venv_bin / "mce"
    mce_bin.write_text("#!/bin/bash\n", encoding="utf-8")
    mce_bin.chmod(0o755)

    # compiled dir is nested under tmp_path
    compiled = tmp_path / "compiled"
    compiled.mkdir()
    result = Orchestrator._resolve_mce_command(compiled)
    assert result == str(mce_bin)


# ---------------------------------------------------------------------------
# _auth_env_hints — lines 431-464
# ---------------------------------------------------------------------------


def test_auth_env_hints_static() -> None:
    from mce.models import StaticAuthConfig  # noqa: PLC0415

    hints = Orchestrator._auth_env_hints("my-api", StaticAuthConfig(value="Bearer tok"))
    assert hints == {"MCE_MY_API_AUTH": "Bearer tok"}


def test_auth_env_hints_jwt() -> None:
    from mce.models import JwtAuthConfig  # noqa: PLC0415

    hints = Orchestrator._auth_env_hints("my-api", JwtAuthConfig(token="eyJhb"))
    assert hints == {"MCE_MY_API_AUTH": "Bearer eyJhb"}


def test_auth_env_hints_oauth2_with_ref() -> None:
    from mce.models import OAuth2AuthConfig  # noqa: PLC0415

    auth = OAuth2AuthConfig(token_url="https://auth.example.com/token", client_id="ci", client_secret="${MY_SECRET}")
    hints = Orchestrator._auth_env_hints("svc", auth)
    assert "MY_SECRET" in hints
    assert hints["MY_SECRET"] == "${MY_SECRET}"


def test_auth_env_hints_oauth2_literal_secret_returns_empty() -> None:
    from mce.models import OAuth2AuthConfig  # noqa: PLC0415

    auth = OAuth2AuthConfig(token_url="https://auth.example.com/token", client_id="ci", client_secret="literal-secret")
    hints = Orchestrator._auth_env_hints("svc", auth)
    assert hints == {}


def test_auth_env_hints_keycloak_with_ref() -> None:
    from mce.models import KeycloakAuthConfig  # noqa: PLC0415

    auth = KeycloakAuthConfig(
        base_url="https://kc.example.com/auth", realm="myrealm", client_id="ci", client_secret="${KC_SECRET}"
    )
    hints = Orchestrator._auth_env_hints("svc", auth)
    assert "KC_SECRET" in hints


def test_auth_env_hints_session_with_refs() -> None:
    from mce.models import SessionAuthConfig  # noqa: PLC0415

    auth = SessionAuthConfig(login_url="https://app.example.com/login", username="${APP_USER}", password="${APP_PASS}")
    hints = Orchestrator._auth_env_hints("svc", auth)
    assert "APP_USER" in hints
    assert "APP_PASS" in hints


def test_auth_env_hints_session_literal_returns_empty() -> None:
    from mce.models import SessionAuthConfig  # noqa: PLC0415

    auth = SessionAuthConfig(login_url="https://app.example.com/login", username="user", password="pass")
    hints = Orchestrator._auth_env_hints("svc", auth)
    assert hints == {}


def test_auth_env_hints_unknown_type_returns_empty() -> None:
    hints = Orchestrator._auth_env_hints("svc", object())
    assert hints == {}


# ---------------------------------------------------------------------------
# _generate_mcp_json — lines 510-516 (auth, extra_headers, server_url_vars)
# ---------------------------------------------------------------------------


async def test_generate_mcp_json_includes_extra_headers(tmp_path: Path) -> None:
    """Extra headers appear serialised as JSON in the env block."""
    servers = [
        {
            "name": "weather",
            "swagger_url": str(FIXTURES_DIR / "weather_api.yaml"),
            "base_url": "https://api.weather.example.com/v1",
            "extra_headers": {"kbn-version": "8.0.0"},
        }
    ]
    _write_swagger_yaml(tmp_path, servers)
    config = _make_config(tmp_path, str(tmp_path / "swaggers.yaml"))
    orchestrator = Orchestrator(config)
    result = await orchestrator.compile_all()
    assert result.mcp_json is not None
    mcp = json.loads(result.mcp_json)
    env = mcp["mcpServers"]["mcp-code-execution"]["env"]
    assert "MCE_WEATHER_EXTRA_HEADERS" in env


async def test_generate_mcp_json_includes_static_auth(tmp_path: Path) -> None:
    """Static auth value appears in the env block."""
    servers = [
        {
            "name": "weather",
            "swagger_url": str(FIXTURES_DIR / "weather_api.yaml"),
            "base_url": "https://api.weather.example.com/v1",
            "auth": {"type": "static", "value": "Bearer mytoken"},
        }
    ]
    _write_swagger_yaml(tmp_path, servers)
    config = _make_config(tmp_path, str(tmp_path / "swaggers.yaml"))
    orchestrator = Orchestrator(config)
    result = await orchestrator.compile_all()
    assert result.mcp_json is not None
    mcp = json.loads(result.mcp_json)
    env = mcp["mcpServers"]["mcp-code-execution"]["env"]
    assert env.get("MCE_WEATHER_AUTH") == "Bearer mytoken"


async def test_generate_mcp_json_includes_enable_additional_tools_false(tmp_path: Path) -> None:
    """MCE_ENABLE_ADDITIONAL_TOOLS is always emitted as 'false' by default."""
    servers = [
        {
            "name": "weather",
            "swagger_url": str(FIXTURES_DIR / "weather_api.yaml"),
            "base_url": "https://api.weather.example.com/v1",
        }
    ]
    _write_swagger_yaml(tmp_path, servers)
    config = _make_config(tmp_path, str(tmp_path / "swaggers.yaml"))
    result = await Orchestrator(config).compile_all()
    assert result.mcp_json is not None
    env = json.loads(result.mcp_json)["mcpServers"]["mcp-code-execution"]["env"]
    assert env["MCE_ENABLE_ADDITIONAL_TOOLS"] == "false"


# ---------------------------------------------------------------------------
# base_url propagation from spec (line 172)
# ---------------------------------------------------------------------------


async def test_compile_propagates_base_url_from_spec_when_not_set(tmp_path: Path) -> None:
    """When source.base_url is empty, the parsed spec's base_url is propagated."""
    servers = [
        {
            "name": "weather",
            "swagger_url": str(FIXTURES_DIR / "weather_api.yaml"),
            # no base_url — falls back to spec
        }
    ]
    _write_swagger_yaml(tmp_path, servers)
    config = _make_config(tmp_path, str(tmp_path / "swaggers.yaml"))
    result = await Orchestrator(config).compile_all()
    assert result.mcp_json is not None
    env = json.loads(result.mcp_json)["mcpServers"]["mcp-code-execution"]["env"]
    assert "MCE_WEATHER_BASE_URL" in env
    assert env["MCE_WEATHER_BASE_URL"]  # non-empty
