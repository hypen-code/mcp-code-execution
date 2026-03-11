# Getting Started

This guide walks you from zero to a running MCE instance connected to your MCP client.

---

## Prerequisites

| Tool | Minimum version | Notes |
|------|----------------|-------|
| Python | **3.13** | Required; earlier versions are not supported |
| Docker | **24.0** | Required for sandbox execution |
| Git | 2.40 | For cloning and contributing |
| pre-commit | 3.x | Required only for contributors |

---

## Step 1 — Clone and Install

```bash
git clone https://github.com/hypen-code/mcp-code-execution.git
cd mcp-code-execution

# Core install
pip install -e "."

# With dev tools (pytest, ruff, mypy, pre-commit)
pip install -e ".[dev]"

# Optional: LLM-enhanced docstring generation
pip install -e ".[llm]"
```

---

## Step 2 — Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and fill in values relevant to your APIs. Every variable is prefixed with `MCE_`. See the full [Configuration Reference](Configuration-Reference) for all options.

**Minimum required** to run the server:

```env
# Required if Docker is not on the default socket
# MCE_DOCKER_HOST=unix:///var/run/docker.sock

# Credentials per API server (replace MYAPI with your server name in swaggers.yaml)
MCE_MYAPI_BASE_URL=https://api.example.com/v1
MCE_MYAPI_AUTH=Authorization: Bearer YOUR_TOKEN_HERE
```

> Never commit your `.env` file. It is already in `.gitignore`.

---

## Step 3 — Configure Swagger Sources

```bash
cp config/swaggers.yaml.example config/swaggers.yaml
```

Edit `config/swaggers.yaml`:

```yaml
servers:
  - name: weather
    swagger_url: "https://api.weather.example.com/v1/openapi.json"
    base_url: "https://api.weather.example.com/v1"
    auth_header: "Bearer ${WEATHER_API_KEY}"   # resolves from env at runtime
    is_read_only: true                          # strips POST/PUT/PATCH/DELETE

  - name: hotel_booking
    swagger_url: "./swaggers/hotel.yaml"        # local files are supported
    base_url: "https://api.hotel.example.com/v2"
    auth_header: "Bearer ${HOTEL_API_TOKEN}"
    is_read_only: false
```

**Key fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique server identifier. Used in import paths and env var names. |
| `swagger_url` | Yes | HTTP URL or local file path to the OpenAPI/Swagger spec. |
| `base_url` | Yes | API base URL injected at runtime (never exposed to LLMs). |
| `auth_header` | No | Full header value, e.g. `Bearer ${VAR}`. Omit for public APIs. |
| `is_read_only` | No | `true` strips all mutating endpoints at compile time. |
| `extra_headers` | No | Map of custom HTTP headers added to every request. |

> Use `${VAR_NAME}` placeholders in `auth_header` — never paste raw secrets into the YAML file.

---

## Step 4 — Build the Docker Sandbox

```bash
docker build -t mce-sandbox:latest sandbox/
docker network create mce_network
```

The sandbox image is a hardened `python:3.13-slim` container with:
- Non-root user (`executor`, UID 1000)
- Read-only filesystem (except `/tmp`)
- Memory limit: 256 MB
- CPU quota: 50% of one core

---

## Step 5 — Compile Swagger Sources

```bash
mce compile
```

MCE parses each entry in `swaggers.yaml`, generates a `functions.py` with typed wrappers, and writes a `manifest.json` to `./compiled/<server-name>/`.

```
✅ Compiled: weather (8 endpoints)
✅ Compiled: hotel_booking (24 endpoints)
--- MCP Server Config (add to your MCP client) ---
{ ... ready-to-use config snippet ... }
```

Additional compile flags:

```bash
# Improve docstrings with an LLM (requires [llm] extra and MCE_LLM_API_KEY)
mce compile --llm-enhance

# Validate parsing without writing output
mce compile --dry-run

# Remove compiled output, then recompile from scratch
mce clean compile

# Use a different .env file
mce compile --env-file /path/to/.env.production
```

---

## Step 6 — Start the MCP Server

```bash
# stdio mode — for Claude Desktop, Cursor, and most MCP clients
mce serve

# HTTP mode — for web-based or custom integrations
mce serve --transport http --port 8000

# Compile + serve in one command
mce run

# With a specific environment file
mce serve --env-file /path/to/.env.staging
```

---

## Step 7 — Connect Your MCP Client

`mce compile` prints a ready-to-use config snippet. For **Claude Desktop**, add to `mcp_servers.json`:

```json
{
  "mcpServers": {
    "mcp-code-execution": {
      "command": "/path/to/mcp-code-execution/.venv/bin/mce",
      "args": ["serve"],
      "env": {
        "MCE_COMPILED_OUTPUT_DIR": "/path/to/mcp-code-execution/compiled",
        "MCE_SWAGGER_CONFIG_FILE": "/path/to/mcp-code-execution/config/swaggers.yaml",
        "MCE_DOCKER_IMAGE": "mce-sandbox:latest",
        "MCE_NETWORK_MODE": "mce_network",
        "MCE_CACHE_DB_PATH": "/path/to/mcp-code-execution/data/cache.db"
      }
    }
  }
}
```

Restart your MCP client. You should now see MCE tools available.

---

## Verify the Setup

Once connected, ask the LLM to call `list_servers()`. A successful response looks like:

```json
{
  "sandbox_libraries": ["httpx", "pydantic", "orjson"],
  "servers": [
    {
      "name": "weather",
      "functions": ["get_current_weather", "get_forecast", "..."]
    }
  ]
}
```

If `servers` is empty, the compile step may not have run or the output directory is misconfigured. Check `MCE_COMPILED_OUTPUT_DIR`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `mce: command not found` | Package not installed in active env | `pip install -e "."` in the project directory |
| `Docker not available` | Docker daemon not running | `sudo systemctl start docker` |
| Empty `list_servers()` | `compiled/` directory empty or wrong path | Run `mce compile`, verify `MCE_COMPILED_OUTPUT_DIR` |
| `SecurityViolationError` | Generated code uses a blocked module | See [Security Model](Security-Model) for blocked patterns |
| Container timeout | API response too slow | Increase `MCE_EXECUTION_TIMEOUT_SECONDS` in `.env` |
| Auth failure in sandbox | Wrong env var name | Env var must match `MCE_{SERVER_NAME}_AUTH` where `SERVER_NAME` is uppercase |

---

*Next: [MCP Tools Reference](MCP-Tools-Reference) →*
