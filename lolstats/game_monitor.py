"""Background poll loop that watches the LoL Live Client Data API."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from . import live_client

log = logging.getLogger(__name__)


class GameMonitor:
    """Polls the LoL client and pushes state updates to subscribers."""

    def __init__(
        self,
        get_config: Callable[[], dict[str, Any]],
        on_state_change: Callable[[dict[str, Any]], Any],
    ):
        self._get_config = get_config
        self._on_state_change = on_state_change
        self._task: asyncio.Task | None = None
        self._client: live_client.LiveClient | None = None
        self.state: dict[str, Any] = {
            "in_game": False,
            "snapshot": None,
            "fingerprint": None,
            "last_poll": 0.0,
            "last_game_start": None,
        }

    async def _emit(self) -> None:
        try:
            res = self._on_state_change(self.state)
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            log.exception("GameMonitor: subscriber failed")

    async def _loop(self) -> None:
        cfg = self._get_config()
        host = cfg.get("live_client_host", "https://127.0.0.1:2999")
        self._client = live_client.LiveClient(base_url=host)
        try:
            while True:
                cfg = self._get_config()
                interval = max(1.0, float(cfg.get("poll_interval_seconds", 3.0)))
                try:
                    raw = await self._client.all_game_data()
                except Exception:
                    log.exception("GameMonitor: poll error")
                    raw = None

                now = time.time()
                self.state["last_poll"] = now

                if raw is None:
                    if self.state["in_game"]:
                        log.info("GameMonitor: game ended")
                        self.state["in_game"] = False
                        self.state["snapshot"] = None
                        self.state["fingerprint"] = None
                        await self._emit()
                else:
                    snap = live_client.reduce_snapshot(raw)
                    fp = live_client.state_fingerprint(snap)
                    if not self.state["in_game"]:
                        log.info("GameMonitor: game detected")
                        self.state["in_game"] = True
                        self.state["last_game_start"] = now
                    if fp != self.state["fingerprint"]:
                        self.state["snapshot"] = snap
                        self.state["fingerprint"] = fp
                        await self._emit()
                    else:
                        self.state["snapshot"] = snap

                await asyncio.sleep(interval)
        finally:
            if self._client is not None:
                await self._client.close()
                self._client = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="game-monitor")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
