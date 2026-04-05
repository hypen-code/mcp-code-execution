"""MCE FastMCP server — registers the 4 MCP tools and 1 prompt exposed to LLMs."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP
from toon_format import encode as _toon_encode

from mce.errors import (
    CacheError,
    ExecutionError,
    ExecutionTimeoutError,
    FunctionNotFoundError,
    LintError,
    SecurityViolationError,
    ServerNotFoundError,
)
from mce.runtime.cache import CacheStore
from mce.runtime.executor import CodeExecutor  # noqa: TC001  — used in function signature
from mce.runtime.registry import Registry
from mce.utils.logging import get_logger

if TYPE_CHECKING:
    from mce.config import MCEConfig

logger = get_logger(__name__)

_SANDBOX_CAPABILITIES = """\
## Sandbox Python Libraries

### Data Processing
**pandas** / **numpy**
Use: tabular data manipulation, array math, time-series indexing
Example:
  import pandas as pd, numpy as np
  df = pd.DataFrame({"price": np.random.randn(100).cumsum() + 100})
  result = df["price"].describe().to_dict()

### Technical Analysis
**pandas-ta**
Use: 130+ technical indicators (RSI, MACD, Bollinger Bands) computed directly on a DataFrame
Example:
  import pandas as pd, pandas_ta as ta
  df = pd.DataFrame({"close": [100, 101, 99, 103, 102, 105, 104, 107, 106, 108,
                                110, 109, 111, 113, 112, 115, 114, 116, 118, 117]})
  df.ta.rsi(length=14, append=True)
  result = df.tail(5).to_dict()

**TA-Lib** (talib)
Use: battle-tested C-backed TA indicators, candlestick pattern recognition
Example:
  import talib, numpy as np
  close = np.array([100.0, 101.5, 99.8, 102.3, 103.1, 104.0, 103.5, 105.2])
  result = {"sma": talib.SMA(close, timeperiod=5).tolist(),
            "rsi": talib.RSI(close, timeperiod=5).tolist()}

### Backtesting & Strategy
**backtrader**
Use: event-driven strategy backtesting with built-in broker, commission, and sizer
Example:
  import backtrader as bt
  class SmaCross(bt.Strategy):
      def __init__(self):
          sma = bt.ind.SMA(period=5)
          self.signal = bt.ind.CrossOver(self.data.close, sma)
      def next(self):
          if self.signal > 0:
              self.buy()
          elif self.signal < 0:
              self.sell()
  cerebro = bt.Cerebro()
  cerebro.addstrategy(SmaCross)
  result = "backtrader strategy registered"

### Machine Learning & Forecasting
**scikit-learn** (sklearn)
Use: classification, regression, clustering, dimensionality reduction, preprocessing pipelines
Example:
  from sklearn.linear_model import LinearRegression
  import numpy as np
  X = np.array([[1],[2],[3],[4],[5],[6],[7],[8],[9],[10]])
  y = np.array([2.1, 3.9, 6.2, 7.8, 10.1, 12.0, 14.2, 15.9, 18.1, 20.0])
  model = LinearRegression().fit(X, y)
  result = {"coef": round(model.coef_[0], 3), "intercept": round(model.intercept_, 3),
            "r2": round(model.score(X, y), 4)}

**prophet**
Use: additive time-series forecasting with automatic trend, seasonality, and holiday effects
Example:
  from prophet import Prophet
  import pandas as pd
  df = pd.DataFrame({"ds": pd.date_range("2023-01-01", periods=365, freq="D"),
                     "y": range(365)})
  m = Prophet(yearly_seasonality=True)
  m.fit(df)
  future = m.make_future_dataframe(periods=30)
  result = m.predict(future)[["ds", "yhat", "yhat_lower", "yhat_upper"]].tail(5).to_dict("records")

**statsmodels**
Use: OLS/GLS regression, ARIMA/GARCH time-series, hypothesis testing, statistical diagnostics
Example:
  import statsmodels.api as sm
  import numpy as np
  np.random.seed(42)
  X = sm.add_constant(np.arange(50))
  y = X[:, 1] * 2.5 + 10 + np.random.randn(50)
  model = sm.OLS(y, X).fit()
  result = {"r2": round(model.rsquared, 4), "coef": [round(c, 3) for c in model.params]}

### Portfolio & Risk Analysis
**PyPortfolioOpt** (pypfopt)
Use: mean-variance optimisation, Black-Litterman, risk parity, efficient frontier weights
Example:
  from pypfopt import EfficientFrontier, expected_returns, risk_models
  import pandas as pd, numpy as np
  np.random.seed(0)
  prices = pd.DataFrame(
      np.random.randn(252, 3).cumsum(0) + 100, columns=["AAPL", "MSFT", "GOOG"])
  mu = expected_returns.mean_historical_return(prices)
  S = risk_models.sample_cov(prices)
  ef = EfficientFrontier(mu, S)
  result = dict(ef.max_sharpe())

**cvxpy**
Use: convex optimisation — portfolio weight constraints, risk minimisation, factor models
Example:
  import cvxpy as cp, numpy as np
  w = cp.Variable(3)
  expected_ret = np.array([0.10, 0.15, 0.08])
  prob = cp.Problem(cp.Maximize(expected_ret @ w), [cp.sum(w) == 1, w >= 0])
  prob.solve()
  result = {"weights": [round(v, 4) for v in w.value]}

### Sentiment Analysis
**vaderSentiment**
Use: rule-based sentiment scoring tuned for social media and financial news text
Example:
  from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
  sia = SentimentIntensityAnalyzer()
  texts = ["The stock surged 20% on strong earnings beat!",
           "Missed revenue targets for the third consecutive quarter.",
           "Market closed flat amid mixed economic signals."]
  result = [{"text": t, "scores": sia.polarity_scores(t)} for t in texts]
"""


def _load_top_level_tools(compiled_dir: str | Path) -> list[dict[str, Any]]:
    """Scan the compiled directory for ``top_level_functions.py`` files and load them.

    Each file exposes a ``_TOP_LEVEL_TOOLS`` list of ``{"name", "fn", "server"}``
    dicts that ``create_server`` uses to register direct FastMCP tools.

    The compiled directory is added to ``sys.path`` (once) so the generated
    files can import their sibling ``functions.py`` modules.

    Args:
        compiled_dir: Path to the compiled output directory.

    Returns:
        List of tool descriptor dicts ready to register with FastMCP.
    """
    compiled_path = Path(compiled_dir)
    tools: list[dict[str, Any]] = []

    tlf_paths = sorted(compiled_path.glob("*/top_level_functions.py"))
    if not tlf_paths:
        return tools

    # Make compiled dir importable so `from <server>.functions import …` works
    compiled_str = str(compiled_path.resolve())
    if compiled_str not in sys.path:
        sys.path.insert(0, compiled_str)

    for tlf_path in tlf_paths:
        server_name = tlf_path.parent.name
        module_key = f"_mce_tlf_{server_name}"
        try:
            spec = importlib.util.spec_from_file_location(module_key, tlf_path)
            if spec is None or spec.loader is None:
                logger.warning("top_level_functions_spec_invalid", path=str(tlf_path))
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            tool_list: list[dict[str, Any]] = getattr(module, "_TOP_LEVEL_TOOLS", [])
            tools.extend(tool_list)
            logger.info("top_level_tools_loaded", server=server_name, count=len(tool_list))
        except Exception as exc:  # noqa: BLE001
            logger.warning("top_level_tools_load_failed", server=server_name, error=str(exc))

    return tools


_BASE_INSTRUCTIONS = """\
# MCE — MCP Code Execution: Usage Guide

## TWO MANDATORY RULES — read before every tool call

**Rule 1 — `get_functions` first**: Call `get_functions` before writing any code.
Never use `from <server>.functions import <fn>` without calling `get_functions`
first in the same session.

**Rule 2 — `run_cached_code` for repeat operations**: When the `execute_code`
response contains a `cache_id`, that ID is your key for re-running the same logic
with new values. The `_next` field in every successful response shows the exact
call to make. You MUST use `run_cached_code` — do NOT call `execute_code` again
for the same type of operation.

## Workflow

1. **`list_servers`** — Discover available API servers.

2. **`get_functions`** — Fetch signatures for 1–5 functions. Use the
   `import_statement` from the response verbatim.

3. **`execute_code`** — Run sandboxed Python code.
   - Put every dynamic value (city, ID, date…) in a **top-level variable**.
   - `main()` reads those variables as globals — it takes NO arguments.
   - On success the response includes `cache_id` AND a `_next` field that shows
     the exact `run_cached_code(...)` call to use for the next similar request.

4. **`run_cached_code`** — Re-run cached code with new parameter values.
   - Use the `cache_id` from step 3.
   - Pass only the changed variable(s) as `params`.
   - The `_next` field in the `execute_code` response shows the exact syntax.

## Additional rules

- Keep `execute_code` payloads minimal — extract only the fields you need.
- description must be generic — "action + entity type", NO specific values or dates.
- If execution fails, re-read `get_functions` output before retrying.
"""


def _build_instructions(
    registry: Registry,
    servers_with_skills: list[str],
    top_level_tools: list[dict[str, Any]] | None = None,
    additional_tools_enabled: bool = False,
) -> str:
    """Build the FastMCP instructions string.

    Skills content for each entry in ``servers_with_skills`` is embedded inline
    so the LLM receives it automatically via the MCP ``initialize`` response —
    no explicit resource fetch required.  When the list is empty the base
    instructions are returned as-is, spending zero extra tokens.

    Args:
        registry: Registry used to resolve each server's skills file path.
        servers_with_skills: Pre-computed list of server module names that have
            a ``skills.md`` on disk.  Computed once in ``create_server`` so this
            function never calls ``registry.list_servers()`` itself.
    """
    # Inject skills-tool steps into the workflow when additional tools are enabled.
    base = _BASE_INSTRUCTIONS
    if additional_tools_enabled:
        base = base.replace(
            "1. **`list_servers`** — Discover available API servers.",
            "1. **`list_servers`** — Discover available API servers.\n\n"
            "1a. **`list_skills`** — See which servers have a skills guide.\n"
            "1b. **`get_server_skills`** — Read a server's skills guide before using it.",
        )

    # Prepend a direct-tools section when top-level tools are registered so the
    # LLM knows it can call them immediately — no workflow required.
    direct_section = ""
    if top_level_tools:
        lines = [
            "\n\n## Direct API Tools\n\n"
            "The following API functions are registered as **direct MCP tools** "
            "and can be called immediately — no `list_servers` → `get_functions` "
            "→ `execute_code` workflow is needed.\n"
        ]
        # Group by server for readability
        by_server: dict[str, list[str]] = {}
        for entry in top_level_tools:
            srv = entry.get("server", "unknown")
            by_server.setdefault(srv, []).append(entry["name"])
        for srv, names in by_server.items():
            lines.append(f"\n**`{srv}`**: " + ", ".join(f"`{n}`" for n in names))
        direct_section = "".join(lines) + "\n"

    if not servers_with_skills:
        return base + direct_section

    skills_blocks: list[str] = []
    for sn in servers_with_skills:
        path = registry.skills_path(sn)
        if path is not None:
            skills_blocks.append(f"### `{sn}`\n\n{path.read_text(encoding='utf-8')}")

    if not skills_blocks:
        return base + direct_section

    divider = "\n\n---\n\n"
    skills_section = (
        "\n\n## Server Skills\n\n"
        "The following server-specific guides are pre-loaded. "
        "Apply their guidance whenever you use that server's tools.\n\n" + divider.join(skills_blocks) + "\n"
    )
    return base + direct_section + skills_section


def _apply_params_to_code(code: str, params: dict[str, Any]) -> str:
    """Inject *params* into cached code by replacing top-level variable assignments.

    For each ``key`` in *params*:

    * If ``key = <anything>`` appears as a **non-indented** assignment anywhere in
      the code, every such occurrence is replaced with ``key = <new_value>``.
    * If no such assignment exists, ``key = <new_value>`` is **prepended** so the
      variable is available when the code references it.

    A ``_params`` dict is also prepended so code can optionally read from it:
        location = _params.get("location_name", "Colombo")

    This approach works for both code patterns:
    * ``result``-only code (module-level statements, no ``main()``): the replaced
      assignment runs with the new value, producing the correct ``result``.
    * ``main()``-based code with module-level side effects (e.g. geocoding):
      replacing the variable before the code runs means geocoding uses the new
      value from the first line, not from the cached original.

    Args:
        code: Original cached Python source.
        params: Parameter overrides to inject.

    Returns:
        Modified Python source ready for re-execution.
    """
    for key, value in params.items():
        # Match non-indented assignment: `key = <anything to EOL>`
        pattern = rf"^{re.escape(key)}\s*=.*$"
        replacement = f"{key} = {value!r}"
        new_code, count = re.subn(pattern, replacement, code, flags=re.MULTILINE)
        code = new_code if count else f"{key} = {value!r}\n{code}"

    # Prepend full params dict so code can also read from _params directly
    return f"_params = {params!r}\n{code}"


def create_server(
    config: MCEConfig,
    registry: Registry | None = None,
    cache: CacheStore | None = None,
    executor: CodeExecutor | None = None,
) -> FastMCP:
    """Create and configure the MCE FastMCP server with all 6 tools and 1 prompt.

    Args:
        config: MCE configuration instance.
        registry: Pre-loaded Registry. If None, a new one is created from config.
        cache: Pre-initialized CacheStore. If None, a new one is created from config.
        executor: Pre-started CodeExecutor. If None, a new one is created — caller is
            responsible for calling ``executor.startup()`` before requests arrive.

    Returns:
        Configured FastMCP server ready to run.
    """
    # Initialise registry before FastMCP so we can inspect skills availability
    # and tailor the server instructions accordingly.
    if registry is None:
        registry = Registry(config.compiled_output_dir)
        registry.load()
    if cache is None:
        cache = CacheStore(config.cache_db_path, config.cache_ttl_seconds, config.cache_max_entries)

    # Compute once; guard so a broken registry at startup doesn't crash the server.
    try:
        servers_with_skills = [s.name for s in registry.list_servers() if registry.has_skills(s.name)]
    except Exception:  # noqa: BLE001
        logger.warning("skills_discovery_failed")
        servers_with_skills = []

    # Load top-level tool definitions from compiled directories (if any).
    # Done before FastMCP construction so their names appear in the instructions.
    top_level_tools = _load_top_level_tools(config.compiled_output_dir)

    mcp: FastMCP = FastMCP(
        name="MCE — MCP Code Execution",
        instructions=_build_instructions(
            registry, servers_with_skills, top_level_tools, config.enable_additional_tools
        ),
    )

    if executor is None:
        executor = CodeExecutor(config, cache)

    try:
        _sandbox_libraries = [
            line.strip() for line in Path(config.sandbox_requirements_path).read_text().splitlines() if line.strip()
        ]
    except OSError:
        _sandbox_libraries = []

    @mcp.tool()
    async def list_servers() -> str:
        """List all available API servers and their functions.

        Returns a compact overview of each server with:
        - Server name and description
        - List of available functions with one-line summaries

        Use this to discover what APIs are available before getting function details.
        """
        try:
            servers = registry.list_servers()
            logger.info("tool_list_servers_called", server_count=len(servers))
            return str(
                _toon_encode(
                    {
                        "sandbox_libraries": _sandbox_libraries,
                        "servers": [
                            {
                                "name": s.name,
                                "description": s.description,
                                "functions": [
                                    {"name": fn, "summary": s.function_summaries.get(fn, "")} for fn in s.functions
                                ],
                            }
                            for s in servers
                        ],
                    }
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("list_servers_unexpected_error")
            return str(_toon_encode({"error": "Internal error loading servers", "detail": str(exc)}))

    if config.enable_additional_tools:

        @mcp.tool()
        async def list_skills() -> str:
            """List all servers that have a skills guide available.

            Returns each server name and whether it has a skills guide configured
            via skills_url in swaggers.yaml. Call get_server_skills to read the
            full guide before using a particular server's functions.
            """
            try:
                servers = registry.list_servers()
                result = [{"server": s.name, "has_skills": registry.has_skills(s.name)} for s in servers]
                logger.info("tool_list_skills_called", count=len(result))
                return str(_toon_encode({"skills": result}))
            except Exception as exc:  # noqa: BLE001
                logger.exception("list_skills_unexpected_error")
                return str(_toon_encode({"error": "Internal error", "detail": str(exc)}))

        @mcp.tool()
        async def get_server_skills(server_name: str) -> str:
            """Fetch the skills guide for a server before using it.

            Skills guides contain usage patterns, best practices, and worked
            examples specific to that server's API. Call this before calling
            get_functions or execute_code for a server you haven't used before.

            Args:
                server_name: Name of the server (from list_servers or list_skills).
            """
            if not registry.has_skills(server_name):
                return str(
                    _toon_encode({"error": f"No skills guide for server '{server_name}'", "error_type": "not_found"})
                )
            path = registry.skills_path(server_name)
            if path is None:
                return str(
                    _toon_encode({"error": f"Skills file missing for '{server_name}'", "error_type": "not_found"})
                )
            logger.info("tool_get_server_skills_called", server=server_name)
            return str(_toon_encode({"server": server_name, "skills": path.read_text(encoding="utf-8")}))

        @mcp.tool()
        async def get_sandbox_capabilities() -> str:
            """List Python libraries available in the sandbox with use cases and code examples.

            Returns a catalogue of every pre-installed library — technical analysis, backtesting,
            machine learning, portfolio optimisation, and sentiment analysis — with one runnable
            code example per library so you can write correct execute_code calls immediately.
            """
            return _SANDBOX_CAPABILITIES

    @mcp.tool()
    async def get_functions(functions: list[dict[str, str]]) -> str:
        """Get detailed function signatures and return schemas for 1–5 functions at once.

        Args:
            functions: List of 1–5 items, each with:
                - server_name: Name of the server (from list_servers).
                - function_name: Name of the function to inspect.

        Returns each function's parameters, types, and response data structure
        so you can write Python code that calls them correctly.
        Requesting more than 5 functions at once returns a validation error.
        """
        if not functions:
            return str(_toon_encode({"error": "Provide at least 1 function.", "error_type": "validation"}))
        if len(functions) > 5:
            return str(
                _toon_encode({"error": "At most 5 functions can be requested at once.", "error_type": "validation"})
            )

        results = []
        for item in functions:
            server_name = item.get("server_name", "")
            function_name = item.get("function_name", "")
            try:
                fn = registry.get_function(server_name, function_name)
                logger.info("tool_get_function_called", server=server_name, function=function_name)
                results.append(
                    {
                        "server": server_name,
                        "function": fn.function_name,
                        "summary": fn.summary,
                        "method": fn.method,
                        "path": fn.path,
                        "parameters": [p.model_dump() for p in fn.parameters],
                        "return_type": fn.return_type,
                        "response_fields": [r.model_dump() for r in fn.response_fields],
                        "usage_example": fn.source_code,
                        "import_statement": f"from {server_name}.functions import {function_name}",
                    }
                )
            except ServerNotFoundError as exc:
                results.append(
                    {
                        "server": server_name,
                        "function": function_name,
                        "error": str(exc),
                        "error_type": "server_not_found",
                    }
                )
            except FunctionNotFoundError as exc:
                results.append(
                    {
                        "server": server_name,
                        "function": function_name,
                        "error": str(exc),
                        "error_type": "function_not_found",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("get_function_unexpected_error")
                results.append(
                    {
                        "server": server_name,
                        "function": function_name,
                        "error": "Internal error",
                        "error_type": "internal",
                        "detail": str(exc),
                    }
                )
        return str(_toon_encode({"functions": results}))

    @mcp.tool()
    async def execute_code(code: str, description: str) -> dict[str, Any]:
        """Execute Python code in a sandboxed Docker container.

        *** AFTER THIS CALL SUCCEEDS: read the `_next` field in the response. ***
        It contains the exact `run_cached_code(...)` call to use next time the
        user asks for the same type of operation with a different value.
        You MUST use `run_cached_code` — do NOT call `execute_code` again.

        Code MUST define either a `main()` function that returns a result,
        or a `result` variable containing the output.

        Available imports in sandbox:
        - Server functions: `from {server_name}.functions import {function_name}`
        - Standard: httpx, json, datetime, re, math, dataclasses, typing, collections

        ## Code pattern — top-level variables are REQUIRED for reuse

        WRONG — hardcoded inside main(), cannot be reused:
            def main():
                return geocoding_search(name="Colombo, Sri Lanka")  # BAD

        CORRECT — top-level variable, reusable via run_cached_code:
            location_name = "Colombo, Sri Lanka"   # ← top-level param

            def main():
                return geocoding_search(name=location_name)  # reads global

        Args:
            code: Valid Python code. ALL dynamic values MUST be top-level variables.
            description: Generic description — "action + entity type", NO specific
                values, cities, or dates (e.g. "get hourly weather forecast for city").
        """
        try:
            result = await executor.execute(code, description)
            logger.info("tool_execute_code_called", success=result.success, description=description[:60])
            dump = result.model_dump()
            if result.success and result.cache_id:
                # Inject an explicit next-step instruction so the LLM uses
                # run_cached_code for the next similar request instead of
                # calling execute_code again.
                dump["_next"] = (
                    f"IMPORTANT — for the same operation with different values call: "
                    f"run_cached_code(cache_id='{result.cache_id}', "
                    f'params={{"<top_level_variable_name>": "<new_value>"}}) '
                    f"Do NOT call execute_code again for this type of query."
                )
            return dump
        except SecurityViolationError as exc:
            return {"success": False, "error": f"Security violation: {exc}", "error_type": "security"}
        except LintError as exc:
            return {
                "success": False,
                "error": f"Code has issues: {exc}",
                "lint_output": exc.lint_output,
                "error_type": "lint",
            }
        except ExecutionTimeoutError:
            return {
                "success": False,
                "error": f"Execution timed out after {config.execution_timeout_seconds}s",
                "error_type": "timeout",
            }
        except ExecutionError as exc:
            return {"success": False, "error": str(exc), "stderr": exc.stderr, "error_type": "execution"}
        except Exception:  # noqa: BLE001
            logger.exception("execute_code_unexpected_error")
            return {"success": False, "error": "Internal error occurred", "error_type": "internal"}

    @mcp.tool()
    async def run_cached_code(cache_id: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Re-run a cached code snippet with new parameter values — use this INSTEAD of execute_code.

        Whenever the user asks for the same type of operation with a different value
        (different city, date, ID, name…), call this tool with the `cache_id` from
        the previous `execute_code` response.  Do NOT call `execute_code` again.

        Each entry in `params` replaces the matching top-level variable in the
        cached code before execution, so the logic runs fresh with the new values:

            # Cached code has: location_name = "Colombo"
            run_cached_code(cache_id="...", params={"location_name": "Kandy"})
            # → replaces location_name = "Colombo" with location_name = "Kandy"
            # → re-runs geocoding + weather fetch for Kandy

        Args:
            cache_id: ID from a previous execute_code or run_cached_code response.
            params: Top-level variable overrides — {variable_name: new_value}.

        Returns:
            Same structure as execute_code: success, data, error, execution_time_ms, cache_id.
        """
        try:
            entry = await cache.get(cache_id)
        except CacheError as exc:
            return {"success": False, "error": f"Cache unavailable: {exc}", "error_type": "cache"}

        if entry is None:
            return {
                "success": False,
                "error": f"Cache entry '{cache_id[:16]}…' not found or expired",
                "error_type": "cache_miss",
            }

        code = entry.code
        if params:
            # Replace top-level variable assignments for each param key so the
            # code runs fresh with the new values.  Works for both result-only
            # code (no main()) and main()-based code with module-level side
            # effects (e.g. geocoding computed from a location variable).
            code = _apply_params_to_code(code, params)

        logger.info("tool_run_cached_code_called", cache_id=cache_id[:16], has_params=bool(params))

        try:
            result = await executor.execute(code, entry.description)
            return result.model_dump()
        except SecurityViolationError as exc:
            return {"success": False, "error": f"Security violation: {exc}", "error_type": "security"}
        except LintError as exc:
            return {
                "success": False,
                "error": f"Code has issues: {exc}",
                "lint_output": exc.lint_output,
                "error_type": "lint",
            }
        except ExecutionTimeoutError:
            return {
                "success": False,
                "error": f"Execution timed out after {config.execution_timeout_seconds}s",
                "error_type": "timeout",
            }
        except ExecutionError as exc:
            return {"success": False, "error": str(exc), "stderr": exc.stderr, "error_type": "execution"}
        except Exception:  # noqa: BLE001
            logger.exception("run_cached_code_unexpected_error")
            return {"success": False, "error": "Internal error occurred", "error_type": "internal"}

    @mcp.prompt()
    def reusable_code_guide() -> str:
        """Guide for writing reusable, cacheable execute_code payloads."""
        return (
            "WRONG — hardcoded inside main():\n"
            "    def main(): return fn(name='Colombo')  # BAD\n\n"
            "CORRECT — top-level variable:\n"
            "    location_name = 'Colombo'\n"
            "    def main(): return fn(name=location_name)  # reads global\n"
            "    result = main()\n\n"
            "After execute_code, remember the cache_id. Next request of the same type:\n"
            "    run_cached_code(cache_id, params={'location_name': 'Galle'})\n\n"
            "Rules:\n"
            "- ALL dynamic values → top-level variables\n"
            "- main() NEVER takes arguments\n"
            "- description: 'action + entity + key param', no specific values"
        )

    # Register one concrete static resource per server that has a skills document.
    # Static resources (no URI-template params) appear in resources/list, making them
    # immediately discoverable.  Keeping registration conditional avoids surfacing
    # empty resources when no skills are configured.
    def _make_skills_resource(sn: str) -> None:
        @mcp.resource(
            f"skills://{sn}",
            name=f"{sn}_skills",
            description=f"Skills guide for {sn}: usage patterns, best practices, and worked examples.",
            mime_type="text/markdown",
        )
        def _get_skills() -> str:
            """Return the skills guide for this API server."""
            skills_file = registry.skills_path(sn)
            if skills_file is None:
                logger.debug("skills_resource_miss", server=sn)
                return f"No skills documentation is available for server '{sn}'."
            logger.debug("skills_resource_served", server=sn)
            return skills_file.read_text(encoding="utf-8")

    for _sn in servers_with_skills:
        _make_skills_resource(_sn)

    # Register top-level tools as first-class FastMCP tools.
    # Each tool is an async function defined in compiled/<server>/top_level_functions.py.
    # The function's __name__ becomes the MCP tool name; its docstring the description.
    _registered_tool_names: set[str] = set()
    for _entry in top_level_tools:
        _tool_fn = _entry["fn"]
        _tool_name: str = _entry.get("name", _tool_fn.__name__)
        _tool_server: str = _entry.get("server", "?")
        if _tool_name in _registered_tool_names:
            logger.warning(
                "top_level_tool_name_conflict",
                name=_tool_name,
                server=_tool_server,
                detail="Skipping duplicate tool name — rename the function in swaggers.yaml",
            )
            continue
        try:
            mcp.tool()(_tool_fn)
            _registered_tool_names.add(_tool_name)
            logger.info("top_level_tool_registered", name=_tool_name, server=_tool_server)
        except Exception as _exc:  # noqa: BLE001
            logger.warning(
                "top_level_tool_registration_failed",
                name=_tool_name,
                server=_tool_server,
                error=str(_exc),
            )

    return mcp


def create_degraded_server(error_message: str) -> FastMCP:
    """Create a minimal MCP server for when Docker is unavailable.

    Exposes only the ``get_server_state`` tool, which explains the issue
    and guides the user to install or start Docker.  All other tools are
    omitted so the LLM cannot attempt code execution in this state.

    Args:
        error_message: The error string from the failed Docker startup.

    Returns:
        Configured FastMCP server in degraded mode.
    """
    mcp: FastMCP = FastMCP(
        name="MCE — MCP Code Execution (Degraded)",
        instructions=(
            "MCE is running in degraded mode — Docker is unavailable.\n"
            "Use the `get_server_state` tool to see what is wrong and how to fix it.\n"
            "Once Docker is running, restart the MCE server to restore full functionality."
        ),
    )

    @mcp.tool()
    async def get_server_state() -> dict[str, Any]:
        """Get the current server health state and Docker setup instructions.

        MCE is currently running in degraded mode because Docker is not available.
        This tool explains the issue and provides steps to resolve it.

        After fixing Docker, restart the MCE MCP server — all tools will be
        available again and this tool will no longer appear.
        """
        return {
            "status": "degraded",
            "issue": "docker_not_found",
            "message": error_message,
            "description": (
                "MCE requires Docker to run sandboxed code. "
                "The Docker daemon is not running or Docker is not installed on this machine."
            ),
            "resolution": {
                "check_installed": "docker --version",
                "start_linux": "sudo systemctl start docker",
                "start_macos": "open -a Docker  # or launch Docker Desktop from Applications",
                "start_windows": "Launch Docker Desktop from the Start Menu",
                "enable_on_boot_linux": "sudo systemctl enable docker",
                "install_guide": "https://docs.docker.com/engine/install/",
            },
            "next_steps": [
                "1. Verify Docker is installed: run `docker --version` in a terminal.",
                "2. If not installed, follow: https://docs.docker.com/engine/install/",
                "3. Start Docker: `sudo systemctl start docker` (Linux) or open Docker Desktop (Mac/Windows).",
                "4. Restart the MCE MCP server — full functionality will be restored automatically.",
            ],
        }

    return mcp


async def initialize_server(config: MCEConfig, mcp: FastMCP) -> None:
    """Run startup initialization: load registry and initialize cache.

    Args:
        config: MCE configuration.
        mcp: FastMCP server instance (used to access registry/cache via closure — handled elsewhere).
    """
    # Registry and cache are loaded via the create_server closure
    # This function can be used for pre-flight checks
    registry = Registry(config.compiled_output_dir)
    registry.load()

    cache = CacheStore(config.cache_db_path, config.cache_ttl_seconds, config.cache_max_entries)
    await cache.initialize()
    await cache.cleanup_expired()

    logger.info(
        "mce_server_initialized",
        compiled_dir=config.compiled_output_dir,
        cache_db=config.cache_db_path,
        log_level=config.log_level,
    )
