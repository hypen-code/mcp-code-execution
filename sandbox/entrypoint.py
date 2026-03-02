"""Sandbox entrypoint — receives Python code via stdin, executes it, prints JSON result.

This file runs INSIDE the Docker sandbox container. It must remain minimal and secure.
The executed code must define either:
  - A `main()` function that returns the result, OR
  - A `result` variable containing the output

print() calls inside user code are captured and returned in the `prints` field so they
don't corrupt the JSON output line that the host reads.
"""

from __future__ import annotations

import builtins
import io
import json
import sys
import traceback


def main() -> None:
    """Read code from stdin, execute it, and write JSON result to stdout."""
    code = sys.stdin.read()

    if not code.strip():
        print(json.dumps({"success": False, "error": "No code provided"}))
        return

    # Block dangerous builtins — keep __import__ so import statements work.
    # Security is enforced by the Docker container itself (read-only FS,
    # no network outside mce_network, non-root user, resource limits).
    _blocked = {"open", "exec", "eval", "compile", "input", "breakpoint"}
    safe_builtins = {k: v for k, v in vars(builtins).items() if k not in _blocked}

    # Use a single namespace dict for both globals and locals so that
    # top-level imports (e.g. `import httpx`) are visible inside defined
    # functions (e.g. `def main(): ... httpx.get(...)`).
    namespace: dict = {"__builtins__": safe_builtins}

    # Redirect stdout so user print() calls don't corrupt the JSON result line.
    _captured = io.StringIO()
    _real_stdout = sys.stdout
    sys.stdout = _captured

    try:
        compiled_code = compile(code, "<mce>", "exec")
        exec(compiled_code, namespace)  # noqa: S102

        # Convention: code must define main() or result
        if "main" in namespace and callable(namespace["main"]):
            output = namespace["main"]()
        elif "result" in namespace:
            output = namespace["result"]
        else:
            sys.stdout = _real_stdout
            print(json.dumps({"success": False, "error": "Code must define 'result' variable or 'main()' function"}))
            return

        sys.stdout = _real_stdout
        prints = _captured.getvalue() or None
        print(json.dumps({"success": True, "data": output, "prints": prints}, default=str))

    except SyntaxError as exc:
        sys.stdout = _real_stdout
        print(
            json.dumps(
                {
                    "success": False,
                    "error": f"Syntax error: {exc}",
                    "traceback": traceback.format_exc(),
                }
            )
        )
    except Exception as exc:  # noqa: BLE001
        sys.stdout = _real_stdout
        print(
            json.dumps(
                {
                    "success": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
        )


if __name__ == "__main__":
    main()
