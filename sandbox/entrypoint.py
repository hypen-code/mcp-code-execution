"""Sandbox entrypoint — receives Python code via stdin, executes it, prints JSON result.

This file runs INSIDE the Docker sandbox container. It must remain minimal and secure.
The executed code must define either:
  - A `main()` function that returns the result, OR
  - A `result` variable containing the output
"""

from __future__ import annotations

import builtins
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
    # no network outside mfp_network, non-root user, resource limits).
    _blocked = {"open", "exec", "eval", "compile", "input", "breakpoint"}
    safe_builtins = {k: v for k, v in vars(builtins).items() if k not in _blocked}

    # Use a single namespace dict for both globals and locals so that
    # top-level imports (e.g. `import httpx`) are visible inside defined
    # functions (e.g. `def main(): ... httpx.get(...)`).
    namespace: dict = {"__builtins__": safe_builtins}

    try:
        compiled_code = compile(code, "<mfp>", "exec")
        exec(compiled_code, namespace)  # noqa: S102

        # Convention: code must define main() or result
        if "main" in namespace and callable(namespace["main"]):
            output = namespace["main"]()
        elif "result" in namespace:
            output = namespace["result"]
        else:
            print(json.dumps({"success": False, "error": "Code must define 'result' variable or 'main()' function"}))
            return

        print(json.dumps({"success": True, "data": output}, default=str))

    except SyntaxError as exc:
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
