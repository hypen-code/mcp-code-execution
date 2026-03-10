# MCE — MCP Code Execution

> **APIs were designed for developers. MCE recompiles them for AI.**

[![CI](https://github.com/hypen-code/mcp-code-execution/actions/workflows/ci.yml/badge.svg)](https://github.com/hypen-code/mcp-code-execution/actions)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## The Problem

1. **Context window bloat** — Naive Swagger-to-MCP tools expose every API endpoint as a separate tool. A 200-endpoint API burns hundreds of tokens per call just describing tools the LLM will never use.
2. **Tool processing limits** — MCP clients cap tool counts. Large APIs hit the limit and fail silently.
3. **Insecure execution** — Running LLM-generated code on the host is dangerous. You need isolation.
4. **Bloated responses** — Raw API responses dump everything: metadata, nulls, pagination envelopes, deprecated fields. The LLM sees 90% noise and wastes context on data it never needed.
5. **Integration friction** — Every API with a Swagger spec should be instantly usable by an LLM. Instead, developers spend days writing glue code, auth wrappers, and prompt scaffolding just to call a single endpoint.

## The Solution

MCE exposes **5 meta-tools + 1 prompt** instead of N API-specific tools:

```
list_servers        → discover available APIs and their functions
get_functions       → inspect 1–5 function signatures and return schemas (batch)
execute_code        → run Python in a sandboxed Docker container
get_cached_code     → search previously successful code snippets
run_cached_code     → re-execute a cached snippet, optionally with new parameters

reusable_code_guide → prompt: concise rules for writing parameterized, cacheable code
```

The LLM workflow: **discover → inspect → generate → execute → cache → reuse**

```
┌─────────────────────────────────────────────────────┐
│                   MCE MCP Server                     │
│                                                     │
│  ┌───────────┐  ┌───────────┐  ┌────────────────┐  │
│  │  Compiler  │  │  Runtime   │  │  Code Executor │  │
│  │  (setup)   │  │  (serve)   │  │  (Docker SDK)  │  │
│  └─────┬─────┘  └─────┬─────┘  └───────┬────────┘  │
│        │              │                 │            │
│  ┌─────▼──────────────▼─────────────────▼────────┐  │
│  │              Core Services                     │  │
│  │  SwaggerParser | FunctionRegistry | CacheStore │  │
│  │  SecurityGuard | CredentialVault               │  │
│  └───────────────────────────────────────────────┘  │
│                                                     │
│  ┌───────────────────────────────────────────────┐  │
│  │           5 MCP Tools (exposed to LLM)         │  │
│  │  list_servers | get_functions | execute_code   │  │
│  │  get_cached_code | run_cached_code             │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
         │                            │
         ▼                            ▼
   ┌───────────┐            ┌──────────────────┐
   │  Swagger   │            │  python:3.13-slim │
   │  Sources   │            │  Docker Container  │
   └───────────┘            └──────────────────┘
```

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/hypen-code/mcp-code-execution.git
cd mcp-code-execution
pip install -e ".[dev]"

# Optional: LLM-enhanced compilation (OpenAI, Gemini, Anthropic, OpenRouter)
pip install -e ".[llm]"
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your API credentials

cp config/swaggers.yaml.example config/swaggers.yaml
# Edit to point at your swagger URLs
```

### 3. Build the Sandbox

```bash
docker build -t mce-sandbox:latest sandbox/
docker network create mce_network
```

### 4. Compile Swagger Sources

```bash
mce compile
# ✅ Compiled: weather, hotel_booking (12 endpoints)
# --- MCP Server Config (add to your MCP client) ---
# { ... ready-to-use config snippet ... }

# Optional: enhance docstrings and examples with an LLM
mce compile --llm-enhance

# Validate without writing output
mce compile --dry-run

# Remove compiled output and recompile
mce clean compile
```

### 5. Run the MCP Server

```bash
# stdio mode (for Claude Desktop, Cursor, etc.)
mce serve

# HTTP mode
mce serve --transport http --port 8000

# Compile + serve in one command
mce run

# Use a custom .env file (works with all subcommands)
mce serve --env-file /path/to/.env.production
mce run --env-file /path/to/.env.staging
```

### 6. Connect to Your MCP Client

Add to your `mcp_servers.json` (Claude Desktop example):

```json
{
  "mcpServers": {
    "mcp-code-execution": {
      "command": "~/mcp-code-execution/.venv/bin/mce",
      "args": ["serve"],
      "env": {
        "MCE_COMPILED_OUTPUT_DIR": "~/mcp-code-execution/compiled",
        "MCE_SWAGGER_CONFIG_FILE": "~/mcp-code-execution/config/swaggers.yaml",
        "MCE_DOCKER_IMAGE": "mce-sandbox:latest",
        "MCE_NETWORK_MODE": "mce_network",
        "MCE_CACHE_DB_PATH": "~/mcp-code-execution/data/cache.db"
      }
    }
  }
}
```

> `mce compile` prints a ready-to-use config snippet you can paste directly.

## How It Works

### Tool Workflow Example

```
LLM → list_servers()
← { sandbox_libraries: [...], servers: [{ name: "weather", functions: [{ name: "get_current_weather", summary: "..." }] }] }

LLM → get_functions([{"server_name": "weather", "function_name": "get_current_weather"}])
← { functions: [{ parameters: [...], response_fields: [...], import_statement: "from weather.functions import get_current_weather" }] }

LLM → execute_code("""
from weather.functions import get_current_weather

def main():
    return get_current_weather(city="London", units="metric")
""", description="Get London weather")
← { success: true, data: { temperature: 15.2, condition: "Cloudy" }, cache_id: "abc123" }

LLM → get_cached_code(search="weather")
← { cached_entries: [{ id: "abc123", description: "Get London weather", use_count: 1 }] }

LLM → run_cached_code("abc123", params={"city": "Paris"})
← { success: true, data: { temperature: 18.5, condition: "Sunny" }, cache_id: "def456" }
```

> **`get_functions` must be called before writing any `execute_code` payload.** It returns the exact `import_statement`, parameter names, and response schema. Guessing will produce broken code.

> **`execute_code` requires** either a `main()` function that returns the result, or a module-level `result` variable.

## Configuration

### Custom `.env` File

By default, MCE loads `.env` from the current working directory. You can override this with the `--env-file` flag on any subcommand:

```bash
mce compile --env-file /path/to/.env.production
mce serve   --env-file /path/to/.env.staging
mce run     --env-file /path/to/.env.local
mce clean   --env-file /path/to/.env.local
```

Explicit environment variables always take precedence over values in the `.env` file.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MCE_LOG_LEVEL` | `INFO` | Log verbosity |
| `MCE_DEBUG` | `false` | Enable debug mode |
| `MCE_HOST` | `0.0.0.0` | HTTP server bind host |
| `MCE_PORT` | `8000` | HTTP server port |
| `MCE_COMPILE_ON_STARTUP` | `true` | Auto-compile swagger sources at startup |
| `MCE_COMPILED_OUTPUT_DIR` | `./compiled` | Compiled functions directory |
| `MCE_SWAGGER_CONFIG_FILE` | `./config/swaggers.yaml` | Swagger source definitions |
| `MCE_LLM_ENHANCE` | `false` | Enable LLM docstring enhancement at compile time |
| `MCE_LLM_MODEL` | `gemini/gemini-2.0-flash` | LiteLLM model string (`provider/model`) |
| `MCE_LLM_API_KEY` | — | API key for the LLM provider |
| `MCE_LINT_ENABLED` | `false` | Enable ruff lint validation before sandbox execution |
| `MCE_DOCKER_IMAGE` | `mce-sandbox:latest` | Sandbox image name |
| `MCE_DOCKER_HOST` | — | Docker host socket (e.g. `unix:///var/run/docker.sock`) |
| `MCE_EXECUTION_TIMEOUT_SECONDS` | `30` | Max code execution time |
| `MCE_MAX_OUTPUT_SIZE_BYTES` | `1048576` | Max sandbox stdout size (1 MB) |
| `MCE_NETWORK_MODE` | `mce_network` | Docker network for sandbox containers |
| `MCE_CACHE_ENABLED` | `true` | Enable code caching |
| `MCE_CACHE_TTL_SECONDS` | `3600` | Cache entry lifetime |
| `MCE_CACHE_MAX_ENTRIES` | `500` | Maximum cached entries before LRU eviction |
| `MCE_CACHE_DB_PATH` | `./data/cache.db` | SQLite cache database path |
| `MCE_MAX_CODE_SIZE_BYTES` | `65536` | Maximum allowed code size (64 KB) |
| `MCE_ALLOWED_DOMAINS` | — | Comma-separated API domain allowlist (empty = allow all) |
| `MCE_{SERVER}_BASE_URL` | — | API base URL per server |
| `MCE_{SERVER}_AUTH` | — | Auth header per server (e.g. `Authorization: Bearer <token>`) |
| `MCE_{SERVER}_EXTRA_HEADERS` | — | JSON object of custom HTTP headers per server (e.g. `{"X-Version":"v1"}`) |

### Swagger Config (`config/swaggers.yaml`)

```yaml
servers:
  - name: weather
    swagger_url: "https://api.weather.example.com/v1/openapi.json"
    base_url: "https://api.weather.example.com/v1"
    auth_header: "${WEATHER_API_KEY}"   # Resolved from env
    is_read_only: true                  # Omit POST/PUT/PATCH/DELETE at compile time
    extra_headers:                      # Optional: custom headers injected on every request
      X-API-Version: "v1"
      X-Custom-Header: "value"

  - name: hotel_booking
    swagger_url: "./swaggers/hotel.yaml"   # Local file paths are supported
    base_url: "https://api.hotel.example.com/v2"
    auth_header: "Bearer ${HOTEL_API_TOKEN}"
    is_read_only: false
```

> If `auth_header` is omitted, the server is treated as a public API — no auth header is injected.

> `extra_headers` are serialized to `MCE_{SERVER}_EXTRA_HEADERS` (JSON string) at compile time and injected into every generated function call.

### LLM Enhancement (Optional)

When `MCE_LLM_ENHANCE=true`, the compiler sends each generated function through an LLM to improve docstrings and add usage examples. Requires the `[llm]` extra and a valid `MCE_LLM_API_KEY`.

```bash
pip install -e ".[llm]"

# Supports any LiteLLM-compatible provider:
MCE_LLM_MODEL=openai/gpt-4o           # OpenAI
MCE_LLM_MODEL=anthropic/claude-3-5-sonnet-20241022  # Anthropic
MCE_LLM_MODEL=gemini/gemini-2.0-flash # Google Gemini (default)
MCE_LLM_MODEL=openrouter/mistralai/mistral-7b-instruct  # OpenRouter
```

## Security

MCE uses a **defense-in-depth** approach:

1. **Code Size Limit** — Code exceeding `MCE_MAX_CODE_SIZE_BYTES` (default 64 KB) is rejected before any analysis begins.

2. **AST Security Guard** — Statically analyzes LLM-generated code before execution. Blocks dangerous imports (`os`, `sys`, `subprocess`, `socket`) and calls (`eval`, `exec`, `open`, `__import__`).

3. **Ruff Lint Gate** — When `MCE_LINT_ENABLED=true`, generated code is linted before entering the sandbox. Syntactically invalid or style-violating code is rejected with actionable feedback.

4. **Docker Sandbox** — Code runs in an isolated `python:3.13-slim` container:
   - Non-root user (`executor`)
   - Memory limit: 256 MB
   - CPU quota: 50% of one core
   - No host volume mounts
   - Read-only filesystem (except `/tmp`)
   - Execution timeout

5. **Credential Injection** — API credentials are injected as Docker environment variables. They never appear in generated code, logs, or tool responses.

6. **Read-Only Enforcement** — Servers marked `is_read_only: true` have POST/PUT/PATCH/DELETE endpoints excluded at compile time.

7. **Domain Allowlist** — When `MCE_ALLOWED_DOMAINS` is set, requests to any hostname outside the list are rejected.

## Development

```bash
# Install all dev dependencies
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

# Pre-commit hooks (runs ruff + mypy + pytest ≥90% coverage)
pre-commit install
pre-commit run --all-files
```

Coverage gate: **≥ 90%** (`--cov-fail-under=90`) — enforced by the pre-commit hook on every commit.

## Examples

See [`examples/`](examples/) for demo scripts and swagger configs.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contribution guide (setup, workflow, PR checklist, coding standards).

For the AI agent development guide and internal coding conventions, see [AGENTS.md](AGENTS.md).

## License

MIT — see [LICENSE](LICENSE).
