# Security Model

MCE uses a **defense-in-depth** approach: multiple independent layers, each of which stops a different class of attack. No single layer is trusted to be sufficient on its own.

---

## Layers at a Glance

```
User code submitted via execute_code
          │
          ▼
 1. Code Size Limit          ← reject before any parsing
          │
          ▼
 2. AST Security Guard       ← static analysis, block dangerous patterns
          │
          ▼
 3. Ruff Lint Gate (opt.)    ← syntax and style validation
          │
          ▼
 4. Docker Sandbox           ← isolated execution environment
          │
          ▼
 5. Credential Vault         ← secrets injected as env vars, never in code
          │
          ▼
 Runtime enforcement:
 6. Read-Only Enforcement    ← compile-time endpoint filtering
 7. Domain Allowlist         ← runtime hostname whitelist
```

---

## Layer 1 — Code Size Limit

Code exceeding `MCE_MAX_CODE_SIZE_BYTES` (default 64 KB) is rejected immediately, before any parsing or analysis.

This prevents resource exhaustion attacks through pathologically large payloads.

---

## Layer 2 — AST Security Guard

Before code reaches the Docker sandbox, MCE walks the Python AST and blocks any of the following:

**Blocked imports (70+ modules):**

```
os, sys, subprocess, socket, threading, multiprocessing,
pickle, marshal, shelve, ctypes, cffi, importlib,
shutil, pathlib, tempfile, io, builtins, gc, inspect,
signal, mmap, resource, pty, tty, termios, fcntl, grp,
pwd, crypt, spwd, nis, syslog, platform, posix,
posixpath, nt, ntpath, ...and more
```

**Blocked builtins:**

```
eval, exec, open, compile, __import__, getattr (on unknown objects),
vars, locals, globals, dir, delattr, setattr
```

**Blocked AST patterns:**

- `import os` — standard blocked import
- `from os import *` — wildcard import of blocked module
- `import os as operating_system` — aliased import
- `__builtins__['eval'](...)` — builtin access via subscript
- `getattr(obj, 'dangerous_attr')` — dynamic attribute access
- `nonlocal` statements in unexpected scopes

Any violation raises a `SecurityViolationError` and the code is never executed.

### Why AST Analysis Instead of Sandboxing Alone?

The AST guard fails fast and provides actionable error messages before any container overhead is incurred. It also prevents entire classes of attacks that could theoretically bypass Docker (e.g. kernel exploits) by eliminating dangerous patterns at the source.

---

## Layer 3 — Ruff Lint Gate (Optional)

When `MCE_LINT_ENABLED=true`, generated code is linted with `ruff` before entering the sandbox. Syntactically invalid or severely style-violating code is rejected with the specific lint error, giving the LLM actionable feedback to self-correct.

Disabled by default to avoid latency overhead in most deployments. Enable it for environments where code quality is a higher priority than execution speed.

---

## Layer 4 — Docker Sandbox

All code runs inside a purpose-built Docker container (`mce-sandbox:latest`). The container is created fresh for each execution and destroyed afterward.

### Sandbox Configuration

| Constraint | Value | Purpose |
|------------|-------|---------|
| Base image | `python:3.13-slim` | Minimal attack surface |
| User | `executor` (UID 1000) | Non-root execution |
| Memory limit | 256 MB | Prevent memory exhaustion |
| CPU quota | 50% of one core | Prevent CPU starvation |
| Filesystem | Read-only (except `/tmp`) | No persistent writes |
| Host mounts | None | No host filesystem access |
| Execution timeout | 30 s (configurable) | Prevent infinite loops |
| Network | Isolated Docker network | API access only; no host network |

### Available Libraries in Sandbox

The sandbox has a minimal, intentionally small set of packages:

```
httpx      — HTTP requests (the only way to call APIs)
pydantic   — Data validation
orjson     — Fast JSON serialization
```

Nothing else is available. Any `import` that isn't one of the above or a stdlib module allowed by the AST guard will fail at import time inside the container.

---

## Layer 5 — Credential Vault

API keys, bearer tokens, and custom headers are **never written to generated code, never logged, and never exposed to any LLM**.

### How Credentials Flow

```
.env / host environment
  MCE_WEATHER_AUTH=Authorization: Bearer sk-secret
  MCE_WEATHER_BASE_URL=https://api.weather.example.com/v1
          │
          │  (1) vault.py reads credentials at execution time only
          ▼
  CodeExecutor._run_in_docker()
    build_all_server_env_vars(["weather"])
          │
          │  (2) passed as Docker -e flags — never written to code
          ▼
  docker run -e MCE_WEATHER_AUTH=... -e MCE_WEATHER_BASE_URL=...
          │
          │  (3) read from container environment at import time
          ▼
  compiled/weather/functions.py (inside sandbox)
    _AUTH_HEADER = os.environ.get("MCE_WEATHER_AUTH", "")
```

### What the LLM Sees vs. What It Never Sees

| Stage | LLM sees | LLM never sees |
|-------|----------|----------------|
| `execute_code` call | User code with `from weather.functions import ...` | API keys, base URLs, header values |
| `--llm-enhance` compile step | Code with `os.environ["MCE_WEATHER_AUTH"]` placeholder strings | The actual resolved values |
| `get_functions` response | Function signatures, parameter names, return schemas | Credentials, base URLs, server internals |

### Credential Safety Checklist

- Store secrets in `.env` or the system environment — never in `config/swaggers.yaml` as literal values.

  ```yaml
  # Safe
  auth_header: "Bearer ${MY_API_TOKEN}"

  # Unsafe — literal secret will be visible in git history
  auth_header: "Bearer sk-actual-secret-key"
  ```

- Never pass credentials as arguments to `execute_code`. Generated code calls the pre-built functions (e.g. `get_current_weather(city="London")`) which handle auth internally.

- The generated `functions.py` files in `compiled/` contain only `os.environ` name references, not values — they are safe to inspect or commit.

---

## Layer 6 — Read-Only Enforcement

Servers marked `is_read_only: true` in `swaggers.yaml` have all mutating endpoints stripped **at compile time**:

- `POST` — create
- `PUT` — replace
- `PATCH` — update
- `DELETE` — delete

The generated `functions.py` will contain only `GET` (and `HEAD`) wrappers. Even if the LLM writes code that imports a missing write function, it will fail with an `ImportError` — not an unauthorized API call.

---

## Layer 7 — Domain Allowlist

When `MCE_ALLOWED_DOMAINS` is set (comma-separated), any request targeting a hostname outside the list is rejected at runtime.

```env
MCE_ALLOWED_DOMAINS=api.weather.com,api.hotel.com
```

This prevents a compromised code snippet from reaching arbitrary external hosts, even if it somehow bypassed the AST guard.

---

## Reporting Security Vulnerabilities

**Do not open public GitHub issues for security vulnerabilities.**

If you discover an issue (AST guard bypass, credential leak, sandbox escape):

1. Email the maintainers privately or use [GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability).
2. Include a minimal reproducible example and an impact assessment.
3. Allow up to **72 hours** for an initial response before any public disclosure.

Responsible disclosure will be credited in the changelog.

---

*Next: [Compiler Pipeline](Compiler-Pipeline) →*
