"""
main.py
-------
TorProxy entry point.

Usage
-----
    python main.py                   # default settings
    python main.py --circuits 5      # 5 parallel Tor circuits
    python main.py --port 8888       # listen on port 8888
    python main.py --sequential      # disable race mode (sequential fallback)
    python main.py --no-stats        # disable /stats endpoint

Environment variables (or .env file) take precedence over CLI defaults
but CLI flags override everything.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import os
import signal
import sys
from pathlib import Path

# Force UTF-8 output on Windows (fixes UnicodeEncodeError with box-drawing chars)
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="TorProxy — async Tor-backed HTTP proxy with parallel routing",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host",      default=None, help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port",      type=int, default=None, help="Proxy port (default: 8080)")
    parser.add_argument("--circuits",  type=int, default=None, help="Number of Tor circuits")
    parser.add_argument("--sequential", action="store_true", help="Disable parallel race mode")
    parser.add_argument("--no-stats",  action="store_true", help="Disable /__torproxy__/stats")
    parser.add_argument("--log",       default=None, choices=["DEBUG","INFO","WARNING","ERROR"],
                        help="Log level")
    return parser.parse_args()


def _apply_cli_overrides(args: argparse.Namespace) -> None:
    """Push CLI flags into the environment so cfg picks them up."""
    if args.host:
        os.environ["PROXY_HOST"] = args.host
    if args.port:
        os.environ["PROXY_PORT"] = str(args.port)
    if args.circuits:
        os.environ["NUM_CIRCUITS"] = str(args.circuits)
    if args.sequential:
        os.environ["PARALLEL_RACE"] = "false"
    if args.no_stats:
        os.environ["STATS_ENDPOINT"] = "false"
    if args.log:
        os.environ["LOG_LEVEL"] = args.log


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


async def _run() -> None:
    # Late import so env overrides are applied before cfg singleton is used
    from torproxy.server import ProxyServer
    from torproxy.config import cfg

    server = ProxyServer()

    loop = asyncio.get_running_loop()

    # Graceful shutdown on SIGINT / SIGTERM
    stop_event = asyncio.Event()

    def _on_signal():
        print("\n[TorProxy] Signal received — shutting down…")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, RuntimeError):
            # Windows does not support add_signal_handler on all loops
            pass

    try:
        await server.start()
        print(
            f"\n  +-----------------------------------------------+\n"
            f"  |  TorProxy is running                         |\n"
            f"  |  Proxy  : http://{cfg.proxy_host}:{cfg.proxy_port:<5}                |\n"
            f"  |  Stats  : http://127.0.0.1:{cfg.proxy_port}/__torproxy__/stats  |\n"
            f"  |  Press Ctrl+C to stop                        |\n"
            f"  +-----------------------------------------------+\n"
        )
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await server.stop()
        print("[TorProxy] Goodbye.")


def main() -> None:
    args = _parse_args()
    _apply_cli_overrides(args)

    # Import cfg AFTER overrides are in env
    from torproxy.config import cfg
    _setup_logging(args.log or cfg.log_level)

    # Windows: use SelectorEventLoop for compatibility with subprocess + asyncio
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
