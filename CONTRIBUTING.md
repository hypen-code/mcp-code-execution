# Contributing to MCE

Thank you for your interest in contributing to **MCE — MCP Code Execution**!
This document covers everything you need to open a high-quality pull request.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Workflow](#development-workflow)
- [Branch & Commit Conventions](#branch--commit-conventions)
- [Pull Request Process](#pull-request-process)
- [Coding Standards](#coding-standards)
- [Testing Requirements](#testing-requirements)
- [Security Policy](#security-policy)
- [Reporting Bugs](#reporting-bugs)
- [Requesting Features](#requesting-features)

---

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).
By participating you agree to uphold it. Please report unacceptable behaviour to the maintainers via a private GitHub issue.

---

## Getting Started

### Prerequisites

| Tool | Minimum version |
|------|----------------|
| Python | 3.13 |
| Docker | 24.0 |
| Git | 2.40 |
| pre-commit | 3.x |

### Local Setup

```bash
# 1. Fork the repo on GitHub, then clone your fork
git clone https://github.com/<your-username>/mcp-code-execution.git
cd mcp-code-execution

# 2. Install in editable mode with all dev dependencies
pip install -e ".[dev]"

# 3. (Optional) Install LLM enhancement support
pip install -e ".[llm]"

# 4. Install pre-commit hooks
pre-commit install

# 5. Copy and configure environment
cp .env.example .env
# Edit .env — you don't need real API keys for unit tests

# 6. Build the sandbox image (required for executor integration tests)
docker build -t mce-sandbox:latest sandbox/
docker network create mce_network

# 7. Verify everything works
pytest
```

---

## Development Workflow

```
main           ← protected; release-ready at all times
  └── feat/your-feature
  └── fix/the-bug-description
  └── chore/housekeeping-task
  └── docs/update-readme
```

1. **Create a branch** from `main` (see naming below).
2. **Make atomic commits** — one logical change per commit.
3. **Run the full quality suite** before pushing (all steps below must exit 0):

   ```bash
   ruff check src/ tests/          # lint
   ruff format --check src/ tests/ # format
   mypy src/                       # type check
   pytest                          # tests + coverage ≥ 90%
   pre-commit run --all-files      # full hook suite
   ```

4. **Open a pull request** against `main`.

---

## Branch & Commit Conventions

### Branch Names

```
feat/<short-description>      # new feature
fix/<short-description>       # bug fix
chore/<short-description>     # tooling, deps, CI
docs/<short-description>      # documentation only
refactor/<short-description>  # no behaviour change
test/<short-description>      # adding or fixing tests
```

### Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <imperative summary>

[optional body — explain the *why*, not the *what*]

[optional footer — e.g. Closes #123]
```

**Types:** `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `perf`, `ci`

**Examples:**

```
feat(server): add run_cached_code tool with parameter injection
fix(executor): handle container timeout when Docker daemon is slow
docs(readme): update env var table to include MCE_DOCKER_HOST
test(ast_guard): add tests for nonlocal statement blocking
chore(deps): bump fastmcp to 2.1.0
```

Rules:
- Summary line ≤ 72 characters, imperative mood, no trailing period.
- Reference issues with `Closes #N` or `Refs #N` in the footer.
- **Never use `git commit --no-verify`** for feature work — only emergency hotfixes.

---

## Pull Request Process

1. **Fill in the PR template** completely. Reviewers will not merge PRs with empty sections.
2. **Keep PRs focused** — one feature or fix per PR. Bundled unrelated changes will be asked to split.
3. **All CI checks must be green** before review begins.
4. **Update `README.md`** in the same PR if the change affects any documented behaviour (see the table in [AGENTS.md § 8.7](AGENTS.md)).
5. **One approving review** from a maintainer is required to merge.
6. PRs are merged with **squash merge** to keep `main` history linear.

### PR Checklist

Before marking your PR ready for review, confirm every item:

- [ ] New/modified functions have full type annotations (`from __future__ import annotations` at top)
- [ ] All public functions have Google-style docstrings
- [ ] `ruff check src/ tests/` exits 0
- [ ] `ruff format --check src/ tests/` exits 0
- [ ] `mypy src/` exits 0
- [ ] `pytest --cov=mce --cov-fail-under=90` exits 0
- [ ] `pre-commit run --all-files` exits 0
- [ ] `README.md` updated if documented behaviour changed
- [ ] No credentials, tokens, or secrets in code, tests, or assertions
- [ ] No `print()` statements (use `structlog` via `get_logger(__name__)`)
- [ ] No new files created outside the defined project structure
- [ ] `mce compile --dry-run` succeeds if compiler was touched
- [ ] Security guard tests updated if `ast_guard.py` was touched

---

## Coding Standards

MCE enforces strict coding standards. The full specification lives in [AGENTS.md](AGENTS.md). Key rules:

### Language & Typing

- **Python 3.13+** only. Use `from __future__ import annotations` in every file.
- Use `X | Y` union syntax — not `Optional[X]` or `Union[X, Y]`.
- Use lowercase generics: `list[str]`, `dict[str, Any]` — not `List`, `Dict`.
- All public signatures must be fully typed; `mypy --strict` must pass.

### Logging

```python
from mce.utils.logging import get_logger
logger = get_logger(__name__)

# ✅ Structured key=value pairs
logger.info("cache_stored", id=entry_id[:12], description=description[:50])

# ❌ No f-strings in log messages, no print()
logger.info(f"stored cache entry {entry_id}")
```

### Error Handling

- No bare `except:` — always name the exception type.
- MCP tool functions must **never raise** — always return a `dict` with an `"error"` key.
- Always `logger.exception(...)` before swallowing unexpected errors.

### Architecture Rules

- **One model file**: all Pydantic models in `src/mce/models/__init__.py`.
- **One error file**: all exceptions in `src/mce/errors.py`.
- **No new top-level directories** without explicit maintainer approval.
- Max file length: **400 lines**. Max function length: **50 lines**.
- Never call `os.environ` directly in business logic — use `MCEConfig`.
- Never use `requests` or `urllib` — use `httpx`.

---

## Testing Requirements

- Coverage gate: **≥ 90%** (hard floor enforced by pre-commit hook).
- Unit tests (`tests/unit/`) must have **no Docker, no network, no real filesystem** side effects.
- Use `tmp_path` for any SQLite or file I/O in tests — never share state across tests.
- Mock `httpx` calls with `respx`; mock Docker with `unittest.mock.MagicMock`.
- One assertion concept per test — split long tests.
- Test naming: `test_{unit}_{condition}_{expected_outcome}`.

New blocked AST patterns **must** have a test that confirms `SecurityViolationError` is raised.
New allowed patterns **must** have a test confirming no exception is raised.

---

## Security Policy

**Do not open public GitHub issues for security vulnerabilities.**

If you discover a security issue (e.g. AST guard bypass, credential leak, sandbox escape):

1. Email the maintainers privately or use [GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability).
2. Include a minimal reproducible example and the impact assessment.
3. Allow up to **72 hours** for an initial response before any public disclosure.

We follow responsible disclosure and will credit reporters in the changelog.

---

## Reporting Bugs

Open a [GitHub Issue](https://github.com/hypen-code/mcp-code-execution/issues/new?template=bug_report.md) with:

- **MCE version** (`pip show mce`)
- **Python version** and OS
- **Docker version** (`docker --version`)
- **Minimal reproducible example** — smallest code/config that triggers the bug
- **Expected vs actual behaviour**
- **Relevant log output** (sanitize credentials before pasting)

---

## Requesting Features

Open a [GitHub Issue](https://github.com/hypen-code/mcp-code-execution/issues/new?template=feature_request.md) with:

- **Problem statement** — what limitation or friction inspired this?
- **Proposed solution** — how should MCE behave differently?
- **Alternatives considered** — what else did you evaluate?
- **Scope estimate** — is this a small addition or a new subsystem?

Check the [ROADMAP.md](ROADMAP.md) first — your idea may already be planned.

---

## Getting Help

- **Questions about usage**: open a [GitHub Discussion](https://github.com/hypen-code/mcp-code-execution/discussions).
- **Questions about the codebase**: read [AGENTS.md](AGENTS.md) — it is the canonical development reference.
- **Stuck on a PR?** Leave a comment and tag a maintainer.
