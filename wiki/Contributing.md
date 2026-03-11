# Contributing

Thank you for your interest in contributing to MCE. This page covers everything you need to open a high-quality pull request — from local setup through the definition of done.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Local Setup](#local-setup)
- [Project Structure](#project-structure)
- [Development Workflow](#development-workflow)
- [Branch and Commit Conventions](#branch-and-commit-conventions)
- [Pull Request Process](#pull-request-process)
- [Coding Standards](#coding-standards)
- [Testing Requirements](#testing-requirements)
- [Definition of Done](#definition-of-done)
- [Getting Help](#getting-help)

---

## Prerequisites

| Tool | Minimum version |
|------|----------------|
| Python | **3.13** |
| Docker | **24.0** |
| Git | 2.40 |
| pre-commit | 3.x |

---

## Local Setup

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
# Edit .env — you do not need real API keys for unit tests

# 6. Build the sandbox image (required for executor integration tests)
docker build -t mce-sandbox:latest sandbox/
docker network create mce_network

# 7. Verify everything works
pytest
```

---

## Project Structure

```
src/mce/
├── __init__.py              # Version exports
├── __main__.py              # CLI entry point (compile, clean, serve, run)
├── server.py                # FastMCP tool registration
├── config.py                # Pydantic settings — all MCE_* env vars
├── errors.py                # Exception hierarchy — all custom exceptions
├── models/__init__.py       # ALL Pydantic models — single file, no exceptions
│
├── compiler/                # Code generation pipeline
│   ├── swagger_parser.py    # OpenAPI 3.x / Swagger 2.0 parser
│   ├── codegen.py           # Jinja2 Python code generator
│   ├── orchestrator.py      # Compile pipeline coordinator
│   ├── llm_enhancer.py      # Optional LLM docstring improvement
│   └── templates/
│       └── function.py.j2   # Jinja2 template for generated functions.py
│
├── runtime/                 # Execution pipeline
│   ├── registry.py          # Loads manifests, provides function lookups
│   ├── executor.py          # Docker sandbox code execution
│   └── cache.py             # Async SQLite code cache
│
├── security/                # Defense layers
│   ├── ast_guard.py         # AST static analysis
│   ├── policies.py          # Read-only + domain allowlist enforcement
│   └── vault.py             # Credential → Docker env var injection
│
└── utils/
    ├── logging.py           # Structlog setup
    └── hashing.py           # SHA-256 helpers

tests/
├── conftest.py              # Shared fixtures
├── fixtures/                # YAML swagger test specs
│   ├── weather_api.yaml
│   ├── hotel_api.yaml
│   └── petstore.yaml
├── unit/                    # 13+ fast test modules (no Docker, no network)
└── integration/             # Tests with compiled output
```

### Architecture Rules (Hard Constraints)

These are not style preferences — they are enforced:

| Rule | Rationale |
|------|-----------|
| All Pydantic models in `models/__init__.py` | Single source of truth; prevents model sprawl |
| All exceptions in `errors.py` | Uniform exception hierarchy; easy to audit |
| No new top-level directories | Structure is intentional; discuss with maintainers first |
| Max file length: 400 lines | Keeps modules focused |
| Max function length: 50 lines | Forces decomposition |
| No `print()` — use `structlog` | `print()` breaks stdio MCP transport |
| No `requests` or `urllib` — use `httpx` | Consistent async HTTP client |
| No `os.environ` in business logic — use `MCEConfig` | Centralizes config management |
| `Optional[X]` / `Union[X,Y]` not allowed | Use `X | None` / `X | Y` (Python 3.10+ syntax) |

---

## Development Workflow

```
main           ← protected; release-ready at all times
  └── feat/your-feature
  └── fix/the-bug-description
  └── chore/housekeeping-task
  └── docs/update-readme
```

1. Create a branch from `main`.
2. Make atomic commits — one logical change per commit.
3. Run the full quality suite before pushing:

```bash
ruff check src/ tests/           # lint
ruff format --check src/ tests/  # format
mypy src/                        # type check
pytest                           # tests + coverage ≥ 90%
pre-commit run --all-files       # full hook suite
```

All steps must exit 0.

4. Open a pull request against `main`.

---

## Branch and Commit Conventions

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

**Rules:**
- Summary line ≤ 72 characters, imperative mood, no trailing period
- Reference issues with `Closes #N` or `Refs #N` in the footer
- Never use `git commit --no-verify` for feature work

---

## Pull Request Process

1. **Fill in the PR template** completely. Reviewers will not merge PRs with empty sections.
2. **Keep PRs focused** — one feature or fix per PR. Bundled unrelated changes will be asked to split.
3. **All CI checks must be green** before review begins.
4. **Update `README.md`** in the same PR if the change affects documented behaviour.
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

### Language and Typing

```python
from __future__ import annotations  # required in every file

# Use modern union syntax
def foo(value: str | None) -> list[str] | None: ...

# Use lowercase generics
items: list[str] = []
mapping: dict[str, Any] = {}

# NOT this:
from typing import Optional, Union, List, Dict
def foo(value: Optional[str]) -> Optional[List[str]]: ...
```

### Logging

```python
from mce.utils.logging import get_logger
logger = get_logger(__name__)

# Correct: structured key=value pairs
logger.info("cache_stored", id=entry_id[:12], description=description[:50])

# Wrong: f-strings, print()
logger.info(f"stored cache entry {entry_id}")
print("stored")
```

### Error Handling

```python
# MCP tool functions must never raise — always return an error dict
async def execute_code(code: str, description: str) -> dict[str, Any]:
    try:
        result = await executor.run(code)
        return {"success": True, "data": result.data}
    except SecurityViolationError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.exception("unexpected_error", error=str(e))
        return {"success": False, "error": "Internal error"}

# Never use bare except:
try:
    ...
except:           # wrong
    pass

except Exception: # correct
    ...
```

### Docstrings (Google Style)

```python
def get_current_weather(city: str, units: str = "metric") -> GetCurrentWeatherResponse:
    """Get current weather conditions for a city.

    Args:
        city: City name, e.g. 'London'.
        units: Temperature units: metric | imperial. Defaults to 'metric'.

    Returns:
        GetCurrentWeatherResponse containing temperature, condition, and humidity.

    Raises:
        httpx.HTTPStatusError: If the API returns a non-2xx response.
    """
```

---

## Testing Requirements

- **Coverage gate: ≥ 90%** — enforced by the pre-commit hook. PRs that drop coverage below this threshold will not be merged.
- **Unit tests** (`tests/unit/`) must have **no Docker, no network, no real filesystem** side effects.
- Use `tmp_path` for SQLite or file I/O in tests — never share state across tests.
- Mock `httpx` calls with `respx`; mock Docker with `unittest.mock.MagicMock`.
- **Test naming**: `test_{unit}_{condition}_{expected_outcome}`.
- One assertion concept per test — split long tests into smaller focused ones.

### AST Guard Tests

Any new blocked pattern in `ast_guard.py` **must** include a test:

```python
def test_import_subprocess_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="subprocess"):
        guard.validate("import subprocess")

def test_import_allowed_module_passes(guard: ASTGuard) -> None:
    guard.validate("import json")  # must not raise
```

### Running Tests

```bash
# All tests with coverage
pytest

# Fast unit tests only (no Docker)
pytest tests/unit/ --no-cov -v

# Integration tests
pytest tests/integration/ --no-cov -v

# Specific file
pytest tests/unit/test_ast_guard.py -v

# With coverage report
pytest --cov=mce --cov-report=html
```

---

## Definition of Done

A change is complete when **all** of the following are true:

| Check | Command |
|-------|---------|
| Ruff lint passes | `ruff check src/ tests/` |
| Format passes | `ruff format --check src/ tests/` |
| Type check passes | `mypy src/` |
| Tests pass with ≥ 90% coverage | `pytest --cov-fail-under=90` |
| Pre-commit passes | `pre-commit run --all-files` |
| README updated (if behaviour changed) | Manual check |
| No credentials in code/tests | Manual check |
| No `print()` statements | `grep -r "print(" src/` |
| Dry-run succeeds (if compiler touched) | `mce compile --dry-run` |

---

## Getting Help

| Channel | Use for |
|---------|---------|
| [GitHub Discussions](https://github.com/hypen-code/mcp-code-execution/discussions) | Usage questions, design discussions |
| [GitHub Issues](https://github.com/hypen-code/mcp-code-execution/issues) | Bug reports, feature requests |
| PR comments | Code review, implementation questions |
| [AGENTS.md](../AGENTS.md) | Canonical internal development reference for AI agents and human contributors |

---

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](https://www.contributor-covenant.org/version/2/1/code_of_conduct/). By participating you agree to uphold it. Report unacceptable behaviour to the maintainers via a private GitHub issue.

