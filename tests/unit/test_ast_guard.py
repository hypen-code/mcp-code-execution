"""Unit tests for the AST security guard."""

from __future__ import annotations

import pytest

from mfp.errors import SecurityViolationError
from mfp.security.ast_guard import ASTGuard


@pytest.fixture
def guard() -> ASTGuard:
    return ASTGuard()


# ---------------------------------------------------------------------------
# Safe code — should pass
# ---------------------------------------------------------------------------


def test_plain_math_passes(guard: ASTGuard) -> None:
    guard.validate("result = 2 + 2")


def test_import_httpx_passes(guard: ASTGuard) -> None:
    guard.validate("import httpx\nresult = httpx.get('http://example.com').json()")


def test_import_json_passes(guard: ASTGuard) -> None:
    guard.validate("import json\nresult = json.dumps({'key': 'value'})")


def test_import_datetime_passes(guard: ASTGuard) -> None:
    guard.validate("from datetime import datetime\nresult = datetime.now().isoformat()")


def test_import_typing_passes(guard: ASTGuard) -> None:
    guard.validate("from typing import Any, Optional\nresult: Any = None")


def test_import_collections_passes(guard: ASTGuard) -> None:
    guard.validate("from collections import defaultdict\nresult = defaultdict(list)")


def test_server_function_import_passes(guard: ASTGuard) -> None:
    guard.validate("from weather.functions import get_current_weather\nresult = None")


# ---------------------------------------------------------------------------
# Blocked patterns — must raise SecurityViolationError
# ---------------------------------------------------------------------------


def test_import_os_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_import"):
        guard.validate("import os")


def test_import_sys_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_import"):
        guard.validate("import sys")


def test_import_subprocess_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_import"):
        guard.validate("import subprocess")


def test_import_socket_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_import"):
        guard.validate("import socket")


def test_from_os_import_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_import"):
        guard.validate("from os import path")


def test_eval_call_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_call"):
        guard.validate("eval('1 + 1')")


def test_exec_call_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_call"):
        guard.validate("exec('x = 1')")


def test_compile_call_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_call"):
        guard.validate("compile('x = 1', '<str>', 'exec')")


def test_dunder_import_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_call"):
        guard.validate("__import__('os')")


def test_open_call_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_call"):
        guard.validate("open('/etc/passwd').read()")


def test_dunder_subclasses_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_attribute"):
        guard.validate("().__class__.__subclasses__()")


def test_dunder_globals_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_attribute"):
        guard.validate("x = {}; x.__globals__")


def test_environ_access_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_attribute"):
        guard.validate("import os; x = os.environ['SECRET']")


def test_invalid_syntax_raises_violation(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="syntax"):
        guard.validate("def broken(: pass")


def test_global_statement_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_global"):
        guard.validate("def f():\n    global x\n    x = 1")
