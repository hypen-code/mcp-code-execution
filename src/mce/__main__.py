"""MCE CLI entry point — supports `compile`, `clean`, `serve`, and combined `run` commands."""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from mce.config import _ENV_FILE, load_config
from mce.utils.logging import get_logger, setup_logging


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        prog="mce",
        description="MCE — MCP Code Execution: Turn any Swagger into LLM-native functions",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        metavar="PATH",
        help="Path to a custom .env file (default: .env in current directory)",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # clean subcommand
    clean_parser = subparsers.add_parser("clean", help="Remove the compiled output directory and cache database")
    clean_parser.add_argument(
        "then",
        nargs="?",
        choices=["compile"],
        metavar="compile",
        help="Optionally run compile immediately after cleaning",
    )
    clean_parser.add_argument(
        "--llm-enhance",
        action="store_true",
        help="(with compile) Use LLM to improve generated code quality",
    )
    clean_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="(with compile) Parse swaggers but don't write output",
    )

    # compile subcommand
    compile_parser = subparsers.add_parser("compile", help="Compile swagger sources to Python functions")
    compile_parser.add_argument(
        "--llm-enhance",
        action="store_true",
        help="Use LLM to improve generated code quality",
    )
    compile_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse swaggers but don't write output",
    )

    # serve subcommand
    serve_parser = subparsers.add_parser("serve", help="Start the MCP server")
    serve_parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport mode (default: stdio)",
    )
    serve_parser.add_argument(
        "--host",
        default=None,
        help="Override host for HTTP transport",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override port for HTTP transport",
    )

    # run subcommand (compile + serve)
    run_parser = subparsers.add_parser("run", help="Compile then start the MCP server")
    run_parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport mode (default: stdio)",
    )

    return parser


async def _cmd_clean(args: argparse.Namespace) -> int:
    """Remove the compiled output directory and the cache database.

    Args:
        args: Parsed CLI arguments (may include ``then="compile"``).

    Returns:
        Exit code (0 = success).
    """
    import shutil  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    config = load_config(getattr(args, "env_file", None))

    # Remove compiled output directory
    compiled_dir = Path(config.compiled_output_dir)
    if compiled_dir.exists():
        shutil.rmtree(compiled_dir)
        print(f"🗑  Removed: {compiled_dir}")
    else:
        print(f"⏭  Nothing to clean: {compiled_dir!r} does not exist")

    # Remove cache database
    cache_db = Path(config.cache_db_path)
    if cache_db.exists():
        cache_db.unlink()
        print(f"🗑  Removed: {cache_db}")
    else:
        print(f"⏭  Nothing to clean: {cache_db!r} does not exist")

    if getattr(args, "then", None) == "compile":
        return await _cmd_compile(args)

    return 0


async def _cmd_compile(args: argparse.Namespace) -> int:
    """Execute the compile command.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code (0 = success).
    """
    from mce.compiler.orchestrator import Orchestrator  # noqa: PLC0415

    config = load_config(getattr(args, "env_file", None))
    orchestrator = Orchestrator(config)
    logger = get_logger(__name__)

    if args.llm_enhance:
        config.llm_enhance = True

    result = await orchestrator.compile_all(dry_run=args.dry_run)

    logger.info(
        "compile_summary",
        compiled=result.compiled,
        skipped=result.skipped,
        failed=result.failed,
        total_endpoints=result.total_endpoints,
    )

    if result.failed:
        print(f"❌ Compile failed for: {', '.join(result.failed)}", file=sys.stderr)
        return 1

    if result.compiled:
        print(f"✅ Compiled: {', '.join(result.compiled)} ({result.total_endpoints} endpoints)")
    if result.skipped:
        print(f"⏭  Skipped (up-to-date): {', '.join(result.skipped)}")

    if result.mcp_json:
        print("\n--- MCP Server Config (add to your MCP client) ---")
        print(result.mcp_json)

    return 0


async def _cmd_serve(config_args: argparse.Namespace) -> int:
    """Start the MCP server.

    Args:
        config_args: Parsed CLI arguments.

    Returns:
        Exit code.
    """
    from mce.compiler.orchestrator import Orchestrator, _to_module_name  # noqa: PLC0415
    from mce.runtime.cache import CacheStore  # noqa: PLC0415
    from mce.runtime.executor import CodeExecutor  # noqa: PLC0415
    from mce.runtime.registry import Registry  # noqa: PLC0415
    from mce.server import create_server  # noqa: PLC0415

    config = load_config(getattr(config_args, "env_file", None))
    logger = get_logger(__name__)

    if config_args.host:
        config.host = config_args.host
    if getattr(config_args, "port", None):
        config.port = config_args.port

    # Initialize cache
    cache = CacheStore(config.cache_db_path, config.cache_ttl_seconds, config.cache_max_entries)
    await cache.initialize()
    await cache.cleanup_expired()

    # Load registry
    registry = Registry(config.compiled_output_dir)
    registry.load()

    # Load auth configs from swaggers.yaml so dynamic token fetching (Keycloak, OAuth2, etc.)
    # is available at runtime without requiring MCE_{SERVER}_AUTH env vars.
    orchestrator = Orchestrator(config)
    sources = orchestrator.load_swagger_sources()
    auth_configs = {_to_module_name(s.name): s.auth for s in sources if s.auth is not None}

    servers = registry.list_servers()
    logger.info(
        "mce_starting",
        servers=[s.name for s in servers],
        transport=getattr(config_args, "transport", "stdio"),
        sandbox_mode=config.sandbox_mode,
        warm_pool_size=config.warm_pool_size if config.sandbox_mode == "warm" else 0,
        host=config.host,
        port=config.port,
    )

    # Start executor (creates warm container pool if sandbox_mode=warm).
    # startup() is inside the try so shutdown() always runs — even if startup
    # fails mid-way (e.g. first container created, second raises), ensuring
    # no warm containers are left orphaned in Docker.
    executor = CodeExecutor(config, cache, auth_configs)
    try:
        await executor.startup()
        mcp = create_server(config, registry=registry, cache=cache, executor=executor)
        transport = getattr(config_args, "transport", "stdio")

        if transport == "stdio":
            await mcp.run_stdio_async()
        else:
            await mcp.run_http_async(host=config.host, port=config.port)
    finally:
        # Stop and remove all warm containers, close Docker client
        await executor.shutdown()

    return 0


async def _cmd_run(args: argparse.Namespace) -> int:
    """Compile then serve.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code.
    """
    from mce.compiler.orchestrator import Orchestrator  # noqa: PLC0415

    config = load_config(getattr(args, "env_file", None))
    orchestrator = Orchestrator(config)

    result = await orchestrator.compile_all()
    if result.failed:
        print(f"❌ Compile failed for: {', '.join(result.failed)}", file=sys.stderr)
        return 1

    return await _cmd_serve(args)


def main() -> None:
    """CLI entry point invoked by `mce` script or `python -m mce`."""
    parser = _build_parser()
    args = parser.parse_args()

    # Load .env into os.environ early so vault.py can read server credentials.
    # override=False means explicit env vars always win over .env values.
    env_file_path = args.env_file if args.env_file else str(_ENV_FILE)
    load_dotenv(env_file_path, override=False)

    # Load config early for log level
    try:
        config = load_config(args.env_file)
        setup_logging(config.log_level)
    except Exception:  # noqa: BLE001
        setup_logging("INFO")

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    command_map = {
        "clean": _cmd_clean,
        "compile": _cmd_compile,
        "serve": _cmd_serve,
        "run": _cmd_run,
    }

    handler = command_map.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    exit_code = asyncio.run(handler(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
