# SIMD Pattern

**SIMD** stands for **Single Instruction, Multiple Data** — borrowed from CPU architecture, applied to LLM code execution.

The core idea: write code once, run it many times with different input values. The LLM never rewrites the same logic twice.

---

## The Problem Without SIMD

Without caching and parameter injection, an LLM asking "What's the weather in London, Paris, and Tokyo?" would:

1. Call `list_servers()`
2. Call `get_functions(...)`
3. Write code for London → `execute_code(...)`
4. Write **identical code** for Paris → `execute_code(...)`
5. Write **identical code** again for Tokyo → `execute_code(...)`

Steps 4 and 5 waste tokens rewriting code the model already wrote. In a 50-city iteration, the wasted context grows linearly.

---

## The SIMD Solution

The `run_cached_code` tool solves this. After the first successful `execute_code`, every subsequent call with different data skips straight to execution:

```
execute_code(city="London") → cache_id: "abc123"
run_cached_code("abc123", {city: "Paris"}) → result
run_cached_code("abc123", {city: "Tokyo"}) → result
run_cached_code("abc123", {city: "Sydney"}) → result
```

Same code. Different data. No rewriting.

---

## Writing SIMD-Compatible Code

### The Top-Level Variable Rule

**All dynamic values must be top-level variables that `main()` reads as globals.**

```python
# CORRECT — city is a top-level variable
from weather.functions import get_current_weather

city = "London"       # ← top-level: injectable via run_cached_code
units = "metric"      # ← top-level: injectable if it ever changes

def main():
    return get_current_weather(city=city, units=units)

result = main()
```

```python
# INCORRECT — hardcoded inside main()
from weather.functions import get_current_weather

def main():
    return get_current_weather(city="London", units="metric")   # ← cannot inject

result = main()
```

When `run_cached_code("abc123", params={"city": "Paris"})` runs, MCE prepends the injected assignments to the cached code:

```python
# Injected by run_cached_code:
city = "Paris"

# Then the original cached code runs:
from weather.functions import get_current_weather
units = "metric"
def main():
    return get_current_weather(city=city, units=units)
result = main()
```

The injected assignment shadows the original top-level value — `city = "Paris"` overrides `city = "London"`.

---

## Multi-Parameter Injection

Any number of top-level variables can be injected simultaneously:

```python
# Original code
from hotel_booking.functions import search_hotels

city = "London"
check_in = "2026-04-01"
check_out = "2026-04-05"
guests = 2

def main():
    return search_hotels(
        city=city,
        check_in=check_in,
        check_out=check_out,
        guests=guests,
    )

result = main()
```

Reuse with different dates and city:

```python
run_cached_code("xyz789", params={
    "city": "Paris",
    "check_in": "2026-05-10",
    "check_out": "2026-05-15",
})
# guests stays as 2 — only the injected keys are overridden
```

---

## Multi-API SIMD

SIMD works across multiple APIs in the same code snippet:

```python
from weather.functions import get_current_weather
from hotel_booking.functions import search_hotels

city = "London"
check_in = "2026-04-01"
check_out = "2026-04-05"

def main():
    weather = get_current_weather(city=city)
    hotels = search_hotels(city=city, check_in=check_in, check_out=check_out)
    return {"weather": weather, "hotels": hotels}

result = main()
```

This fetches weather and hotel availability for the same city in a single sandbox execution. To repeat for a different city and dates:

```python
run_cached_code("cache_id_here", params={
    "city": "Paris",
    "check_in": "2026-05-10",
    "check_out": "2026-05-15",
})
```

---

## Cache Behavior

### Cache Key

The cache key is a SHA-256 hash of the **normalized code** — whitespace-normalized and sorted imports. This means:

- The exact same code always produces the same `cache_id`.
- Trivial formatting differences (extra blank lines) do not create duplicate cache entries.
- `run_cached_code` with injected `params` produces a **new** `cache_id` (the injected code is different), but the original is still available.

### TTL and Eviction

| Setting | Default |
|---------|---------|
| `MCE_CACHE_TTL_SECONDS` | 3600 (1 hour) |
| `MCE_CACHE_MAX_ENTRIES` | 500 |
| Eviction policy | LRU (least recently used) |

When the same `cache_id` is executed again, the entry's `use_count` increments and `last_used_at` is updated. Frequently used entries stay warm even across multiple TTL periods as long as `max_entries` is not exceeded.

### Cache Storage

The cache is an async SQLite database at `MCE_CACHE_DB_PATH` (default `./data/cache.db`). It is local to the MCE instance — no shared cache between multiple instances (Redis-backed shared cache is on the [Roadmap](Roadmap)).

---

## The `reusable_code_guide` Prompt

If the LLM is not following the top-level variable pattern, call the built-in prompt:

```
reusable_code_guide()
```

It returns a concise set of rules reminding the model to:

1. Identify all dynamic values before writing code
2. Declare each one as a top-level variable
3. Have `main()` read from globals, not from hardcoded literals
4. Include `result = main()` at module level
5. Use `run_cached_code` for subsequent calls with different data

---

## Summary

| Pattern | Code | Tokens |
|---------|------|--------|
| Without SIMD (N cities) | N unique snippets | O(N × code_size) |
| With SIMD (N cities) | 1 snippet + N `run_cached_code` calls | O(code_size + N × param_size) |

For any loop-like task — iterating cities, users, IDs, dates — the SIMD pattern is the correct approach.

---

*Next: [Contributing](Contributing) →*
