# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- **`clean` CLI command** — removes compiled output directory; supports optional `clean compile` chaining to re-compile immediately after cleaning.
- **MCP client configuration generation** — `compile` now writes a ready-to-use `mcp.json` config snippet alongside compiled servers, following the standard `mcpServers` shape.
- **Operation-level base URL overrides** — individual operations in a Swagger spec can override the server's default base URL.
- **Template hash for change detection** — codegen skips re-writing files whose template output is unchanged, speeding up incremental compiles.
- **URL parameter encoding for list types** — list-typed query/path parameters are now correctly percent-encoded in generated function calls.

### Changed
- camelCase identifiers in generated code are converted to snake_case for PEP 8 compliance.
- Path f-strings are only emitted when path parameters are actually present (avoids redundant f-string prefixes).

### Fixed
- Sanitized identifiers no longer shadow Python keywords (e.g., `import`, `class`, `type`).
