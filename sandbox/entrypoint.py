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

    local_ns: dict = {}
    # Block dangerous builtins — keep __import__ so import statements work.
    # Security is enforced by the Docker container itself (read-only FS,
    # no network outside mfp_network, non-root user, resource limits).
    _blocked = {"open", "exec", "eval", "compile", "input", "breakpoint"}
    safe_builtins = {k: v for k, v in vars(builtins).items() if k not in _blocked}

    try:
        compiled_code = compile(code, "<mfp>", "exec")
        exec(compiled_code, {"__builtins__": safe_builtins}, local_ns)  # noqa: S102

        # Convention: code must define main() or result
        if "main" in local_ns and callable(local_ns["main"]):
            output = local_ns["main"]()
        elif "result" in local_ns:
            output = local_ns["result"]
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
