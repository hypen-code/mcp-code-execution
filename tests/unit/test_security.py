"""Unit tests for mce/security/policies.py and mce/security/vault.py."""

from __future__ import annotations

import time

import pytest
import respx
from httpx import Response

from mce.errors import SecurityViolationError
from mce.models import JwtAuthConfig, KeycloakAuthConfig, OAuth2AuthConfig, SessionAuthConfig, StaticAuthConfig
from mce.security.policies import check_domain_allowed, enforce_read_only
from mce.security.vault import (
    _SESSION_CACHE,
    _TOKEN_CACHE,
    AuthResult,
    build_all_server_env_vars,
    build_server_env_vars,
    resolve_auth_config,
    resolve_auth_env_vars,
    resolve_env_references,
)

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


# ---------------------------------------------------------------------------
# resolve_auth_config — static / jwt
# ---------------------------------------------------------------------------


def test_resolve_auth_config_static_passthrough() -> None:
    auth = StaticAuthConfig(value="Bearer hardcoded")
    assert resolve_auth_config("svc", auth) == "Bearer hardcoded"


def test_resolve_auth_config_static_resolves_env_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "resolved")
    auth = StaticAuthConfig(value="Bearer ${MY_TOKEN}")
    assert resolve_auth_config("svc", auth) == "Bearer resolved"


def test_resolve_auth_config_jwt_prefixes_bearer() -> None:
    auth = JwtAuthConfig(token="a.b.c")
    assert resolve_auth_config("svc", auth) == "Bearer a.b.c"


def test_resolve_auth_config_jwt_resolves_env_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_JWT", "x.y.z")
    auth = JwtAuthConfig(token="${MY_JWT}")
    assert resolve_auth_config("svc", auth) == "Bearer x.y.z"


# ---------------------------------------------------------------------------
# resolve_auth_config — OAuth2 token fetch (mocked)
# ---------------------------------------------------------------------------

_TOKEN_ENDPOINT = "https://auth.example.com/token"
_MOCK_TOKEN_RESPONSE = {"access_token": "mocked-token", "expires_in": 3600, "token_type": "Bearer"}


@respx.mock
def test_resolve_auth_config_oauth2_fetches_token() -> None:
    _TOKEN_CACHE.pop("oauth_svc", None)
    respx.post(_TOKEN_ENDPOINT).mock(return_value=Response(200, json=_MOCK_TOKEN_RESPONSE))
    auth = OAuth2AuthConfig(token_url=_TOKEN_ENDPOINT, client_id="cid", client_secret="sec")
    result = resolve_auth_config("oauth_svc", auth)
    assert result == "Bearer mocked-token"
    assert "oauth_svc" in _TOKEN_CACHE


@respx.mock
def test_resolve_auth_config_oauth2_cache_hit() -> None:
    """Second call within TTL must not hit the token endpoint."""
    _TOKEN_CACHE["cached_svc"] = ("Bearer cached-token", time.time() + 3600)
    respx.post(_TOKEN_ENDPOINT).mock(return_value=Response(200, json=_MOCK_TOKEN_RESPONSE))
    auth = OAuth2AuthConfig(token_url=_TOKEN_ENDPOINT, client_id="cid", client_secret="sec")
    result = resolve_auth_config("cached_svc", auth)
    assert result == "Bearer cached-token"
    # Token endpoint must NOT have been called
    assert not respx.calls.called
    _TOKEN_CACHE.pop("cached_svc", None)


@respx.mock
def test_resolve_auth_config_oauth2_cache_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expired cache entry triggers a fresh fetch."""
    _TOKEN_CACHE["exp_svc"] = ("Bearer old-token", time.time() - 1)  # already expired
    respx.post(_TOKEN_ENDPOINT).mock(return_value=Response(200, json=_MOCK_TOKEN_RESPONSE))
    auth = OAuth2AuthConfig(token_url=_TOKEN_ENDPOINT, client_id="cid", client_secret="sec")
    result = resolve_auth_config("exp_svc", auth)
    assert result == "Bearer mocked-token"
    assert respx.calls.called
    _TOKEN_CACHE.pop("exp_svc", None)


@respx.mock
def test_resolve_auth_config_oauth2_error_raises_runtime() -> None:
    _TOKEN_CACHE.pop("err_svc", None)
    respx.post(_TOKEN_ENDPOINT).mock(return_value=Response(401, json={"error": "unauthorized"}))
    auth = OAuth2AuthConfig(token_url=_TOKEN_ENDPOINT, client_id="cid", client_secret="bad")
    with pytest.raises(RuntimeError, match="OAuth2 token fetch failed"):
        resolve_auth_config("err_svc", auth)


# ---------------------------------------------------------------------------
# resolve_auth_config — Keycloak token URL construction
# ---------------------------------------------------------------------------

_KC_BASE = "https://keycloak.example.com/auth"
_KC_REALM = "myrealm"
_KC_TOKEN_URL = f"{_KC_BASE}/realms/{_KC_REALM}/protocol/openid-connect/token"


@respx.mock
def test_resolve_auth_config_keycloak_builds_token_url() -> None:
    _TOKEN_CACHE.pop("kc_svc", None)
    respx.post(_KC_TOKEN_URL).mock(return_value=Response(200, json=_MOCK_TOKEN_RESPONSE))
    auth = KeycloakAuthConfig(
        base_url=_KC_BASE,
        realm=_KC_REALM,
        client_id="mce",
        client_secret="sec",
    )
    result = resolve_auth_config("kc_svc", auth)
    assert result == "Bearer mocked-token"
    assert respx.calls.called
    _TOKEN_CACHE.pop("kc_svc", None)


@respx.mock
def test_resolve_auth_config_keycloak_base_url_trailing_slash() -> None:
    """Trailing slash on base_url must not produce double slash in token URL."""
    _TOKEN_CACHE.pop("kc_slash", None)
    respx.post(_KC_TOKEN_URL).mock(return_value=Response(200, json=_MOCK_TOKEN_RESPONSE))
    auth = KeycloakAuthConfig(
        base_url=_KC_BASE + "/",  # trailing slash
        realm=_KC_REALM,
        client_id="mce",
        client_secret="sec",
    )
    result = resolve_auth_config("kc_slash", auth)
    assert result == "Bearer mocked-token"
    _TOKEN_CACHE.pop("kc_slash", None)


# ---------------------------------------------------------------------------
# build_server_env_vars with auth_config
# ---------------------------------------------------------------------------


def test_build_server_env_vars_with_static_auth_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCE_SVC_BASE_URL", raising=False)
    auth = StaticAuthConfig(value="Bearer direct")
    result = build_server_env_vars("svc", auth_config=auth)
    assert result["MCE_SVC_AUTH"] == "Bearer direct"


def test_build_server_env_vars_auth_config_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCE_SVC_AUTH", "Bearer from-env")
    auth = StaticAuthConfig(value="Bearer from-config")
    result = build_server_env_vars("svc", auth_config=auth)
    assert result["MCE_SVC_AUTH"] == "Bearer from-config"


# ---------------------------------------------------------------------------
# resolve_auth_env_vars
# ---------------------------------------------------------------------------


def test_resolve_auth_env_vars_static_sets_auth_key() -> None:
    auth = StaticAuthConfig(value="Bearer xyz")
    result = resolve_auth_env_vars("mysvc", auth)
    assert result == {"MCE_MYSVC_AUTH": "Bearer xyz"}


def test_resolve_auth_env_vars_jwt_sets_auth_key() -> None:
    auth = JwtAuthConfig(token="a.b.c")
    result = resolve_auth_env_vars("mysvc", auth)
    assert result == {"MCE_MYSVC_AUTH": "Bearer a.b.c"}


# ---------------------------------------------------------------------------
# Session auth — cookie-based login (mocked)
# ---------------------------------------------------------------------------

_LOGIN_URL = "https://app.example.com/api/login"
_LOGIN_URL_FORM = "https://app.example.com/login"


@respx.mock
def test_session_auth_cookie_all_cookies() -> None:
    """Login response sets cookies; all are collected into Cookie header."""
    _SESSION_CACHE.pop("sess_svc", None)
    respx.post(_LOGIN_URL).mock(
        return_value=Response(
            200,
            json={"status": "ok"},
            headers={"Set-Cookie": "JSESSIONID=abc123; Path=/"},
        )
    )
    auth = SessionAuthConfig(login_url=_LOGIN_URL, username="user", password="pass")
    result = resolve_auth_env_vars("sess_svc", auth)
    assert "MCE_SESS_SVC_COOKIE" in result
    assert "JSESSIONID=abc123" in result["MCE_SESS_SVC_COOKIE"]
    assert "MCE_SESS_SVC_AUTH" not in result
    _SESSION_CACHE.pop("sess_svc", None)


@respx.mock
def test_session_auth_specific_cookie_name() -> None:
    """Only the named cookie is extracted."""
    _SESSION_CACHE.pop("named_svc", None)
    respx.post(_LOGIN_URL).mock(
        return_value=Response(
            200,
            json={},
            headers={"Set-Cookie": "JSESSIONID=sess42; Path=/"},
        )
    )
    auth = SessionAuthConfig(
        login_url=_LOGIN_URL,
        username="u",
        password="p",
        cookie_name="JSESSIONID",
    )
    result = resolve_auth_env_vars("named_svc", auth)
    assert result["MCE_NAMED_SVC_COOKIE"] == "JSESSIONID=sess42"
    _SESSION_CACHE.pop("named_svc", None)


@respx.mock
def test_session_auth_token_field_sets_auth_header() -> None:
    """Login response returns a token in JSON body → Authorization: Bearer."""
    _SESSION_CACHE.pop("tok_svc", None)
    respx.post(_LOGIN_URL).mock(return_value=Response(200, json={"access_token": "jwt-token-here"}))
    auth = SessionAuthConfig(
        login_url=_LOGIN_URL,
        username="u",
        password="p",
        token_field="access_token",
    )
    result = resolve_auth_env_vars("tok_svc", auth)
    assert result["MCE_TOK_SVC_AUTH"] == "Bearer jwt-token-here"
    assert "MCE_TOK_SVC_COOKIE" not in result
    _SESSION_CACHE.pop("tok_svc", None)


@respx.mock
def test_session_auth_form_content_type() -> None:
    """content_type=form sends form-encoded body."""
    _SESSION_CACHE.pop("form_svc", None)
    respx.post(_LOGIN_URL_FORM).mock(
        return_value=Response(
            200,
            json={},
            headers={"Set-Cookie": "PHPSESSID=php999; Path=/"},
        )
    )
    auth = SessionAuthConfig(
        login_url=_LOGIN_URL_FORM,
        username="u",
        password="p",
        content_type="form",
    )
    result = resolve_auth_env_vars("form_svc", auth)
    assert "PHPSESSID=php999" in result["MCE_FORM_SVC_COOKIE"]
    _SESSION_CACHE.pop("form_svc", None)


@respx.mock
def test_session_auth_cache_hit() -> None:
    """Cached session result is returned without hitting the login endpoint."""
    _SESSION_CACHE["hit_svc"] = (AuthResult(cookie="JSESSIONID=cached"), time.time() + 3600)
    respx.post(_LOGIN_URL).mock(return_value=Response(200, json={}))
    auth = SessionAuthConfig(login_url=_LOGIN_URL, username="u", password="p")
    result = resolve_auth_env_vars("hit_svc", auth)
    assert result["MCE_HIT_SVC_COOKIE"] == "JSESSIONID=cached"
    assert not respx.calls.called
    _SESSION_CACHE.pop("hit_svc", None)


@respx.mock
def test_session_auth_cache_expired_refetches() -> None:
    _SESSION_CACHE["exp2_svc"] = (AuthResult(cookie="JSESSIONID=old"), time.time() - 1)
    respx.post(_LOGIN_URL).mock(
        return_value=Response(
            200,
            json={},
            headers={"Set-Cookie": "JSESSIONID=fresh; Path=/"},
        )
    )
    auth = SessionAuthConfig(login_url=_LOGIN_URL, username="u", password="p")
    result = resolve_auth_env_vars("exp2_svc", auth)
    assert "JSESSIONID=fresh" in result["MCE_EXP2_SVC_COOKIE"]
    _SESSION_CACHE.pop("exp2_svc", None)


@respx.mock
def test_session_auth_login_failure_raises_runtime() -> None:
    _SESSION_CACHE.pop("fail_svc", None)
    respx.post(_LOGIN_URL).mock(return_value=Response(401, json={"error": "bad credentials"}))
    auth = SessionAuthConfig(login_url=_LOGIN_URL, username="u", password="wrong")
    with pytest.raises(RuntimeError, match="Session login failed"):
        resolve_auth_env_vars("fail_svc", auth)


@respx.mock
def test_session_auth_missing_token_field_raises_runtime() -> None:
    _SESSION_CACHE.pop("nofield_svc", None)
    respx.post(_LOGIN_URL).mock(return_value=Response(200, json={"other": "data"}))
    auth = SessionAuthConfig(
        login_url=_LOGIN_URL,
        username="u",
        password="p",
        token_field="access_token",
    )
    with pytest.raises(RuntimeError, match="access_token.*not found"):
        resolve_auth_env_vars("nofield_svc", auth)


@respx.mock
def test_session_auth_no_cookies_raises_runtime() -> None:
    """When no token_field and response sets no cookies, raise RuntimeError."""
    _SESSION_CACHE.pop("nocook_svc", None)
    respx.post(_LOGIN_URL).mock(return_value=Response(200, json={"status": "ok"}))
    auth = SessionAuthConfig(login_url=_LOGIN_URL, username="u", password="p")
    with pytest.raises(RuntimeError, match="no cookies"):
        resolve_auth_env_vars("nocook_svc", auth)


# ---------------------------------------------------------------------------
# build_server_env_vars — cookie env var (legacy path)
# ---------------------------------------------------------------------------


def test_build_server_env_vars_reads_cookie_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy path: MCE_{SERVER}_COOKIE env var is forwarded to the container."""
    monkeypatch.setenv("MCE_LEGACY_COOKIE", "JSESSIONID=legacyval")
    monkeypatch.delenv("MCE_LEGACY_BASE_URL", raising=False)
    result = build_server_env_vars("legacy")
    assert result["MCE_LEGACY_COOKIE"] == "JSESSIONID=legacyval"
