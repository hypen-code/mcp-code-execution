"""Unit tests for mce/security/policies.py and mce/security/vault.py."""

from __future__ import annotations

import pytest

from mce.errors import SecurityViolationError
from mce.security.policies import check_domain_allowed, enforce_read_only
from mce.security.vault import build_all_server_env_vars, build_server_env_vars, resolve_env_references

# ---------------------------------------------------------------------------
# enforce_read_only
# ---------------------------------------------------------------------------


def test_enforce_read_only_get_is_allowed() -> None:
    """GET is not mutating — should not raise."""
    enforce_read_only("GET", "weather")  # must not raise


def test_enforce_read_only_post_raises() -> None:
    with pytest.raises(SecurityViolationError, match="read-only"):
        enforce_read_only("POST", "weather")


def test_enforce_read_only_put_raises() -> None:
    with pytest.raises(SecurityViolationError):
        enforce_read_only("PUT", "hotel")


def test_enforce_read_only_patch_raises() -> None:
    with pytest.raises(SecurityViolationError):
        enforce_read_only("PATCH", "hotel")


def test_enforce_read_only_delete_raises() -> None:
    with pytest.raises(SecurityViolationError):
        enforce_read_only("DELETE", "hotel")


def test_enforce_read_only_lowercase_post_raises() -> None:
    """Method string is case-normalised before checking."""
    with pytest.raises(SecurityViolationError):
        enforce_read_only("post", "weather")


def test_enforce_read_only_head_is_allowed() -> None:
    enforce_read_only("HEAD", "weather")  # must not raise


# ---------------------------------------------------------------------------
# check_domain_allowed
# ---------------------------------------------------------------------------


def test_check_domain_allowed_empty_list_permits_all() -> None:
    """Empty allowed_domains means no restriction."""
    check_domain_allowed("https://anything.example.com/v1", [])  # must not raise


def test_check_domain_allowed_matching_exact_domain() -> None:
    check_domain_allowed("https://api.weather.com/v1", ["api.weather.com"])  # must not raise


def test_check_domain_allowed_subdomain_accepted() -> None:
    """Subdomains of allowed domains should be permitted."""
    check_domain_allowed("https://sub.example.com/v1", ["example.com"])  # must not raise


def test_check_domain_allowed_blocked_domain_raises() -> None:
    with pytest.raises(SecurityViolationError, match="not in the allowed domains"):
        check_domain_allowed("https://evil.example.com/v1", ["safe.com"])


def test_check_domain_allowed_multiple_domains_one_matches() -> None:
    check_domain_allowed("https://api.example.com/v1", ["other.com", "example.com"])  # must not raise


def test_check_domain_allowed_multiple_domains_none_match_raises() -> None:
    with pytest.raises(SecurityViolationError):
        check_domain_allowed("https://bad.io/v1", ["safe.com", "good.org"])


# ---------------------------------------------------------------------------
# resolve_env_references
# ---------------------------------------------------------------------------


def test_resolve_env_references_no_placeholders() -> None:
    assert resolve_env_references("Bearer hardcoded-key") == "Bearer hardcoded-key"


def test_resolve_env_references_resolves_existing_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_SECRET_TOKEN", "supersecret")
    result = resolve_env_references("Bearer ${MY_SECRET_TOKEN}")
    assert result == "Bearer supersecret"


def test_resolve_env_references_unresolvable_left_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_VAR", raising=False)
    result = resolve_env_references("Bearer ${MISSING_VAR}")
    # Unresolvable placeholders stay unchanged
    assert "${MISSING_VAR}" in result


def test_resolve_env_references_multiple_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST", "api.example.com")
    monkeypatch.setenv("PORT", "8080")
    result = resolve_env_references("http://${HOST}:${PORT}/v1")
    assert result == "http://api.example.com:8080/v1"


def test_resolve_env_references_empty_string() -> None:
    assert resolve_env_references("") == ""


# ---------------------------------------------------------------------------
# build_server_env_vars
# ---------------------------------------------------------------------------


def test_build_server_env_vars_no_env_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCE_WEATHER_BASE_URL", raising=False)
    monkeypatch.delenv("MCE_WEATHER_AUTH", raising=False)
    result = build_server_env_vars("weather")
    assert result == {}


def test_build_server_env_vars_base_url_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCE_WEATHER_BASE_URL", "https://api.weather.example.com/v1")
    monkeypatch.delenv("MCE_WEATHER_AUTH", raising=False)
    result = build_server_env_vars("weather")
    assert result["MCE_WEATHER_BASE_URL"] == "https://api.weather.example.com/v1"
    assert "MCE_WEATHER_AUTH" not in result


def test_build_server_env_vars_auth_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCE_HOTEL_AUTH", "Bearer test-token")
    monkeypatch.delenv("MCE_HOTEL_BASE_URL", raising=False)
    result = build_server_env_vars("hotel")
    assert result["MCE_HOTEL_AUTH"] == "Bearer test-token"


def test_build_server_env_vars_auth_resolves_references(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_API_KEY", "resolved-key")
    monkeypatch.setenv("MCE_MYAPI_AUTH", "Bearer ${MY_API_KEY}")
    result = build_server_env_vars("myapi")
    assert result["MCE_MYAPI_AUTH"] == "Bearer resolved-key"


def test_build_server_env_vars_uppercase_server_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server name is upper-cased when building env var keys."""
    monkeypatch.setenv("MCE_PETSTORE_BASE_URL", "https://petstore.example.com")
    result = build_server_env_vars("petstore")
    assert "MCE_PETSTORE_BASE_URL" in result


# ---------------------------------------------------------------------------
# build_all_server_env_vars
# ---------------------------------------------------------------------------


def test_build_all_server_env_vars_empty_list() -> None:
    result = build_all_server_env_vars([])
    assert result == {}


def test_build_all_server_env_vars_combines_multiple(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCE_SVC1_BASE_URL", "https://svc1.example.com")
    monkeypatch.setenv("MCE_SVC2_BASE_URL", "https://svc2.example.com")
    result = build_all_server_env_vars(["svc1", "svc2"])
    assert "MCE_SVC1_BASE_URL" in result
    assert "MCE_SVC2_BASE_URL" in result


def test_build_all_server_env_vars_single_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCE_ONLY_BASE_URL", "https://only.example.com")
    result = build_all_server_env_vars(["only"])
    assert result["MCE_ONLY_BASE_URL"] == "https://only.example.com"
