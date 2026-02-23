# AGENTS.md — MFP Development Guide for AI Agents

> This file governs how AI agents develop, maintain, and extend the MFP
> (ModelFunctionProtocol) codebase. Read it in full before making any change.

---

## 1. Project Overview

**MFP** is a production-grade MCP server built with Python 3.13 and FastMCP.
It exposes **4 meta-tools** to LLMs instead of bloating the context with
N per-endpoint tools. The 4 tools are:

| Tool | Purpose |
|------|---------|
| `list_servers` | Discover compiled API servers and their functions |
| `get_function` | Inspect a specific function's parameters and response schema |
| `execute_code` | Run LLM-generated Python code in an isolated Docker sandbox |
| `get_cached_code` | Find and reuse previously successful code snippets |

**Key invariant**: credentials are **never** embedded in generated code or logs.
They are injected exclusively as Docker environment variables at runtime.

---

## 2. Repository Layout

```
/home/ob1/aai/mcp-code-execution/      ← project root
├── pyproject.toml                      ← single source of truth for deps & tools
├── .env.example                        ← template; never commit .env
├── config/swaggers.yaml                ← swagger source definitions
├── sandbox/
│   ├── Dockerfile                      ← python:3.13-slim sandbox image
│   ├── entrypoint.py                   ← code receiver inside sandbox
│   └── requirements.txt                ← httpx, pydantic, orjson only
├── src/mfp/
│   ├── __init__.py                     ← version only
│   ├── __main__.py                     ← CLI: compile | serve | run
│   ├── server.py                       ← FastMCP tool registration (4 tools)
│   ├── config.py                       ← MFPConfig (pydantic-settings)
│   ├── errors.py                       ← full exception hierarchy
│   ├── models/__init__.py              ← ALL pydantic models (single file)
│   ├── compiler/
│   │   ├── swagger_parser.py           ← OpenAPI 3.x / Swagger 2.0 parser
│   │   ├── codegen.py                  ← Jinja2 Python function generator
│   │   ├── orchestrator.py             ← compile pipeline coordinator
│   │   ├── llm_enhancer.py             ← optional Claude improvement pass
│   │   └── templates/function.py.j2   ← Jinja2 template for functions.py
│   ├── runtime/
│   │   ├── registry.py                 ← loads manifests, provides lookups
│   │   ├── executor.py                 ← Docker sandbox execution pipeline
│   │   └── cache.py                    ← async SQLite cache (aiosqlite)
│   ├── security/
│   │   ├── ast_guard.py                ← AST static analysis (runs before exec)
│   │   ├── policies.py                 ← read-only + domain allowlist enforcement
│   │   └── vault.py                    ← credential → Docker env var injection
│   └── utils/
│       ├── logging.py                  ← structlog setup
│       └── hashing.py                  ← SHA256 helpers for cache keys
├── tests/
│   ├── conftest.py                     ← shared fixtures (mfp_config, specs, sources)
│   ├── fixtures/                       ← YAML swagger test fixtures
│   │   ├── weather_api.yaml            ← read-only, simple GET endpoints
│   │   ├── hotel_api.yaml              ← read-write, path + body params
│   │   └── petstore.yaml               ← standard petstore with $ref schemas
│   ├── unit/                           ← isolated, no Docker, no network
│   │   ├── test_swagger_parser.py
│   │   ├── test_codegen.py
│   │   ├── test_ast_guard.py
│   │   └── test_cache.py
│   └── integration/                    ← requires compiled output on disk
│       └── test_compiler.py
```

**Rules:**
- **Do not create files outside this structure** without explicit instruction.
- **One model file**: all Pydantic models live in `src/mfp/models/__init__.py`.
- **One error file**: all exceptions live in `src/mfp/errors.py`.
- Max file length: **400 lines**. Split if exceeded.
- Max function length: **50 lines**. Decompose if exceeded.

---

## 3. Technology Decisions (Locked)

| Concern | Choice | Why |
|---------|--------|-----|
| Python version | **3.13+** | Required. Use `from __future__ import annotations`. |
| MCP framework | **FastMCP ≥ 2.0** | `from fastmcp import FastMCP` |
| Config | **pydantic-settings** `BaseSettings` | `MFP_` env prefix, `.env` file |
| HTTP client | **httpx** | Async + sync; used in generated code too |
| Sandbox | **Docker SDK** (`docker` package) | Isolation, resource limits |
| Cache | **aiosqlite** SQLite | Zero external dependency, async |
| Templates | **Jinja2** | Deterministic codegen |
| Logging | **structlog** | JSON in production, console in DEBUG |
| Lint | **ruff** | Fast, replaces flake8 + isort + pyupgrade |
| Types | **mypy --strict** | All public signatures must be fully typed |
| Testing | **pytest + pytest-asyncio** | `asyncio_mode = "auto"` in pyproject.toml |
| HTTP mocking | **respx** | Mock `httpx` calls without real network |

**Do not introduce new dependencies** without adding them to `pyproject.toml`
under the appropriate section (`dependencies`, `dev`, or `llm`).

---

## 4. Code Quality Rules (Non-Negotiable)

Every file you write or modify must follow all of these:

### 4.1 Type Annotations
```python
# CORRECT — fully typed
async def execute(self, code: str, description: str) -> ExecutionResult: ...

# WRONG — missing return type
async def execute(self, code, description): ...
```
- `from __future__ import annotations` at the top of every Python file.
- Use `X | Y` union syntax (Python 3.10+), not `Optional[X]` or `Union[X, Y]`.
- Use `list[str]`, `dict[str, Any]` (lowercase generics), not `List`, `Dict`.

### 4.2 Docstrings
Every public function, method, and class needs a Google-style docstring:
```python
def hash_code(code: str) -> str:
    """Hash Python code string for cache key generation.

    Args:
        code: Python source code to hash.

    Returns:
        SHA256 hex digest of normalized code.

    Raises:
        ValueError: If code is empty.
    """
```
Private methods (leading `_`) need at minimum a one-line docstring.

### 4.3 Error Handling
```python
# CORRECT — specific exceptions, structured logging
try:
    result = await executor.execute(code, description)
except SecurityViolationError as exc:
    logger.warning("security_block", detail=str(exc))
    return {"success": False, "error": str(exc), "error_type": "security"}
except Exception as exc:
    logger.exception("unexpected_error", context="execute_code")
    return {"success": False, "error": "Internal error", "error_type": "internal"}

# WRONG — bare except, print, unhandled
try:
    ...
except:
    print("error")
```
- **No bare `except:`** — always name the exception type.
- **No `print()`** — use `logger = get_logger(__name__)` from `mfp.utils.logging`.
- MCP tool functions must **never raise** — always return a dict with `error` key.

### 4.4 Logging
```python
from mfp.utils.logging import get_logger
logger = get_logger(__name__)

# CORRECT — structured key=value pairs
logger.info("cache_stored", id=entry_id[:12], description=description[:50])

# WRONG — f-string message with embedded data
logger.info(f"stored cache entry {entry_id}")
```
Events to always log:
- Server startup and config summary (mask credentials — never log auth values)
- Each compile phase: server parsed, endpoints found/skipped
- Tool invocations: which tool, input size
- Security violations: violation type and pattern (NOT the full code)
- Cache: hits, misses, evictions, invalidations
- Docker: container create, timeout, remove

### 4.5 Constants Over Magic Values
```python
# CORRECT
_MAX_SCHEMA_DEPTH = 2
_MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# WRONG
if depth > 2: ...
if method in {"post", "put", "patch", "delete"}: ...
```

### 4.6 Pydantic at All Boundaries
- Every tool input/output uses a Pydantic model or `dict` derived from `.model_dump()`.
- Every config field uses `MFPConfig` (never raw `os.environ` in business logic).
- Every cross-module data structure is a Pydantic model in `models/__init__.py`.

### 4.7 No Mutable Defaults
```python
# CORRECT
servers_used: list[str] = Field(default_factory=list)

# WRONG
servers_used: list[str] = []
```

---

## 5. Security Rules (Never Violate)

These rules protect against malicious LLM-generated code and credential leaks:

1. **AST guard runs before every execution** — `ASTGuard().validate(code)` is
   called in `executor.py` *before* any Docker container is started. Never bypass it.

2. **Credentials are never in code** — The vault (`security/vault.py`) builds
   `MFP_{SERVER}_BASE_URL` and `MFP_{SERVER}_AUTH` env vars. These are passed
   to Docker via `environment=`. They must **never** appear in:
   - Generated `functions.py` files
   - Log messages
   - Tool responses (success or error)
   - Cache entries

3. **Read-only enforcement is at parse time** — When `is_read_only: true`,
   `swagger_parser.py` drops POST/PUT/PATCH/DELETE endpoints entirely; they
   never reach codegen. Enforce this in `_parse_operation()`.

4. **Sandbox constraints are non-negotiable** — Docker containers must run with:
   - `mem_limit="256m"`, `memswap_limit="256m"`
   - `cpu_quota=50_000` (50% of one core)
   - `security_opt=["no-new-privileges:true"]`
   - `read_only=True` (plus `tmpfs={"/tmp": "size=64m,mode=1777"}`)
   - Non-root user (`executor` UID 1000) — enforced in `sandbox/Dockerfile`
   - No host volume mounts

5. **Domain allowlist** — When `MFP_ALLOWED_DOMAINS` is set, `policies.py`
   rejects any URL whose hostname is not in the list.

6. **Code size limit** — Reject any code > `MFP_MAX_CODE_SIZE_BYTES` (default
   64 KB) before AST parsing even begins.

---

## 6. Testing Rules (100% Coverage Target)

### 6.1 Coverage Requirements
```
tests/unit/        → no Docker, no network, no filesystem side effects
tests/integration/ → may write to tmp_path, no live Docker required
```

- **100% line coverage** is the target. Every branch must be tested.
- Run `pytest --cov=mfp --cov-report=term-missing` to see gaps.
- A PR that reduces coverage is **rejected**.

### 6.2 Fixture Usage
All shared fixtures live in `tests/conftest.py`. Use them instead of
re-declaring inline. Key fixtures:

| Fixture | Type | Purpose |
|---------|------|---------|
| `mfp_config` | `MFPConfig` | Points at `tmp_path`; safe for all tests |
| `sample_endpoint` | `EndpointSpec` | GET /weather/current with 2 params |
| `sample_server_spec` | `ServerSpec` | weather server with 1 endpoint |
| `weather_swagger_source` | `SwaggerSource` | Points at `tests/fixtures/weather_api.yaml` |
| `hotel_swagger_source` | `SwaggerSource` | Points at `tests/fixtures/hotel_api.yaml` |
| `petstore_swagger_source` | `SwaggerSource` | Points at `tests/fixtures/petstore.yaml` |

### 6.3 Test Naming Convention
```python
# Pattern: test_{unit_under_test}_{condition}_{expected_outcome}

def test_parse_weather_api_returns_server_spec(...)        # ✅
def test_import_os_blocked(...)                            # ✅
def test_expired_entry_not_returned(...)                   # ✅

def test_parser(...)                                       # ❌ too vague
def test_1(...)                                            # ❌ meaningless
```

### 6.4 One Assertion Concept Per Test
Each test should verify exactly **one behaviour**. Long tests that check 10
things must be split.

### 6.5 Async Tests
Use `async def test_...` — `asyncio_mode = "auto"` is set in `pyproject.toml`
so no `@pytest.mark.asyncio` decorator is needed.

### 6.6 Mocking Network and Docker
- **httpx network calls**: use `respx` to mock `httpx` requests.
- **Docker**: mock `docker.from_env()` and `DockerClient` with `unittest.mock.MagicMock`.
- **Time**: use `unittest.mock.patch("time.time", return_value=...)` to freeze time.
- **Environment variables**: use `monkeypatch.setenv("MFP_WEATHER_AUTH", "Bearer test")`.

### 6.7 Swagger Parser Tests
The parser reads from `tests/fixtures/*.yaml` files — these are the single
source of truth for expected behaviour. Do **not** inline YAML strings in
test files. Add a new fixture file if you need a new scenario.

### 6.8 Security Tests (AST Guard)
Every **blocked** pattern must have its own test:
```python
def test_{dangerous_pattern}_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="..."):
        guard.validate("...")
```
Every **allowed** pattern must also have a test confirming no exception.

### 6.9 Cache Tests
Cache tests use `tmp_path` for the SQLite database. Never use a shared
database path across tests — isolation is mandatory.

---

## 7. Module-by-Module Responsibilities

### `errors.py`
- All custom exceptions live here and **only** here.
- Hierarchy: `MFPError` → domain-specific errors.
- `LintError` carries `.lint_output`; `ExecutionError` carries `.stderr` and `.exit_code`.
- Never raise `MFPError` directly — use a specific subclass.

### `config.py`
- `MFPConfig` is the single config object. Instantiate once in `__main__.py`.
- Pass it as a constructor argument — never read `os.environ` outside `vault.py` and `config.py`.
- `load_config()` is the only factory function.

### `models/__init__.py`
- **All** Pydantic models in one place. Do not split into separate files.
- Group by domain with section comments: `# Swagger models`, `# Execution models`, etc.
- `ResponseField` uses `nested: list[ResponseField] | None` for 1-level nesting only.

### `compiler/swagger_parser.py`
- Parses OpenAPI 3.x and Swagger 2.0.
- Resolves `$ref` **one level deep only** — skip anything deeper.
- Skips `oneOf`, `anyOf`, `allOf`, `discriminator` schemas (logs warning, does not fail).
- Auto-generates `operationId` when missing: `{method}_{sanitized_path}`.
- Parse each path+method via `_parse_operation()`. Path-level params merge with operation-level.
- Hash the raw document bytes with `hash_content()` for cache invalidation.

### `compiler/codegen.py`
- Pure function: `ServerSpec → str` (Python source code).
- Uses Jinja2 template `compiler/templates/function.py.j2`.
- Required params come before optional params in function signatures.
- Never hardcode auth values — env vars only.
- `_safe_name()` sanitizes parameter names to valid Python identifiers.

### `compiler/templates/function.py.j2`
- Generated file header: `# GENERATED BY MFP COMPILER — DO NOT EDIT`.
- Every generated function has a Google-style docstring listing parameters.
- Helper `_request()` function handles the actual `httpx.request()` call.
- `_headers()` injects auth from `os.environ`.

### `compiler/orchestrator.py`
- Loads swagger sources from `config/swaggers.yaml` via `load_swagger_sources()`.
- Checks `manifest.json` swagger hash before recompiling (skip if up-to-date).
- Writes `compiled/{server_name}/functions.py` + `manifest.json` + `__init__.py`.
- Runs `ruff check` on all generated files after compile.
- `--dry-run` mode parses only, writes nothing.

### `runtime/registry.py`
- Loads `compiled/*/manifest.json` at startup. Call `.load()` once.
- `list_servers()` → compact `list[ServerInfo]` (names + one-line summaries only).
- `get_function()` → full `FunctionInfo` with source code extracted via AST.
- `_extract_function_snippet()` uses `ast.parse()` to pull one function out of
  `functions.py` — falls back to full file on `SyntaxError`.

### `runtime/executor.py`
- **Full pipeline**: size check → AST guard → ruff lint → Docker → parse output → cache.
- `_detect_servers_used()` finds `from {name}.functions import` patterns via regex.
- `_build_execution_code()` prepends `sys.path.insert(0, compiled_dir)`.
- Docker container uses `container.attach_socket()` to write code via stdin.
- Output parsing: expects `{"success": bool, "data": ...}` JSON. Falls back to raw text.
- On timeout: `container.kill()` then raise `ExecutionTimeoutError`.
- Container is **always** removed in the `finally` block.

### `runtime/cache.py`
- Async SQLite via `aiosqlite`. Initialize with `await cache.initialize()`.
- Cache key = SHA256 of **normalized** code (strip trailing whitespace, skip blank lines).
- On duplicate key: increment `use_count`, update `last_used_at` (upsert).
- TTL check on `get()`: delete expired entries immediately on access.
- LRU eviction: delete oldest `last_used_at` entries when count > `max_entries`.
- `search()` filters by `description LIKE ?` and `ttl_seconds` validity in SQL.

### `security/ast_guard.py`
- `ASTGuard.validate(code, context)` raises `SecurityViolationError` on first violation.
- Maintains two sets: `_BLOCKED_MODULES` (frozenset) and `_ALLOWED_MODULES` (frozenset).
- Visitor pattern: `_SecurityVisitor` extends `ast.NodeVisitor`.
- Checks: `Import`, `ImportFrom`, `Call`, `Attribute`, `Global`, `Nonlocal` nodes.
- Logs the **violation type** only — never log the full user code.

### `security/vault.py`
- `build_server_env_vars(server_name)` reads `MFP_{SERVER}_BASE_URL` and `MFP_{SERVER}_AUTH`.
- `resolve_env_references(value)` expands `${VAR_NAME}` placeholders.
- Returns a plain `dict[str, str]` for Docker's `environment=` parameter.

### `server.py`
- `create_server(config)` returns a `FastMCP` instance.
- All 4 tools registered inside `create_server()` via `@mcp.tool()`.
- Registry and cache are created inside `create_server()` and closed over.
- Tool functions: always `async def`, always return `dict`, never raise.
- Error returns always include `"error_type"` key for programmatic handling.

### `__main__.py`
- CLI entry point: `mfp compile [--llm-enhance] [--dry-run]`
- CLI entry point: `mfp serve [--transport stdio|http] [--host] [--port]`
- CLI entry point: `mfp run` (compile + serve)
- `argparse` only — no click, no typer.
- `asyncio.run()` wraps all async commands.

---

## 8. Making Changes

### 8.1 Adding a New MCP Tool
1. Define the tool function in `server.py` using `@mcp.tool()`.
2. Add error handling covering every exception the tool can raise.
3. Add a unit test in `tests/unit/test_server.py` (create if absent).
4. Update `README.md` tool table.

### 8.2 Adding a New Model
1. Add to `src/mfp/models/__init__.py` under the correct section.
2. Write tests for model validation (optional fields, defaults, enum constraints).
3. Never duplicate a model — check existing ones first.

### 8.3 Extending the AST Guard
1. Add the new blocked pattern to the appropriate frozenset in `ast_guard.py`.
2. Add a test in `tests/unit/test_ast_guard.py` that confirms it raises.
3. If it is allowlisted, add a passing test too.

### 8.4 Adding a New Swagger Fixture
1. Create `tests/fixtures/{name}.yaml` (valid OpenAPI 3.x/Swagger 2.0).
2. Add a `SwaggerSource` fixture in `tests/conftest.py`.
3. Test it in `tests/unit/test_swagger_parser.py`.

### 8.5 Modifying Generated Code Template
1. Edit `src/mfp/compiler/templates/function.py.j2`.
2. Run `mfp compile` on a fixture swagger and confirm valid Python output.
3. Update `test_codegen.py` to assert the new structure.

### 8.6 Changing Cache Schema
1. Modify `_CREATE_TABLE_SQL` in `cache.py`.
2. Add a schema migration step (simple `ALTER TABLE` or recreate) detected
   by catching `aiosqlite.OperationalError` on startup.
3. Update `test_cache.py` to cover the new columns/indexes.

---

## 9. Commands Reference

```bash
# Install project (editable, all dev deps)
pip install -e ".[dev]"

# Run all tests with coverage
pytest

# Run unit tests only (fast, no Docker)
pytest tests/unit/ --no-cov -v

# Run integration tests
pytest tests/integration/ --no-cov -v

# Lint
ruff check src/ tests/

# Format check
ruff format --check src/ tests/

# Type check
mypy src/

# Compile swagger sources
mfp compile

# Compile without writing output (validation)
mfp compile --dry-run

# Start MCP server (stdio transport — for Claude Desktop)
mfp serve

# Start MCP server (HTTP transport)
mfp serve --transport http --port 8000

# Build sandbox Docker image (required before execute_code works)
docker build -t mfp-sandbox:latest sandbox/

# Create Docker network (required for sandbox networking)
docker network create mfp_network
```

---

## 10. What Agents Must Never Do

| Action | Why |
|--------|-----|
| Create new top-level directories | Breaks monorepo structure |
| Add `print()` statements | Use `structlog`; prints break stdio MCP transport |
| Hardcode credentials or tokens | Vault pattern must be used exclusively |
| Use `requests` or `urllib` | `httpx` only |
| Bypass the AST guard | The entire security model depends on it |
| Use `Optional[X]` or `Union[X, Y]` | Use `X | None` and `X | Y` (Python 3.10+) |
| Add models outside `models/__init__.py` | Single source of truth |
| Add exceptions outside `errors.py` | Single source of truth |
| Commit `.env` files | Use `.env.example` only |
| Run tests without `tmp_path` for DB | Tests must be fully isolated |
| Create markdown files not in spec | README, ROADMAP, AGENTS, CONTRIBUTING only |
| Use `time.sleep()` in async code | Use `await asyncio.sleep()` |
| Catch `Exception` without re-logging | Always `logger.exception(...)` first |

---

## 11. Definition of Done

A task is complete only when all of these are true:

- [ ] All new/modified functions have full type annotations
- [ ] All public functions have Google-style docstrings
- [ ] `ruff check src/ tests/` exits 0
- [ ] `mypy src/` exits 0
- [ ] `pytest --cov=mfp` exits 0 with ≥ 100% line coverage for changed modules
- [ ] No credentials appear in code, logs, or test assertions
- [ ] No `print()` statements added
- [ ] No new files created outside the defined structure (Section 2)
- [ ] `mfp compile --dry-run` succeeds if compiler was touched
- [ ] Security guard tests pass if `ast_guard.py` was touched
