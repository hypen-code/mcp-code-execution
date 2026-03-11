# Compiler Pipeline

The MCE compiler converts Swagger/OpenAPI specifications into typed Python modules that the runtime can load and the LLM can call. This page explains each stage of the pipeline and the output it produces.

---

## Pipeline Stages

```
swaggers.yaml
      │
      ▼
 1. Source Loading       ← read YAML, resolve ${VAR} placeholders
      │
      ▼
 2. Change Detection     ← hash spec content, skip if unchanged
      │
      ▼
 3. Swagger Parsing      ← parse JSON/YAML spec, resolve $refs, normalize
      │
      ▼
 4. Code Generation      ← Jinja2 template → functions.py
      │
      ▼
 5. LLM Enhancement      ← optional: improve docstrings via LiteLLM
      │
      ▼
 6. Ruff Validation      ← lint generated code
      │
      ▼
 7. Manifest Writing     ← manifest.json + __init__.py
      │
      ▼
compiled/{server-name}/
  ├── functions.py
  ├── manifest.json
  └── __init__.py
```

---

## Stage 1 — Source Loading

The compiler reads `config/swaggers.yaml` (or the path in `MCE_SWAGGER_CONFIG_FILE`) and validates each entry against the `SwaggerSource` model.

`${VAR_NAME}` placeholders in `auth_header` and `extra_headers` values are **not** resolved at compile time — they remain as literal strings in the generated code. Resolution happens at execution time via the credential vault.

---

## Stage 2 — Change Detection

Before parsing, MCE computes a SHA-256 hash of the swagger spec content (downloaded or read from disk) and compares it against the hash stored in the existing `manifest.json` (if present). If the hashes match, the server is skipped:

```
✅ weather — up to date (skipped)
🔄 hotel_booking — spec changed, recompiling
```

This makes `mce compile` idempotent and fast — only changed specs are reprocessed.

---

## Stage 3 — Swagger Parsing

`swagger_parser.py` handles both **OpenAPI 3.x** and **Swagger 2.0** specs in JSON or YAML format.

### What the parser does:

- **`$ref` resolution** — Resolves one level of `$ref` for request bodies and response schemas. Deep nesting is flattened.
- **`operationId` generation** — If an endpoint lacks an `operationId`, one is auto-generated from the HTTP method and path: `GET /users/{id}` → `get_users_by_id`.
- **Safe identifier normalization** — Non-identifier characters (`@`, `-`, spaces) are replaced with `_`.
- **Read-only filtering** — If `is_read_only: true`, endpoints with methods `POST`, `PUT`, `PATCH`, `DELETE` are excluded from the parsed output.
- **Parameter normalization** — Path, query, header, and body parameters are unified into a flat list with types, defaults, and descriptions.
- **Response schema extraction** — Parses the `200`/`201` response schema into a list of `ResponseField` objects used to generate `TypedDict` return types.

### Output

The parser produces a `ServerSpec` object containing:

```python
class ServerSpec:
    name: str
    base_url: str
    auth_header: str
    extra_headers: dict[str, str]
    endpoints: list[EndpointSpec]

class EndpointSpec:
    operation_id: str       # Python function name
    method: str             # GET, POST, etc.
    path: str               # /users/{id}
    summary: str            # one-line description
    parameters: list[ParamSchema]
    response_fields: list[ResponseField]
```

---

## Stage 4 — Code Generation

`codegen.py` uses a **Jinja2 template** (`src/mce/compiler/templates/function.py.j2`) to render the `ServerSpec` into a valid Python module.

### Generated File Structure

```python
# compiled/weather/functions.py  (generated — do not edit)
from __future__ import annotations
import os
import json
from typing import Any
from typing_extensions import TypedDict
import httpx

# Auth and headers are read from env vars at import time
_AUTH_HEADER: str = os.environ.get("MCE_WEATHER_AUTH", "")
_BASE_URL: str    = os.environ.get("MCE_WEATHER_BASE_URL", "")
_EXTRA_HEADERS: dict[str, str] = json.loads(
    os.environ.get("MCE_WEATHER_EXTRA_HEADERS", "{}")
)

def _headers() -> dict[str, str]:
    """Build request headers from environment."""
    headers: dict[str, str] = {**_EXTRA_HEADERS}
    if _AUTH_HEADER:
        key, _, value = _AUTH_HEADER.partition(":")
        headers[key.strip()] = value.strip()
    return headers

def _request(method: str, path: str, **kwargs: Any) -> Any:
    """Execute an HTTP request against the base URL."""
    response = httpx.request(
        method,
        f"{_BASE_URL}{path}",
        headers=_headers(),
        timeout=30.0,
        **kwargs,
    )
    response.raise_for_status()
    return response.json()

# --- Generated TypedDict return types ---

class GetCurrentWeatherResponse(TypedDict, total=False):
    temperature: float
    condition: str
    humidity: int
    wind_speed: float

# --- Generated function wrappers ---

def get_current_weather(
    city: str,
    units: str = "metric",
) -> GetCurrentWeatherResponse:
    """Get current weather conditions for a city.

    Args:
        city: City name, e.g. 'London'.
        units: Temperature units: metric | imperial. Defaults to 'metric'.

    Returns:
        GetCurrentWeatherResponse with temperature, condition, humidity, wind_speed.
    """
    return _request(
        "GET",
        "/weather/current",
        params={"city": city, "units": units},
    )
```

### Code Generation Rules

| Feature | Behavior |
|---------|----------|
| Type annotations | Inferred from Swagger types (`string` → `str`, `integer` → `int`, `number` → `float`, `boolean` → `bool`, `array` → `list[Any]`, `object` → `dict[str, Any]`) |
| Default values | Preserved from Swagger `default` field |
| Required vs optional | Required parameters have no default; optional parameters have their default or `None` |
| Multi-line signatures | Long parameter lists are wrapped at 88 characters |
| Docstrings | Google-style with Args and Returns sections |
| TypedDict classes | Generated for all endpoints with parseable `200`/`201` response schemas |
| Auth | Always read from `os.environ` — never hardcoded |
| Identifier safety | `@`, `-`, spaces, and other non-identifier chars replaced with `_` |

---

## Stage 5 — LLM Enhancement (Optional)

When `MCE_LLM_ENHANCE=true`, each generated `functions.py` is sent to an LLM (via LiteLLM) with a prompt that instructs it to:

- Improve docstring descriptions and add usage examples
- **Not** modify any HTTP calls, URLs, function signatures, or return types

The LLM receives only the code skeleton with `os.environ[...]` references — actual credentials are never loaded or sent during compilation.

---

## Stage 6 — Ruff Validation

The generated code is linted with `ruff` before being written to disk. If the generated code has a syntax error (which would indicate a bug in the codegen template), the compile step fails with the specific lint error so it can be investigated and fixed.

---

## Stage 7 — Manifest Writing

After a successful compile, MCE writes three files per server:

```
compiled/weather/
├── functions.py      ← the generated module
├── __init__.py       ← empty, makes compiled/weather a Python package
└── manifest.json     ← metadata used by the runtime registry
```

### `manifest.json` Structure

```json
{
  "name": "weather",
  "spec_hash": "sha256:abc123...",
  "compiled_at": "2026-03-11T14:22:00Z",
  "endpoint_count": 8,
  "endpoints": [
    {
      "operation_id": "get_current_weather",
      "method": "GET",
      "path": "/weather/current",
      "summary": "Get current weather conditions for a city",
      "parameters": [
        { "name": "city", "type": "str", "required": true, "description": "City name" },
        { "name": "units", "type": "str", "required": false, "default": "metric" }
      ],
      "response_fields": [
        { "name": "temperature", "type": "float" },
        { "name": "condition", "type": "str" }
      ],
      "return_type_class": "GetCurrentWeatherResponse"
    }
  ]
}
```

The manifest is what `get_functions` reads — not the `functions.py` source. Function source code is extracted separately via AST parsing when `get_functions` requests `usage_example`.

---

## Running the Compiler

```bash
# Standard compile
mce compile

# Dry run — parse and validate without writing output
mce compile --dry-run

# With LLM docstring enhancement
mce compile --llm-enhance

# Use a specific environment file
mce compile --env-file /path/to/.env.production

# Delete all compiled output and recompile from scratch
mce clean compile
```

---

## Supported Swagger Versions

| Format | Version | Support |
|--------|---------|---------|
| OpenAPI | 3.0.x | Full |
| OpenAPI | 3.1.x | Full |
| Swagger | 2.0 | Full |
| GraphQL | — | Planned (see [Roadmap](Roadmap)) |

Both JSON and YAML spec formats are accepted. Local file paths and remote HTTP(S) URLs are both supported in `swagger_url`.

---

*Next: [SIMD Pattern](SIMD-Pattern) →*
