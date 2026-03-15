"""Credential vault — securely injects API credentials from environment variables."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from mce.utils.logging import get_logger

if TYPE_CHECKING:
    from mce.models import AuthConfig, SessionAuthConfig

logger = get_logger(__name__)

# Pattern to resolve ${VAR_NAME} references in config values
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")

# In-memory cache for OAuth2/Keycloak bearer strings: server_name -> (bearer_str, expires_at)
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}

# In-memory cache for session auth results: server_name -> (AuthResult, expires_at)
_SESSION_CACHE: dict[str, tuple[AuthResult, float]] = {}

_CACHE_SKEW_SECONDS = 30  # refresh this many seconds before actual expiry


@dataclass
class AuthResult:
    """Resolved credentials for a server — carries either an Authorization value,
    a Cookie value, or both (when session login returns a token AND sets cookies).
    """

    auth_header: str = field(default="")  # value for the Authorization: header
    cookie: str = field(default="")  # value for the Cookie: header


def resolve_env_references(value: str) -> str:
    """Resolve ${VAR} environment variable references in a string.

    Args:
        value: String potentially containing ${VAR_NAME} references.

    Returns:
        String with all resolvable references replaced by env values.
        Unresolvable references are left as-is and a warning is logged.
    """

    def replace_ref(match: re.Match[str]) -> str:
        var_name = match.group(1)
        resolved = os.environ.get(var_name)
        if resolved is None:
            logger.warning("env_var_not_found", var_name=var_name)
            return match.group(0)  # Leave placeholder unchanged
        return resolved

    return _ENV_VAR_PATTERN.sub(replace_ref, value)


def resolve_auth_config(server_name: str, auth: AuthConfig) -> str:
    """Resolve a non-session AuthConfig to a ready-to-use Authorization header value.

    For static/jwt types the value is returned immediately.
    For oauth2/keycloak the token is fetched and cached with TTL.

    NOTE: For session auth use resolve_auth_env_vars() which returns the full
    dict including Cookie headers.

    Args:
        server_name: Used as cache key and for log context.
        auth: Parsed AuthConfig (not SessionAuthConfig).

    Returns:
        Full Authorization header value, e.g. "Bearer eyJ...".
    """
    from mce.models import JwtAuthConfig, StaticAuthConfig

    if isinstance(auth, StaticAuthConfig):
        return resolve_env_references(auth.value)

    if isinstance(auth, JwtAuthConfig):
        return f"Bearer {resolve_env_references(auth.token)}"

    # OAuth2 / Keycloak — check cache first
    cached = _TOKEN_CACHE.get(server_name)
    if cached and time.time() < cached[1] - _CACHE_SKEW_SECONDS:
        logger.debug("token_cache_hit", server=server_name)
        return cached[0]

    logger.info("fetching_oauth2_token", server=server_name)
    bearer, expires_at = _fetch_oauth2_token(server_name, auth)
    _TOKEN_CACHE[server_name] = (bearer, expires_at)
    return bearer


def resolve_auth_env_vars(server_name: str, auth: AuthConfig) -> dict[str, str]:
    """Resolve an AuthConfig to the MCE_{SERVER}_* env vars it produces.

    For most auth types this sets MCE_{SERVER}_AUTH (Authorization header).
    For session auth it may set MCE_{SERVER}_COOKIE instead, or both when
    the login endpoint returns a token in the response body AND sets cookies.

    Args:
        server_name: Server name — used for env var prefix, cache key, and logging.
        auth: Parsed AuthConfig from SwaggerSource.

    Returns:
        Dict of ``MCE_{SERVER}_AUTH`` and/or ``MCE_{SERVER}_COOKIE`` ready for
        Docker container injection.
    """
    from mce.models import SessionAuthConfig

    prefix = f"MCE_{server_name.upper()}_"

    if isinstance(auth, SessionAuthConfig):
        result = _resolve_session_auth(server_name, auth)
        env: dict[str, str] = {}
        if result.auth_header:
            env[f"{prefix}AUTH"] = result.auth_header
        if result.cookie:
            env[f"{prefix}COOKIE"] = result.cookie
        return env

    return {f"{prefix}AUTH": resolve_auth_config(server_name, auth)}


def _fetch_oauth2_token(server_name: str, auth: AuthConfig) -> tuple[str, float]:
    """Fetch an OAuth2 client credentials token via authlib + httpx.

    Args:
        server_name: Only used for error messages.
        auth: OAuth2AuthConfig or KeycloakAuthConfig.

    Returns:
        (bearer_header_value, expiry_epoch_seconds) tuple.

    Raises:
        RuntimeError: When the token endpoint returns an error.
    """
    from authlib.integrations.httpx_client import OAuth2Client

    from mce.models import KeycloakAuthConfig

    if isinstance(auth, KeycloakAuthConfig):
        token_url = f"{auth.base_url.rstrip('/')}/realms/{auth.realm}/protocol/openid-connect/token"
    else:
        token_url = auth.token_url  # type: ignore[union-attr]

    client_secret = resolve_env_references(auth.client_secret)  # type: ignore[union-attr]
    scope = auth.scope or None  # type: ignore[union-attr]

    client: OAuth2Client = OAuth2Client(
        client_id=auth.client_id,  # type: ignore[union-attr]
        client_secret=client_secret,
    )
    try:
        token = client.fetch_token(
            token_url,
            grant_type="client_credentials",
            scope=scope,
        )
    except Exception as exc:
        raise RuntimeError(f"OAuth2 token fetch failed for server '{server_name}': {exc}") from exc

    access_token: str = token["access_token"]
    expires_in: int = token.get("expires_in", 3600)
    expires_at = time.time() + expires_in
    return f"Bearer {access_token}", expires_at


def _resolve_session_auth(server_name: str, auth: SessionAuthConfig) -> AuthResult:
    """Log in to the session endpoint and cache the resulting cookie or bearer token.

    Flow:
      1. POST credentials to auth.login_url (JSON or form-encoded).
      2a. If auth.token_field is set → extract that field from the JSON response
          body and return it as an Authorization: Bearer token.
      2b. Otherwise → collect cookies from the response.
          If auth.cookie_name is set, only that cookie is extracted.
          If empty, all cookies are joined as "name=value; name2=value2".
      3. Cache the AuthResult for auth.expires_seconds.

    Args:
        server_name: Used as cache key and for error messages.
        auth: SessionAuthConfig with login details.

    Returns:
        AuthResult with auth_header and/or cookie populated.

    Raises:
        RuntimeError: On login failure or missing expected token/cookie.
    """
    cached = _SESSION_CACHE.get(server_name)
    if cached and time.time() < cached[1] - _CACHE_SKEW_SECONDS:
        logger.debug("session_cache_hit", server=server_name)
        return cached[0]

    logger.info("fetching_session_token", server=server_name)

    username = resolve_env_references(auth.username)
    password = resolve_env_references(auth.password)
    payload = {auth.username_field: username, auth.password_field: password}

    try:
        with httpx.Client(verify=False) as client:
            if auth.content_type == "form":
                resp = client.post(auth.login_url, data=payload)
            else:
                resp = client.post(auth.login_url, json=payload)
            resp.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"Session login failed for server '{server_name}': {exc}") from exc

    result: AuthResult

    if auth.token_field:
        # Extract bearer token from JSON response body
        try:
            token_value = resp.json()[auth.token_field]
        except (KeyError, ValueError) as exc:
            raise RuntimeError(
                f"Session login for '{server_name}': field '{auth.token_field}' not found in response body"
            ) from exc
        result = AuthResult(auth_header=f"Bearer {token_value}")
    else:
        # Extract cookies from the Set-Cookie response headers
        if auth.cookie_name:
            cookie_val = resp.cookies.get(auth.cookie_name)
            if not cookie_val:
                raise RuntimeError(
                    f"Session login for '{server_name}': cookie '{auth.cookie_name}' not set in login response"
                )
            cookie_str = f"{auth.cookie_name}={cookie_val}"
        else:
            cookie_str = "; ".join(f"{k}={v}" for k, v in resp.cookies.items())
            if not cookie_str:
                raise RuntimeError(
                    f"Session login for '{server_name}': no cookies in login response and no token_field configured"
                )
        result = AuthResult(cookie=cookie_str)

    expires_at = time.time() + auth.expires_seconds
    _SESSION_CACHE[server_name] = (result, expires_at)
    return result


def build_server_env_vars(
    server_name: str,
    auth_config: AuthConfig | None = None,
) -> dict[str, str]:
    """Build environment variable dict for a server's Docker container.

    When auth_config is provided (from SwaggerSource.auth), it takes precedence
    and tokens are fetched/cached as needed. Falls back to MCE_{SERVER}_AUTH and
    MCE_{SERVER}_COOKIE env vars for servers whose auth was baked in at compile time.

    Credentials are NEVER embedded in generated code.

    Args:
        server_name: Name of the server (e.g., "weather").
        auth_config: Optional typed auth config from SwaggerSource.auth.

    Returns:
        Dict of environment variables ready for Docker container injection.
    """
    prefix = f"MCE_{server_name.upper()}_"
    env_vars: dict[str, str] = {}

    base_url_key = f"{prefix}BASE_URL"
    auth_key = f"{prefix}AUTH"
    cookie_key = f"{prefix}COOKIE"
    extra_headers_key = f"{prefix}EXTRA_HEADERS"

    base_url = os.environ.get(base_url_key, "")
    extra_headers = os.environ.get(extra_headers_key, "")

    if base_url:
        env_vars[base_url_key] = base_url

    if auth_config is not None:
        env_vars.update(resolve_auth_env_vars(server_name, auth_config))
    else:
        # Legacy path: credentials were baked into process env at compile/serve time
        auth = os.environ.get(auth_key, "")
        if auth:
            env_vars[auth_key] = resolve_env_references(auth)
        cookie = os.environ.get(cookie_key, "")
        if cookie:
            env_vars[cookie_key] = cookie  # cookies are not ${VAR}-expanded

    if extra_headers:
        env_vars[extra_headers_key] = extra_headers

    return env_vars


def build_all_server_env_vars(
    server_names: list[str],
    auth_configs: dict[str, AuthConfig | None] | None = None,
) -> dict[str, str]:
    """Build combined env vars for all required servers.

    Args:
        server_names: List of server names to build credentials for.
        auth_configs: Optional mapping of server name to AuthConfig.

    Returns:
        Combined dict of all server environment variables.
    """
    combined: dict[str, str] = {}
    for name in server_names:
        cfg = (auth_configs or {}).get(name)
        combined.update(build_server_env_vars(name, cfg))
    return combined
