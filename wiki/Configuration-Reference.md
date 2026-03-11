# Configuration Reference

MCE is configured entirely through environment variables (all prefixed `MCE_`) and a `swaggers.yaml` file. No config files are edited at runtime.

---

## Environment Variables

### General

| Variable | Default | Description |
|----------|---------|-------------|
| `MCE_LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `MCE_DEBUG` | `false` | Enable debug mode (verbose output, stack traces) |
| `MCE_HOST` | `0.0.0.0` | Bind host for HTTP transport |
| `MCE_PORT` | `8000` | Port for HTTP transport |

### Compiler

| Variable | Default | Description |
|----------|---------|-------------|
| `MCE_COMPILE_ON_STARTUP` | `true` | Automatically re-compile Swagger sources when the server starts |
| `MCE_COMPILED_OUTPUT_DIR` | `./compiled` | Directory where compiled `functions.py` and manifests are written |
| `MCE_SWAGGER_CONFIG_FILE` | `./config/swaggers.yaml` | Path to the Swagger source definitions file |
| `MCE_LLM_ENHANCE` | `false` | Send generated functions through an LLM to improve docstrings |
| `MCE_LLM_MODEL` | `gemini/gemini-2.0-flash` | LiteLLM model string: `provider/model` |
| `MCE_LLM_API_KEY` | — | API key for the LLM provider (required when `MCE_LLM_ENHANCE=true`) |

### Executor

| Variable | Default | Description |
|----------|---------|-------------|
| `MCE_LINT_ENABLED` | `false` | Run ruff lint on generated code before sandbox execution |
| `MCE_DOCKER_IMAGE` | `mce-sandbox:latest` | Docker image for the execution sandbox |
| `MCE_DOCKER_HOST` | — | Docker socket path (e.g. `unix:///var/run/docker.sock`) |
| `MCE_EXECUTION_TIMEOUT_SECONDS` | `30` | Maximum wall-clock time allowed per execution |
| `MCE_MAX_OUTPUT_SIZE_BYTES` | `1048576` | Maximum sandbox stdout size (1 MB). Larger output is truncated. |
| `MCE_NETWORK_MODE` | `mce_network` | Docker network the sandbox container is attached to |

### Cache

| Variable | Default | Description |
|----------|---------|-------------|
| `MCE_CACHE_ENABLED` | `true` | Enable the SQLite code cache |
| `MCE_CACHE_TTL_SECONDS` | `3600` | How long a cache entry lives before expiry (1 hour) |
| `MCE_CACHE_MAX_ENTRIES` | `500` | Maximum entries; oldest entries are evicted (LRU) when the limit is exceeded |
| `MCE_CACHE_DB_PATH` | `./data/cache.db` | Path to the SQLite cache database file |

### Security

| Variable | Default | Description |
|----------|---------|-------------|
| `MCE_MAX_CODE_SIZE_BYTES` | `65536` | Maximum code payload accepted (64 KB). Larger code is rejected before parsing. |
| `MCE_ALLOWED_DOMAINS` | — | Comma-separated list of allowed API hostnames. Empty = allow all. Example: `api.weather.com,api.hotel.com` |

### Per-Server Credentials

These are resolved at execution time and injected as Docker environment variables — they are **never written to code or logs**.

| Variable pattern | Description |
|------------------|-------------|
| `MCE_{SERVER}_BASE_URL` | API base URL for this server |
| `MCE_{SERVER}_AUTH` | Full auth header value, e.g. `Authorization: Bearer sk-...` |
| `MCE_{SERVER}_EXTRA_HEADERS` | JSON object of custom headers, e.g. `{"X-Version":"v1"}` |

`{SERVER}` is the uppercase version of the `name` field in `swaggers.yaml`. For a server named `hotel_booking`, the variables are `MCE_HOTEL_BOOKING_BASE_URL`, `MCE_HOTEL_BOOKING_AUTH`, etc.

---

## Custom .env Files

By default MCE loads `.env` from the current working directory. Override with `--env-file` on any subcommand:

```bash
mce compile --env-file /path/to/.env.production
mce serve   --env-file /path/to/.env.staging
mce run     --env-file /path/to/.env.local
mce clean   --env-file /path/to/.env.local
```

Explicit environment variables always take precedence over values in the `.env` file.

---

## Swagger Config (`config/swaggers.yaml`)

```yaml
servers:
  - name: weather
    swagger_url: "https://api.weather.example.com/v1/openapi.json"
    base_url: "https://api.weather.example.com/v1"
    auth_header: "Bearer ${WEATHER_API_KEY}"   # ${VAR} resolves from env at runtime
    is_read_only: true                          # strips POST/PUT/PATCH/DELETE at compile time
    extra_headers:
      X-API-Version: "v1"
      X-Custom-Header: "value"

  - name: hotel_booking
    swagger_url: "./swaggers/hotel.yaml"        # local file path — relative to CWD
    base_url: "https://api.hotel.example.com/v2"
    auth_header: "Bearer ${HOTEL_API_TOKEN}"
    is_read_only: false
```

### Field Reference

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `name` | Yes | string | Unique identifier. Becomes the Python module name and the `{SERVER}` segment in env vars. Must be a valid Python identifier. |
| `swagger_url` | Yes | string | HTTP(S) URL or local file path to the OpenAPI 3.x or Swagger 2.0 spec (JSON or YAML). |
| `base_url` | Yes | string | API base URL. Injected at runtime via `MCE_{SERVER}_BASE_URL`; never exposed to LLMs. |
| `auth_header` | No | string | Full authorization header. Use `${VAR}` placeholders — never hardcode secrets. If omitted, no auth header is sent. |
| `is_read_only` | No | bool | When `true`, MCE strips all endpoints with HTTP methods `POST`, `PUT`, `PATCH`, and `DELETE` at compile time. Default: `false`. |
| `extra_headers` | No | map | Key-value pairs of custom HTTP headers added to every function call. Serialized to `MCE_{SERVER}_EXTRA_HEADERS`. |

### `${VAR}` Placeholder Resolution

Auth headers and extra header values can reference environment variables using `${VAR_NAME}` syntax:

```yaml
auth_header: "Bearer ${MY_API_TOKEN}"
extra_headers:
  X-Org-Id: "${MY_ORG_ID}"
```

These placeholders are resolved at **runtime** — not at compile time. The generated `functions.py` contains only `os.environ` references; the real values are injected via Docker environment variables when code executes.

---

## LLM Enhancement

When `MCE_LLM_ENHANCE=true`, the compiler sends each generated `functions.py` through an LLM to improve docstrings and add usage examples. Credentials are **not** sent — only the code skeleton with `os.environ[...]` placeholders.

Requires the `[llm]` extra:

```bash
pip install -e ".[llm]"
```

Supported providers (via LiteLLM):

```env
MCE_LLM_MODEL=openai/gpt-4o
MCE_LLM_MODEL=anthropic/claude-3-5-sonnet-20241022
MCE_LLM_MODEL=gemini/gemini-2.0-flash          # default
MCE_LLM_MODEL=openrouter/mistralai/mistral-7b-instruct
```

---

## Complete `.env.example`

```env
# General
MCE_LOG_LEVEL=INFO
MCE_DEBUG=false

# HTTP transport (only used with --transport http)
MCE_HOST=0.0.0.0
MCE_PORT=8000

# Compiler
MCE_COMPILE_ON_STARTUP=true
MCE_COMPILED_OUTPUT_DIR=./compiled
MCE_SWAGGER_CONFIG_FILE=./config/swaggers.yaml
MCE_LLM_ENHANCE=false
# MCE_LLM_MODEL=gemini/gemini-2.0-flash
# MCE_LLM_API_KEY=your-llm-api-key

# Executor
MCE_LINT_ENABLED=false
MCE_DOCKER_IMAGE=mce-sandbox:latest
# MCE_DOCKER_HOST=unix:///var/run/docker.sock
MCE_EXECUTION_TIMEOUT_SECONDS=30
MCE_MAX_OUTPUT_SIZE_BYTES=1048576
MCE_NETWORK_MODE=mce_network

# Cache
MCE_CACHE_ENABLED=true
MCE_CACHE_TTL_SECONDS=3600
MCE_CACHE_MAX_ENTRIES=500
MCE_CACHE_DB_PATH=./data/cache.db

# Security
MCE_MAX_CODE_SIZE_BYTES=65536
# MCE_ALLOWED_DOMAINS=api.example.com,api.other.com

# Per-server credentials (replace MYAPI with your server name, uppercase)
MCE_MYAPI_BASE_URL=https://api.example.com/v1
MCE_MYAPI_AUTH=Authorization: Bearer YOUR_TOKEN_HERE
# MCE_MYAPI_EXTRA_HEADERS={"X-Version":"v1"}
```

---

*Next: [Security Model](Security-Model) →*
