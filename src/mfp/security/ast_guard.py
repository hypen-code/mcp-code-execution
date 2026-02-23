"""AST-based security guard — static analysis of LLM-generated code before execution."""

from __future__ import annotations

import ast
from typing import Any

from mfp.errors import SecurityViolationError
from mfp.utils.logging import get_logger

logger = get_logger(__name__)

# Modules completely blocked from import
_BLOCKED_MODULES: frozenset[str] = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "shutil",
        "socket",
        "ctypes",
        "pty",
        "tty",
        "termios",
        "signal",
        "resource",
        "multiprocessing",
        "threading",
        "concurrent",
        "pickle",
        "marshal",
        "shelve",
        "importlib",
        "pkgutil",
        "pathlib",
        "glob",
        "tempfile",
        "io",
        "builtins",
        "gc",
        "inspect",
        "dis",
        "code",
        "codeop",
        "pdb",
        "trace",
        "profile",
        "pstats",
        "timeit",
        "ast",
        "tokenize",
        "token",
        "keyword",
        "symtable",
        "urllib",
        "http",
        "xmlrpc",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "telnetlib",
        "requests",
        "aiohttp",
        "tornado",
        "flask",
        "django",
        "fastapi",
        "starlette",
    }
)

# Allowed top-level modules (explicit allowlist)
_ALLOWED_MODULES: frozenset[str] = frozenset(
    {
        "httpx",
        "json",
        "datetime",
        "re",
        "math",
        "typing",
        "dataclasses",
        "collections",
        "itertools",
        "functools",
        "operator",
        "string",
        "decimal",
        "fractions",
        "statistics",
        "random",
        "enum",
        "abc",
        "copy",
        "pprint",
        "textwrap",
        "unicodedata",
        "struct",
        "codecs",
        "hashlib",
        "hmac",
        "base64",
        "binascii",
        "zlib",
        "gzip",
        "bz2",
        "lzma",
        "csv",
        "configparser",
        "calendar",
        "time",
        "uuid",
        "pydantic",
        "orjson",
        "__future__",
    }
)

# Dangerous builtin calls that are not allowed
_BLOCKED_CALLS: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "open",
        "input",
        "print",
        "breakpoint",
        "vars",
        "dir",
        "globals",
        "locals",
    }
)

# Dangerous attribute access patterns
_BLOCKED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "__class__",
        "__subclasses__",
        "__globals__",
        "__builtins__",
        "__loader__",
        "__spec__",
        "__dict__",
        "__mro__",
        "__bases__",
        "__import__",
        "environ",  # os.environ access
        "system",
        "popen",
        "spawn",
        "exec_",
        "execve",
        "fork",
        "kill",
        "getenv",
        "setenv",
        "putenv",
    }
)


class ASTGuard:
    """Static code analyzer that blocks unsafe patterns before execution."""

    def validate(self, code: str, context: str = "") -> None:
        """Validate Python code for security violations.

        Parses the code into an AST and walks it, checking all nodes
        against block/allow lists for imports, calls, and attribute access.

        Args:
            code: Python source code to validate.
            context: Optional description for logging (not the code itself).

        Raises:
            SecurityViolationError: If any blocked pattern is found.
        """
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError as exc:
            raise SecurityViolationError(f"Invalid Python syntax: {exc}") from exc

        visitor = _SecurityVisitor()
        visitor.visit(tree)

        if visitor.violations:
            violation = visitor.violations[0]
            logger.warning(
                "security_violation_blocked",
                context=context,
                violation_type=violation["type"],
                detail=violation["detail"],
            )
            raise SecurityViolationError(f"Security violation ({violation['type']}): {violation['detail']}")


class _SecurityVisitor(ast.NodeVisitor):
    """AST node visitor that collects security violations."""

    def __init__(self) -> None:
        self.violations: list[dict[str, Any]] = []

    def _add_violation(self, violation_type: str, detail: str) -> None:
        self.violations.append({"type": violation_type, "detail": detail})

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        """Check top-level import statements."""
        for alias in node.names:
            module = alias.name.split(".")[0]
            if module in _BLOCKED_MODULES:
                self._add_violation("blocked_import", f"import {alias.name}")
            elif module not in _ALLOWED_MODULES and not module.startswith("_"):
                # Allow server function modules (e.g., weather.functions)
                # Deny anything else not in allowlist
                pass  # Conservative: only block explicitly blocked modules
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        """Check from X import Y statements."""
        module = (node.module or "").split(".")[0]
        if module in _BLOCKED_MODULES:
            self._add_violation("blocked_import", f"from {node.module} import ...")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        """Check function call nodes."""
        # Direct function calls: eval(), exec(), etc.
        if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_CALLS:
            self._add_violation("blocked_call", f"call to {node.func.id}()")

        # Method calls: obj.method()
        if isinstance(node.func, ast.Attribute) and node.func.attr in _BLOCKED_ATTRIBUTES:
            self._add_violation("blocked_attribute_call", f"call to .{node.func.attr}()")

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        """Check attribute access nodes."""
        if node.attr in _BLOCKED_ATTRIBUTES:
            self._add_violation("blocked_attribute", f"access to .{node.attr}")
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:  # noqa: N802
        """Block global statement usage."""
        self._add_violation("blocked_global", "global statement not allowed")
        self.generic_visit(node)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:  # noqa: N802
        """Block nonlocal statement usage."""
        self._add_violation("blocked_nonlocal", "nonlocal statement not allowed")
        self.generic_visit(node)
