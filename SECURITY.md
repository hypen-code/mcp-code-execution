# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅ Yes     |

Only the latest release in the `main` branch receives security fixes. Older releases are not backported.

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

To report a security issue, open a [GitHub Security Advisory](https://github.com/hypen-code/mcp-code-execution/security/advisories/new) in this repository. You will receive an acknowledgement within **48 hours** and a status update within **7 days**.

When reporting, please include:
- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept
- The affected version(s)
- Any suggested mitigations (optional)

## Security Model

MCE executes LLM-generated Python code in isolated Docker containers. The security model relies on the following layers:

1. **Code size limit** — code ≥ 64 KB is rejected before analysis.
2. **AST static analysis** — dangerous imports (`os`, `sys`, `subprocess`, `socket`) and calls (`eval`, `exec`, `open`, `__import__`) are blocked before execution.
3. **Docker sandbox** — code runs as a non-root user inside a `python:3.13-slim` container with memory limits (256 MB), CPU limits (50%), a read-only filesystem, and a 30-second execution timeout.
4. **Credential isolation** — API keys are injected exclusively as Docker environment variables and are never embedded in generated code, logs, or tool responses.
5. **Domain allowlist** — outbound HTTP calls can be restricted to a configurable set of allowed domains.

## Scope

The following are considered in-scope security issues:
- Container escape from the Docker sandbox
- Credential leakage through tool responses or logs
- AST guard bypass allowing execution of prohibited operations
- Remote code execution on the host machine

The following are out-of-scope:
- Issues requiring physical access to the host
- Vulnerabilities in third-party dependencies (report to upstream)
- Issues in user-supplied Swagger specs or API servers
