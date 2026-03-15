"""Sandbox entrypoint — executes Python code and prints a JSON result to stdout.

This file runs INSIDE the Docker sandbox container.  It must remain minimal
and dependency-free (stdlib only).

Code delivery
-------------
Two modes are supported, in priority order:

1. **Environment variable** (warm + cold mode, preferred):
   The executor sets ``MCE_EXEC_CODE`` to the base64-encoded Python source.
   The entrypoint decodes it and never touches stdin.  This avoids the
   complexities of interactive stdin over ``docker exec``.

2. **Stdin** (legacy / direct invocation):
   If ``MCE_EXEC_CODE`` is absent the entrypoint falls back to reading the
   entire stdin stream.  This keeps the container usable when invoked
   manually (e.g. ``echo 'result=1' | docker run -i mce-sandbox``).

Execution contract
------------------
The submitted code must define either:
  - A ``main()`` function that returns the result, **or**
  - A ``result`` variable containing the output.

``print()`` calls inside user code are captured and returned in the ``prints``
field so they do not corrupt the single JSON result line written to stdout.

Timeout
-------
``MCE_EXEC_TIMEOUT`` (seconds, default 30) is enforced via ``signal.alarm``.
When the alarm fires the process exits immediately — the warm container stays
alive and the pool returns it cleanly.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import signal
import sys
import traceback


def _install_timeout(seconds: int) -> None:
    """Install a SIGALRM-based hard timeout.

    On timeout the process prints a JSON error and exits with code 1.
    Using ``sys.exit`` inside a signal handler is safe here because the
    handler runs in the main thread and there is no cleanup needed.

    Args:
        seconds: Maximum execution time before the process is killed.
    """

    def _handler(signum: int, frame: object) -> None:  # noqa: ARG001
        sys.stdout = _real_stdout  # type: ignore[name-defined]  # restored inside handler
        print(  # noqa: T201
            json.dumps(
                {
                    "success": False,
                    "error": f"Execution timed out after {seconds}s",
                    "traceback": None,
                }
            )
        )
        sys.exit(1)

    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)


def main() -> None:
    """Read code, execute it inside a restricted namespace, and write JSON to stdout."""
    global _real_stdout  # noqa: PLW0603  # used by timeout handler

    # ------------------------------------------------------------------
    # 1. Read code
    # ------------------------------------------------------------------
    encoded = os.environ.get("MCE_EXEC_CODE")
    if encoded:
        import base64  # noqa: PLC0415

        code = base64.b64decode(encoded).decode("utf-8")
    else:
        code = sys.stdin.read()

    if not code.strip():
        print(json.dumps({"success": False, "error": "No code provided"}))  # noqa: T201
        return

    # ------------------------------------------------------------------
    # 2. Install hard timeout (SIGALRM — Linux only, not available on Windows)
    # ------------------------------------------------------------------
    timeout_seconds = int(os.environ.get("MCE_EXEC_TIMEOUT", "30"))
    if hasattr(signal, "SIGALRM"):
        _install_timeout(timeout_seconds)

    # ------------------------------------------------------------------
    # 3. Block dangerous builtins
    # Security is defence-in-depth alongside the AST guard and Docker limits.
    # __import__ is kept so ``import`` statements work normally.
    # ------------------------------------------------------------------
    _blocked = {"open", "exec", "eval", "compile", "input", "breakpoint"}
    safe_builtins = {k: v for k, v in vars(builtins).items() if k not in _blocked}

    # Single namespace for globals and locals so top-level imports are visible
    # inside functions defined in the same code block.
    namespace: dict[str, object] = {"__builtins__": safe_builtins}

    # ------------------------------------------------------------------
    # 4. Redirect stdout so user print() calls don't corrupt the JSON line
    # ------------------------------------------------------------------
    _captured = io.StringIO()
    _real_stdout = sys.stdout
    sys.stdout = _captured

    try:
        compiled_code = compile(code, "<mce>", "exec")
        exec(compiled_code, namespace)  # noqa: S102

        if "main" in namespace and callable(namespace["main"]):
            output = namespace["main"]()
        elif "result" in namespace:
            output = namespace["result"]
        else:
            sys.stdout = _real_stdout
            print(  # noqa: T201
                json.dumps(
                    {
                        "success": False,
                        "error": "Code must define a 'result' variable or a 'main()' function",
                    }
                )
            )
            return

        sys.stdout = _real_stdout
        # Cancel the alarm — execution completed in time
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)

        prints = _captured.getvalue() or None
        print(json.dumps({"success": True, "data": output, "prints": prints}, default=str))  # noqa: T201

    except SyntaxError as exc:
        sys.stdout = _real_stdout
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
        print(  # noqa: T201
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
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
        print(  # noqa: T201
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
