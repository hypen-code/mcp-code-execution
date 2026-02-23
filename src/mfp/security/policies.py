"""Security policies — read-only enforcement and domain allowlist checking."""

from __future__ import annotations

from urllib.parse import urlparse

from mfp.errors import SecurityViolationError
from mfp.utils.logging import get_logger

logger = get_logger(__name__)

# HTTP methods that mutate state
_MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def enforce_read_only(method: str, server_name: str) -> None:
    """Raise SecurityViolationError if a mutating method is used on a read-only server.

    Args:
        method: HTTP method string (uppercase).
        server_name: Name of the server being accessed.

    Raises:
        SecurityViolationError: If method is mutating and server is read-only.
    """
    if method.upper() in _MUTATING_METHODS:
        logger.warning("read_only_violation", server=server_name, method=method)
        raise SecurityViolationError(
            f"Server '{server_name}' is read-only but code attempts {method} operation"
        )


def check_domain_allowed(url: str, allowed_domains: list[str]) -> None:
    """Verify that a URL's domain is in the allowed domains list.

    If allowed_domains is empty, all domains are permitted.

    Args:
        url: Full URL to check.
        allowed_domains: List of allowed hostname strings.

    Raises:
        SecurityViolationError: If domain is not in the allowlist.
    """
    if not allowed_domains:
        return  # No restrictions configured

    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    if not any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed_domains):
        logger.warning("domain_blocked", url=url, hostname=hostname)
        raise SecurityViolationError(
            f"Domain '{hostname}' is not in the allowed domains list"
        )
