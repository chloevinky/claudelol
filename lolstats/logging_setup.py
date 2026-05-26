"""Central logging setup.

Two log files live in `~/.lolstats/logs/`:

- `lolstats.log` (rotating): general server / monitor / advisor events.
- `claude.jsonl` (append-only): one JSON line per Claude API call with the
  full request and response (including web-search tool results). This is the
  file to share when reporting that advice was wrong.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import CONFIG_DIR

LOGS_DIR = CONFIG_DIR / "logs"
GENERAL_LOG = LOGS_DIR / "lolstats.log"
CLAUDE_TRACE = LOGS_DIR / "claude.jsonl"


def setup(level: str = "INFO") -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)

    rot = logging.handlers.RotatingFileHandler(
        GENERAL_LOG, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    rot.setFormatter(fmt)
    root.addHandler(rot)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    return LOGS_DIR
