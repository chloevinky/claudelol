"""Client for the League of Legends Live Client Data API.

The League client exposes a local HTTPS server on port 2999 while a game is
active. It uses a self-signed cert so we have to disable verification. No
API key is required because the endpoint is loopback-only.

Docs: https://developer.riotgames.com/docs/lol#game-client-api
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class LiveClient:
    """Async client for the local Live Client Data API."""

    def __init__(self, base_url: str = "https://127.0.0.1:2999"):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(verify=False, timeout=4.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        r = await self._client.get(url)
        r.raise_for_status()
        return r.json()

    async def all_game_data(self) -> dict[str, Any] | None:
        """Return the full game-state snapshot, or None if no game is running."""
        try:
            return await self._get("/liveclientdata/allgamedata")
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                httpx.ConnectTimeout, httpx.ReadTimeout):
            return None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            log.warning("Live Client returned %s: %s", exc.response.status_code, exc)
            return None
        except Exception:
            log.exception("Live Client: unexpected error")
            return None


# -- snapshot reduction --------------------------------------------------

def _player_view(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "summoner": p.get("riotIdGameName") or p.get("summonerName") or "",
        "champion": p.get("championName") or "",
        "raw_champion": p.get("rawChampionName") or "",
        "position": p.get("position") or "",
        "team": p.get("team") or "",
        "level": p.get("level", 0),
        "is_dead": bool(p.get("isDead")),
        "scores": p.get("scores", {}),
        "items": [
            {"id": int(it.get("itemID", 0)), "name": it.get("displayName", "")}
            for it in (p.get("items") or [])
        ],
        "summoner_spells": [
            (p.get("summonerSpells", {}).get("summonerSpellOne") or {}).get("displayName", ""),
            (p.get("summonerSpells", {}).get("summonerSpellTwo") or {}).get("displayName", ""),
        ],
        "keystone": (p.get("runes") or {}).get("keystone", {}).get("displayName", ""),
        "primary_tree": (p.get("runes") or {}).get("primaryRuneTree", {}).get("displayName", ""),
        "secondary_tree": (p.get("runes") or {}).get("secondaryRuneTree", {}).get("displayName", ""),
    }


def reduce_snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    """Compress the giant Live Client payload to the fields we actually use."""
    active = raw.get("activePlayer") or {}
    all_players = raw.get("allPlayers") or []
    game_data = raw.get("gameData") or {}

    active_name = (
        active.get("riotIdGameName")
        or active.get("summonerName")
        or ""
    )
    me = None
    for p in all_players:
        candidate = p.get("riotIdGameName") or p.get("summonerName") or ""
        if candidate == active_name or candidate.split("#")[0] == active_name.split("#")[0]:
            me = p
            break

    me_view = _player_view(me) if me else None
    if me_view:
        me_view["current_gold"] = round(float(active.get("currentGold", 0) or 0))
        me_view["champion_stats"] = active.get("championStats", {})
        full_runes = active.get("fullRunes") or {}
        me_view["full_runes"] = {
            "keystone": (full_runes.get("keystone") or {}).get("displayName"),
            "primary_tree": (full_runes.get("primaryRuneTree") or {}).get("displayName"),
            "secondary_tree": (full_runes.get("secondaryRuneTree") or {}).get("displayName"),
            "primary_runes": [r.get("displayName") for r in full_runes.get("generalRunes", []) or []],
            "stat_runes": [r.get("displayName") for r in full_runes.get("statRunes", []) or []],
        }

    my_team = me_view["team"] if me_view else None
    allies, enemies = [], []
    for p in all_players:
        view = _player_view(p)
        if my_team and view["team"] == my_team:
            if me_view and view["summoner"] == me_view["summoner"]:
                continue
            allies.append(view)
        else:
            enemies.append(view)

    return {
        "game": {
            "mode": game_data.get("gameMode"),
            "map_name": game_data.get("mapName"),
            "map_number": game_data.get("mapNumber"),
            "game_time": round(float(game_data.get("gameTime", 0) or 0)),
        },
        "me": me_view,
        "allies": allies,
        "enemies": enemies,
        "events": (raw.get("events") or {}).get("Events", []),
    }


def state_fingerprint(snap: dict[str, Any]) -> str:
    """Stable token that changes only when the game-state changes 'meaningfully'.

    We hash:
      - The set of champion names on each side (catches game start / lobby).
      - The set of item IDs each player owns (catches purchases).
      - Whether the game is in early/mid/late phase (5-minute buckets).
    """
    import hashlib

    me = snap.get("me") or {}
    allies = snap.get("allies") or []
    enemies = snap.get("enemies") or []
    game = snap.get("game") or {}

    pieces: list[str] = []
    pieces.append(f"me={me.get('champion','')}|pos={me.get('position','')}")
    pieces.append("items=" + ",".join(str(i["id"]) for i in me.get("items", [])))
    pieces.append("allies=" + ",".join(sorted(p["champion"] for p in allies)))
    pieces.append("enemies=" + ",".join(sorted(p["champion"] for p in enemies)))
    pieces.append("ally_items=" + "/".join(
        ",".join(str(i["id"]) for i in p.get("items", []))
        for p in sorted(allies, key=lambda x: x["champion"])
    ))
    pieces.append("enemy_items=" + "/".join(
        ",".join(str(i["id"]) for i in p.get("items", []))
        for p in sorted(enemies, key=lambda x: x["champion"])
    ))
    phase = min(int(game.get("game_time", 0) // 300), 6)
    pieces.append(f"phase={phase}")
    return hashlib.sha256("|".join(pieces).encode()).hexdigest()[:16]
