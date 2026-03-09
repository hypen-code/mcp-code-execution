"""Credential vault — securely injects API credentials from environment variables."""

from __future__ import annotations

import os
import re

from mce.utils.logging import get_logger

logger = get_logger(__name__)

# Pattern to resolve ${VAR_NAME} references in config values
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


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


def build_server_env_vars(server_name: str) -> dict[str, str]:
    """Build environment variable dict for a server's Docker container.

    Reads MCE_<SERVER>_BASE_URL and MCE_<SERVER>_AUTH from the host environment
    and returns them for injection into the sandbox container. Credentials are
    NEVER embedded in generated code.

    Args:
        server_name: Name of the server (e.g., "weather").

    Returns:
        Dict of environment variables ready for Docker container injection.
    """
    prefix = f"MCE_{server_name.upper()}_"
    env_vars: dict[str, str] = {}

    base_url_key = f"{prefix}BASE_URL"
    auth_key = f"{prefix}AUTH"
    extra_headers_key = f"{prefix}EXTRA_HEADERS"

    base_url = os.environ.get(base_url_key, "")
    auth = os.environ.get(auth_key, "")
    extra_headers = os.environ.get(extra_headers_key, "")

    if base_url:
        env_vars[base_url_key] = base_url

    if auth:
        # Resolve any ${VAR} references in auth header value
        env_vars[auth_key] = resolve_env_references(auth)

    if extra_headers:
        env_vars[extra_headers_key] = extra_headers

    return env_vars


def build_all_server_env_vars(server_names: list[str]) -> dict[str, str]:
    """Build combined env vars for all required servers.

    Args:
        server_names: List of server names to build credentials for.

    Returns:
        Combined dict of all server environment variables.
    """
    combined: dict[str, str] = {}
    for name in server_names:
        combined.update(build_server_env_vars(name))
    return combined
