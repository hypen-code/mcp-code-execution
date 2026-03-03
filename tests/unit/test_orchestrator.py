"""Unit tests for the compile Orchestrator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from mce.compiler.orchestrator import CompileResult, Orchestrator
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
    manifest_path.write_text(json.dumps({"swagger_hash": "correct_hash"}), encoding="utf-8")
    config = _make_config(tmp_path)
    orchestrator = Orchestrator(config)
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
