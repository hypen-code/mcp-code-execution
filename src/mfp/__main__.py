"""MFP CLI entry point — supports `compile`, `serve`, and combined `run` commands."""

from __future__ import annotations

import argparse
import asyncio
import sys

from mfp.config import load_config
from mfp.utils.logging import get_logger, setup_logging


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        prog="mfp",
        description="MFP — ModelFunctionProtocol: Turn any Swagger into LLM-native functions",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

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


async def _cmd_compile(args: argparse.Namespace) -> int:
    """Execute the compile command.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code (0 = success).
    """
    from mfp.compiler.orchestrator import Orchestrator  # noqa: PLC0415

    config = load_config()
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

    return 0


async def _cmd_serve(config_args: argparse.Namespace) -> int:
    """Start the MCP server.

    Args:
        config_args: Parsed CLI arguments.

    Returns:
        Exit code.
    """
    from mfp.runtime.cache import CacheStore  # noqa: PLC0415
    from mfp.runtime.registry import Registry  # noqa: PLC0415
    from mfp.server import create_server  # noqa: PLC0415

    config = load_config()
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

    servers = registry.list_servers()
    logger.info(
        "mfp_starting",
        servers=[s.name for s in servers],
        transport=getattr(config_args, "transport", "stdio"),
        host=config.host,
        port=config.port,
    )

    mcp = create_server(config)
    transport = getattr(config_args, "transport", "stdio")

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="http", host=config.host, port=config.port)

    return 0


async def _cmd_run(args: argparse.Namespace) -> int:
    """Compile then serve.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code.
    """
    from mfp.compiler.orchestrator import Orchestrator  # noqa: PLC0415

    config = load_config()
    orchestrator = Orchestrator(config)

    result = await orchestrator.compile_all()
    if result.failed:
        print(f"❌ Compile failed for: {', '.join(result.failed)}", file=sys.stderr)
        return 1

    return await _cmd_serve(args)


def main() -> None:
    """CLI entry point invoked by `mfp` script or `python -m mfp`."""
    parser = _build_parser()
    args = parser.parse_args()

    # Load config early for log level
    try:
        config = load_config()
        setup_logging(config.log_level)
    except Exception:  # noqa: BLE001
        setup_logging("INFO")

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    command_map = {
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
