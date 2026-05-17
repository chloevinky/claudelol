"""User-editable configuration stored as JSON on disk."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("LOLSTATS_HOME", Path.home() / ".lolstats"))
CONFIG_PATH = CONFIG_DIR / "config.json"
DB_PATH = CONFIG_DIR / "patch_data.sqlite"

_DEFAULTS: dict[str, Any] = {
    "anthropic_api_key": "",
    "claude_model": "claude-opus-4-7",
    "advisor_enabled": True,
    "web_search_max_uses": 3,
    "poll_interval_seconds": 3.0,
    "patch_check_interval_hours": 6,
    "ddragon_locale": "en_US",
    "live_client_host": "https://127.0.0.1:2999",
    "advisor_cooldown_seconds": 30,
}

_lock = threading.Lock()


def _ensure_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load() -> dict[str, Any]:
    _ensure_dir()
    if not CONFIG_PATH.exists():
        save(_DEFAULTS)
        return dict(_DEFAULTS)
    with _lock:
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            data = {}
    merged = {**_DEFAULTS, **data}
    env_key = os.environ.get("ANTHROPIC_API_KEY")
    if env_key and not merged.get("anthropic_api_key"):
        merged["anthropic_api_key"] = env_key
    return merged


def save(data: dict[str, Any]) -> dict[str, Any]:
    _ensure_dir()
    current = {}
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                current = json.load(f)
        except json.JSONDecodeError:
            current = {}
    merged = {**_DEFAULTS, **current, **data}
    with _lock:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
        tmp.replace(CONFIG_PATH)
    return merged


def public_view(cfg: dict[str, Any]) -> dict[str, Any]:
    """Mask the API key when sending config to the browser."""
    out = dict(cfg)
    key = out.get("anthropic_api_key") or ""
    if key:
        out["anthropic_api_key"] = f"sk-...{key[-4:]}" if len(key) > 8 else "sk-..."
        out["anthropic_api_key_set"] = True
    else:
        out["anthropic_api_key"] = ""
        out["anthropic_api_key_set"] = False
    return out
