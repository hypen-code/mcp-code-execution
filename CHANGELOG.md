# Changelog

All notable changes to MCE are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] — 2026-03-14

Initial public release.

### Added
- **4 MCP tools**: `list_servers`, `get_functions`, `execute_code`, `run_cached_code`
- **1 MCP prompt**: `reusable_code_guide` — rules for writing parameterized, cacheable code
- **Compiler pipeline**: Swagger/OpenAPI 3.x and 2.0 → typed Python functions via Jinja2 templates
- **Docker sandbox**: Isolated `python:3.13-slim` execution with memory/CPU limits, non-root user, and 30-second timeout
- **AST security guard**: Static analysis blocks dangerous imports and calls before execution
- **Credential vault**: API keys injected as Docker env vars; never embedded in code or logs
- **Async SQLite cache**: SHA256-keyed code cache with TTL and LRU eviction (aiosqlite)
- **SIMD execution pattern**: `execute_code` returns `cache_id`; `run_cached_code` re-runs with injected parameters
- **Top-level direct tools**: High-priority functions optionally exposed as first-class MCP tools per server
- **Server skills**: Domain-specific usage guides embedded as MCP resources (`skills://<server>`)
- **LLM enhancement** (optional): LiteLLM-powered docstring and example improvement pass during compilation
- **Domain allowlist**: Restrict outbound HTTP calls to configured domains
- **Read-only enforcement**: POST/PUT/PATCH/DELETE endpoints excluded for `is_read_only: true` servers
- **CLI**: `mce compile`, `mce serve`, `mce run`, `mce clean` commands
- **CI/CD**: GitHub Actions pipeline with ruff, mypy --strict, pytest (≥ 90% coverage), and CodeCov
- **Pre-commit hooks**: ruff, mypy, and pytest enforced locally before every commit

### Removed
- `get_cached_code` tool — superseded by `execute_code` returning `cache_id` directly for use with `run_cached_code`
