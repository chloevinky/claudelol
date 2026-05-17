"""Fetch and cache champion / item / rune data from Riot's Data Dragon CDN."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from . import db

log = logging.getLogger(__name__)

DDRAGON_BASE = "https://ddragon.leagueoflegends.com"
VERSIONS_URL = f"{DDRAGON_BASE}/api/versions.json"


def asset_url(version: str, kind: str, name: str) -> str:
    """Build a CDN URL for a champion / item / spell square icon."""
    return f"{DDRAGON_BASE}/cdn/{version}/img/{kind}/{name}"


async def _get_json(client: httpx.AsyncClient, url: str) -> Any:
    r = await client.get(url, timeout=30.0)
    r.raise_for_status()
    return r.json()


async def latest_version(client: httpx.AsyncClient | None = None) -> str:
    own = client is None
    if client is None:
        client = httpx.AsyncClient()
    try:
        versions = await _get_json(client, VERSIONS_URL)
        if not isinstance(versions, list) or not versions:
            raise RuntimeError("Empty versions list from Data Dragon")
        return versions[0]
    finally:
        if own:
            await client.aclose()


async def fetch_patch(db_path: Path, version: str, locale: str = "en_US") -> dict[str, Any]:
    """Download champions, items, runes, and summoner spells for `version`."""
    async with httpx.AsyncClient() as client:
        champ_url = f"{DDRAGON_BASE}/cdn/{version}/data/{locale}/championFull.json"
        items_url = f"{DDRAGON_BASE}/cdn/{version}/data/{locale}/item.json"
        runes_url = f"{DDRAGON_BASE}/cdn/{version}/data/{locale}/runesReforged.json"
        spells_url = f"{DDRAGON_BASE}/cdn/{version}/data/{locale}/summoner.json"
        champ_payload, items_payload, runes_payload, spells_payload = await asyncio.gather(
            _get_json(client, champ_url),
            _get_json(client, items_url),
            _get_json(client, runes_url),
            _get_json(client, spells_url),
        )

    champions_data = champ_payload.get("data", {})
    items_data = items_payload.get("data", {})
    spells_data = spells_payload.get("data", {})

    db.insert_champions(db_path, version, list(champions_data.values()))
    db.insert_items(db_path, version, items_data)
    db.insert_runes(db_path, version, runes_payload)
    db.insert_summoner_spells(db_path, version, spells_data)
    db.upsert_meta(db_path, version, locale, int(time.time()))

    return {
        "version": version,
        "locale": locale,
        "counts": {
            "champions": len(champions_data),
            "items": len(items_data),
            "runes": sum(
                1
                + sum(len(s.get("runes", [])) for s in tree.get("slots", []))
                for tree in runes_payload
            ),
            "summoner_spells": len(spells_data),
        },
    }


async def ensure_current_patch(
    db_path: Path, locale: str, force: bool = False
) -> dict[str, Any]:
    version = await latest_version()
    if not force and db.have_version(db_path, version):
        log.info("Data Dragon: already have %s", version)
        return {"version": version, "skipped": True, **db.patch_summary(db_path)}
    log.info("Data Dragon: fetching patch %s (%s)", version, locale)
    return await fetch_patch(db_path, version, locale=locale)


class PatchWatcher:
    """Background task that periodically checks for a new patch."""

    def __init__(self, db_path: Path, locale: str, interval_seconds: float):
        self.db_path = db_path
        self.locale = locale
        self.interval = max(60.0, interval_seconds)
        self._task: asyncio.Task | None = None
        self.last_check: float = 0.0
        self.last_version: str | None = None

    async def _loop(self) -> None:
        failures = 0
        while True:
            try:
                summary = await ensure_current_patch(self.db_path, self.locale)
                self.last_version = summary.get("version")
                self.last_check = time.time()
                failures = 0
                sleep_for = self.interval
            except Exception:
                failures += 1
                log.exception("Patch watcher: failed to refresh (attempt %d)", failures)
                sleep_for = min(60.0 * (2 ** (failures - 1)), self.interval)
            await asyncio.sleep(sleep_for)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="patch-watcher")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
