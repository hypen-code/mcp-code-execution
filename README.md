# MFP — ModelFunctionProtocol

> **APIs were designed for developers. MFP recompiles them for AI.**

[![CI](https://github.com/your-org/mfp/actions/workflows/ci.yml/badge.svg)](https://github.com/your-org/mfp/actions)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## The Problem

1. **Context window bloat** — Naive Swagger-to-MCP tools expose every API endpoint as a separate tool. A 200-endpoint API burns hundreds of tokens per call just describing tools the LLM will never use.
2. **Tool processing limits** — MCP clients cap tool counts. Large APIs hit the limit and fail silently.
3. **Insecure execution** — Running LLM-generated code on the host is dangerous. You need isolation.

## The Solution

MFP exposes **4 meta-tools** instead of N API-specific tools:

```
list_servers     → discover available APIs
get_function     → inspect a specific function's signature
execute_code     → run Python in a sandboxed Docker container
get_cached_code  → reuse previously successful code
```

The LLM workflow: **discover → inspect → generate → execute → cache → reuse**

```
┌─────────────────────────────────────────────────────┐
│                   MFP MCP Server                     │
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
│  │           4 MCP Tools (exposed to LLM)         │  │
│  │  list_servers | get_function | execute_code    │  │
│  │  get_cached_code                               │  │
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
git clone https://github.com/your-org/mfp.git
cd mfp
pip install -e .
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your API credentials

cp config/swaggers.yaml config/my_swaggers.yaml
# Edit to point at your swagger URLs
```

### 3. Build the Sandbox

```bash
docker build -t mfp-sandbox:latest sandbox/
docker network create mfp_network
```

### 4. Compile Swagger Sources

```bash
mfp compile
# ✅ Compiled: weather, hotel_booking (12 endpoints)
```

### 5. Run the MCP Server

```bash
# stdio mode (for Claude Desktop, Cursor, etc.)
mfp serve

# HTTP mode
mfp serve --transport http --port 8000
```

### 6. Connect to Your MCP Client

Add to your `mcp_servers.json` (Claude Desktop example):

```json
{
  "mcpServers": {
    "mfp": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--env-file", "/path/to/.env",
        "-v", "/path/to/compiled:/app/compiled",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "--network", "mfp_network",
        "mfp-server:latest",
        "mfp", "serve"
      ]
    }
  }
}
```

## How It Works

### Tool Workflow Example

```
LLM → list_servers()
← { servers: [{ name: "weather", functions: ["get_current_weather", "get_weather_forecast"] }] }

LLM → get_function("weather", "get_current_weather")
← { parameters: [{ name: "city", type: "str", required: true }], usage_example: "..." }

LLM → execute_code("""
from weather.functions import get_current_weather
result = get_current_weather(city="London", units="metric")
""", description="Get London weather")
← { success: true, data: { temperature: 15.2, condition: "Cloudy" }, cache_id: "abc123" }

LLM → get_cached_code(search="weather")
← { cached_entries: [{ id: "abc123", description: "Get London weather", use_count: 1 }] }
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MFP_LOG_LEVEL` | `INFO` | Log verbosity |
| `MFP_DOCKER_IMAGE` | `mfp-sandbox:latest` | Sandbox image name |
| `MFP_EXECUTION_TIMEOUT_SECONDS` | `30` | Max code execution time |
| `MFP_CACHE_ENABLED` | `true` | Enable code caching |
| `MFP_CACHE_TTL_SECONDS` | `3600` | Cache entry lifetime |
| `MFP_COMPILED_OUTPUT_DIR` | `./compiled` | Compiled functions directory |
| `MFP_{SERVER}_BASE_URL` | — | API base URL per server |
| `MFP_{SERVER}_AUTH` | — | Auth header per server |

### Swagger Config (`config/swaggers.yaml`)

```yaml
servers:
  - name: weather
    swagger_url: "https://api.weather.example.com/v1/openapi.json"
    base_url: "https://api.weather.example.com/v1"
    auth_header: "${WEATHER_API_KEY}"   # Resolved from env
    is_read_only: true
```

## Security

MFP uses a **defense-in-depth** approach:

1. **AST Security Guard** — Statically analyzes LLM-generated code before execution. Blocks dangerous imports (`os`, `sys`, `subprocess`, `socket`) and calls (`eval`, `exec`, `open`, `__import__`).

2. **Docker Sandbox** — Code runs in an isolated `python:3.13-slim` container:
   - Non-root user (`executor`)
   - Memory limit: 256MB
   - CPU quota: 50% of one core  
   - No host volume mounts
   - Read-only filesystem (except `/tmp`)
   - Execution timeout

3. **Credential Injection** — API credentials are injected as Docker environment variables. They never appear in generated code, logs, or tool responses.

4. **Read-Only Enforcement** — Servers marked `is_read_only: true` have POST/PUT/PATCH/DELETE endpoints excluded at compile time.

## Examples

See [`examples/`](examples/) for demo scripts and swagger configs.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
