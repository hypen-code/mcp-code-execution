"""Demo script showing the MFP tool workflow for weather + hotel APIs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# This demo shows the expected LLM interaction flow with MFP tools.
# Run after: mfp compile
# ---------------------------------------------------------------------------


async def demo() -> None:
    """Run a scripted MFP workflow demonstration."""
    from mfp.config import load_config
    from mfp.runtime.cache import CacheStore
    from mfp.runtime.executor import CodeExecutor
    from mfp.runtime.registry import Registry
    from mfp.utils.logging import setup_logging

    setup_logging("INFO")
    config = load_config()

    registry = Registry(config.compiled_output_dir)
    registry.load()

    cache = CacheStore(config.cache_db_path, config.cache_ttl_seconds, config.cache_max_entries)
    await cache.initialize()

    executor = CodeExecutor(config, cache)

    print("=" * 60)
    print("MFP Demo Flow")
    print("=" * 60)

    # Step 1: List servers
    print("\n[1] list_servers()")
    servers = registry.list_servers()
    for s in servers:
        print(f"  Server: {s.name} — {s.description}")
        for fn, summary in s.function_summaries.items():
            print(f"    • {fn}: {summary}")

    if not servers:
        print("  No compiled servers found. Run: mfp compile")
        return

    # Step 2: Get function details
    first_server = servers[0]
    if first_server.functions:
        fn_name = first_server.functions[0]
        print(f"\n[2] get_function('{first_server.name}', '{fn_name}')")
        fn_info = registry.get_function(first_server.name, fn_name)
        print(f"  Summary: {fn_info.summary}")
        print(f"  Parameters: {[p.name for p in fn_info.parameters]}")

    # Step 3: Search cached code
    print("\n[3] get_cached_code(search='weather')")
    cached = await cache.search("weather")
    if cached:
        for entry in cached[:3]:
            print(f"  [{entry.id[:12]}] {entry.description} (used {entry.use_count}x)")
    else:
        print("  No cached entries yet. Execute some code first!")

    print("\n" + "=" * 60)
    print("Demo complete. Connect MFP to your MCP client to use live.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(demo())
