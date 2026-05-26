#!/usr/bin/env python3
"""Run the LoL Live Coach server locally.

Usage:
    python run.py [--host 127.0.0.1] [--port 8765]

Opens http://127.0.0.1:8765 in your browser by default. Use --no-open to
skip opening the browser.
"""
from __future__ import annotations

import argparse
import webbrowser

import uvicorn

from lolstats.logging_setup import setup as setup_logging, GENERAL_LOG, CLAUDE_TRACE


def main() -> None:
    parser = argparse.ArgumentParser(description="LoL Live Coach server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true", help="Restart on code changes (dev)")
    parser.add_argument("--no-open", action="store_true", help="Don't open a browser tab")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logs_dir = setup_logging(args.log_level)
    print(f"Logs: {logs_dir}")
    print(f"  general:      {GENERAL_LOG}")
    print(f"  claude trace: {CLAUDE_TRACE}  (send this when reporting bad advice)")

    if not args.no_open and not args.reload:
        try:
            webbrowser.open(f"http://{args.host}:{args.port}", new=2)
        except Exception:
            pass

    uvicorn.run(
        "lolstats.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
