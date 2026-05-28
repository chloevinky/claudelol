"""Ask Claude for itemization advice using web search.

The model is told to recommend items based on STAT-LEVEL traits of the enemy
team (healing, heavy AD, heavy AP, burst, hard CC, mobility, attack speed,
shielding). It is NOT to describe how individual abilities work — that
constraint is enforced in the system prompt because the model can hallucinate
specifics about ability mechanics.

A full trace of every request (request payload + response content blocks
including web-search results + parsed advice) is appended as one JSON line
per call to `~/.lolstats/logs/claude.jsonl` so a user can share it for review.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import anthropic

from . import config, db
from .logging_setup import CLAUDE_TRACE

log = logging.getLogger(__name__)


ADVICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One-line itemization plan (under 80 chars).",
        },
        "lane_opponent": {
            "type": "string",
            "description": (
                "Lane opponent name + 1-2 stat-level traits to itemize against, "
                "e.g. 'Darius - heavy AD, strong sustain'. Do NOT describe "
                "specific ability mechanics."
            ),
        },
        "power_spike": {
            "type": "string",
            "description": (
                "When your champ spikes, in terms of item / level milestones. "
                "Do not describe specific ability mechanics."
            ),
        },
        "best_items": {
            "type": "array",
            "description": "3-6 items for the user's core build, in priority order.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "reason": {
                        "type": "string",
                        "description": "Under 15 words. Stick to stats/role, not ability descriptions.",
                    },
                },
                "required": ["name", "reason"],
                "additionalProperties": False,
            },
        },
        "counter_items": {
            "type": "array",
            "description": (
                "2-5 items chosen to counter STAT-LEVEL traits of the enemy team "
                "(prefer the user's lane opponent first)."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "against": {
                        "type": "string",
                        "description": (
                            "The STAT-LEVEL trait this item counters, e.g. "
                            "'healing', 'heavy AD', 'heavy AP', 'burst', "
                            "'hard CC', 'mobility', 'attack speed', 'shielding'. "
                            "NOT a specific ability name."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "Under 15 words. Reference the stat/trait, not the ability.",
                    },
                },
                "required": ["name", "against", "reason"],
                "additionalProperties": False,
            },
        },
        "tips": {
            "type": "array",
            "description": (
                "2-4 short tips. ONLY itemization timing or game-flow advice "
                "(when to rush an item, when to back, who scales). NEVER claims "
                "about how an enemy ability works."
            ),
            "items": {"type": "string"},
        },
    },
    "required": ["summary", "lane_opponent", "best_items", "counter_items", "tips"],
    "additionalProperties": False,
}


SYSTEM_PROMPT = """You are a League of Legends in-game ITEMIZATION coach. The user is \
in a live match and needs concise itemization advice for the current patch.

YOUR JOB: Recommend items based on stat-level traits of the enemy team — \
healing, heavy AD, heavy AP, burst, hard CC, mobility, attack speed, shielding. \
Focus on the user's LANE OPPONENT first.

USE web_search when:
- Looking up the current-patch core build for the user's champion + role
- Looking up matchup-specific item priorities (e.g. "Aatrox vs Darius items")

HARD RULES — do NOT break these:
- NEVER describe how a specific enemy ability works. Reason ONLY from the \
stat-level traits you are given (plus web_search for builds); never invent \
ability mechanics or numbers. If you don't know, do not write it down.
- NEVER give "bait this ability" or "dodge that ability" style tactical tips.
- NEVER reference an enemy ability by name in `tips`, `reason`, or `against`. \
Refer to traits ("healing", "heavy AD", etc.) instead.
- NEVER invent items. If you're unsure an item exists in this patch, search.

GOOD counter-item examples:
- {"name": "Bramble Vest", "against": "healing", "reason": "anti-heal vs sustain bruisers"}
- {"name": "Plated Steelcaps", "against": "heavy AD / autos", "reason": "AD lane, basic-attack mitigation"}
- {"name": "Mercury's Treads", "against": "hard CC", "reason": "tenacity vs CC-heavy enemy team"}

BAD counter-item examples (do NOT do this):
- "Bait his Q before all-in" — describes ability mechanics
- "Dodge the spinning axe" — references an ability
- {"against": "Darius Q"} — names an ability instead of a trait

FORMAT:
- Keep every `reason` / `tip` under ~15 words.
- best_items must be the standard current-patch core build for the user's champion."""


# --- Pre-game setup advice (runes / spells / starting items) -----------------
#
# Runes, summoner spells and starting items are all locked BEFORE the match
# starts, so the live in-game flow can't help with them. This is a standalone
# Q&A: the user picks a champion + role + topic from dropdowns and we return a
# standard current-patch setup. Answers are cached per (topic, champ, role,
# patch) so re-asking never spends tokens.

PREGAME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One-line recommendation (under 90 chars).",
        },
        "groups": {
            "type": "array",
            "description": "Recommendation groups, in the order they're picked.",
            "items": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": (
                            "Group name, e.g. 'Keystone', 'Primary: Precision', "
                            "'Secondary: Resolve', 'Stat Shards', 'Summoner Spells', "
                            "'Starting Items'."
                        ),
                    },
                    "picks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exact rune / spell / item names, in order.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Under 15 words, stat/role level — no ability mechanics.",
                    },
                },
                "required": ["label", "picks"],
                "additionalProperties": False,
            },
        },
        "notes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "0-3 short setup notes (matchup flex, when to swap). No ability mechanics.",
        },
    },
    "required": ["summary", "groups"],
    "additionalProperties": False,
}

PREGAME_TOPICS: dict[str, str] = {
    "runes": (
        "Recommend the standard current-patch RUNE PAGE. Provide groups in this "
        "order: 'Keystone' (1 pick), 'Primary: <tree>' (the 3 minor runes), "
        "'Secondary: <tree>' (2 runes), and 'Stat Shards' (the 3 shards: "
        "offense / flex / defense). Name every rune, tree, and shard exactly."
    ),
    "summoner_spells": (
        "Recommend the standard current-patch SUMMONER SPELLS as a single group "
        "'Summoner Spells' with the 2 spells, plus any common alternative as a note."
    ),
    "starting_items": (
        "Recommend the standard current-patch STARTING ITEMS as a group "
        "'Starting Items' (items + consumables bought on first back-to-base 0)."
    ),
}

PREGAME_SYSTEM = """You are a League of Legends PRE-GAME setup coach for the current patch. \
The user is at champion select / loading screen and must lock in runes, summoner \
spells, and starting items before the game begins.

YOUR JOB: Give the standard, current-patch setup for the requested champion and \
role. Prefer the most popular / highest-winrate option.

USE web_search to confirm the current-patch meta build (op.gg, u.gg, mobalytics, \
lolalytics). Always search — rune/spell metas shift between patches.

HARD RULES:
- Name runes, keystones, trees, shards, spells, and items EXACTLY as they appear \
in-client this patch. Never invent names.
- Keep every `reason` and note under ~15 words.
- Do NOT describe how abilities work or give in-lane mechanical tips. Stay at the \
setup / stat / role level.
- If you are unsure something exists in the current patch, search rather than guess."""


def _parse_json_blocks(response: Any) -> tuple[dict[str, Any], str]:
    """Return ``(parsed_obj, raw_text)`` from the last JSON-bearing text block.

    With web_search the model emits text before and after tool calls; the answer
    is in the final block, so walk in reverse and parse the first valid JSON.
    """
    text = ""
    text_blocks = [
        b.text for b in response.content
        if getattr(b, "type", None) == "text" and getattr(b, "text", "")
    ]
    for candidate in reversed(text_blocks):
        text = candidate
        try:
            return json.loads(candidate), candidate
        except json.JSONDecodeError:
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(candidate[start : end + 1]), candidate
                except json.JSONDecodeError:
                    continue
    return {}, text


@dataclass
class AdvisorResponse:
    advice: dict[str, Any]
    requested_at: float
    fingerprint: str
    patch_version: str | None
    cached: bool = False
    error: str | None = None
    model: str | None = None
    raw_text: str | None = None


def _dump_block(block: Any) -> dict[str, Any]:
    """Best-effort serialize an SDK response content block to a JSON-safe dict."""
    try:
        return block.model_dump(mode="json")
    except Exception:
        pass
    out: dict[str, Any] = {"type": getattr(block, "type", "unknown")}
    for attr in ("text", "id", "name", "input", "content", "title", "url"):
        v = getattr(block, attr, None)
        if v is None:
            continue
        try:
            json.dumps(v)
            out[attr] = v
        except (TypeError, ValueError):
            out[attr] = repr(v)
    return out


def _write_trace(record: dict[str, Any]) -> None:
    try:
        CLAUDE_TRACE.parent.mkdir(parents=True, exist_ok=True)
        with CLAUDE_TRACE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        log.exception("Failed to write Claude trace")


# --- Distilled Data Dragon reference -----------------------------------------
#
# When the distiller has populated facts for the current patch, we ground the
# advisor with exact item numbers + counter tags and champion threat profiles.
# Everything here degrades gracefully: any miss returns None and the advisor
# behaves exactly as it did before (web_search only).


def _fmt_item(f: dict[str, Any]) -> str:
    parts = [f.get("name", "?")]
    if f.get("gold") is not None:
        parts.append(f"{f['gold']}g")
    stats = f.get("stats") or {}
    stat_str = " ".join(f"{k}={v}" for k, v in stats.items() if v)
    if stat_str:
        parts.append(stat_str)
    effects = [
        (e.get("notes") or e.get("name") or "").strip()
        for e in (f.get("effects") or [])
    ]
    effects = [e for e in effects if e]
    if effects:
        parts.append("fx[" + "; ".join(effects) + "]")
    if f.get("counters"):
        parts.append("counters:" + ",".join(f["counters"]))
    if f.get("grants"):
        parts.append("grants:" + ",".join(f["grants"]))
    return " | ".join(parts)


def _fmt_champion(f: dict[str, Any]) -> str:
    head = f.get("name", "?")
    tags = f.get("tags") or []
    if tags:
        head += f" ({','.join(tags)})"
    parts = [head]
    dmg = f.get("damage") or {}
    dpieces = [
        f"{lbl}{dmg[k]}"
        for k, lbl in (("ad_pct", "ad"), ("ap_pct", "ap"), ("true_pct", "true"))
        if dmg.get(k)
    ]
    if dpieces:
        parts.append("dmg " + "/".join(dpieces))
    if f.get("traits"):
        parts.append("traits:" + ",".join(f["traits"]))
    if f.get("cc"):
        parts.append("cc:" + ",".join(f["cc"]))
    if f.get("mobility"):
        parts.append("mob:" + f["mobility"])
    if f.get("range"):
        parts.append(f["range"])
    if f.get("sustain"):
        parts.append("sustain")
    if f.get("notes"):
        parts.append(f"({f['notes']})")
    return " | ".join(parts)


def _fmt_trait(f: dict[str, Any]) -> str:
    out = f.get("name", "?")
    if f.get("traits"):
        out += ": " + ",".join(f["traits"])
    if f.get("notes"):
        out += f" — {f['notes']}"
    return out


def _collect_unique(values) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def build_reference(
    snapshot: dict[str, Any], patch_version: str | None, cfg: dict[str, Any]
) -> dict[str, str] | None:
    """Build cached item-catalog + per-game champion/rune/spell reference text.

    Returns ``{"item_block", "game_block", "version"}`` or ``None`` when
    Data Dragon facts are unavailable (so the advisor falls back to today's
    web_search-only behavior).
    """
    if not cfg.get("advisor_use_datadragon", True):
        return None
    try:
        version = patch_version or db.patch_summary(config.DB_PATH).get("version")
        if not version:
            return None
        catalog = db.item_facts_catalog(config.DB_PATH, version)
        if not catalog:
            return None

        me = snapshot.get("me") or {}
        allies = snapshot.get("allies") or []
        enemies = snapshot.get("enemies") or []

        champ_names = _collect_unique(
            [me.get("champion")]
            + [p.get("champion") for p in allies]
            + [p.get("champion") for p in enemies]
        )
        champs = [
            f for n in champ_names if (f := db.champion_facts(config.DB_PATH, version, n))
        ]

        full_runes = me.get("full_runes") or {}
        rune_names = _collect_unique(
            [me.get("keystone"), full_runes.get("keystone")]
            + list(full_runes.get("primary_runes") or [])
            + [p.get("keystone") for p in enemies]
        )
        runes = [
            f for n in rune_names if (f := db.rune_facts(config.DB_PATH, version, n))
        ]

        spell_names = _collect_unique(
            [s for p in [me, *enemies] for s in (p.get("summoner_spells") or [])]
        )
        spells = [
            f for n in spell_names if (f := db.spell_facts(config.DB_PATH, version, n))
        ]

        item_block = (
            f"ITEM REFERENCE (patch {version}) — exact stats, parsed effects, and the "
            "enemy traits each item COUNTERS. Recommend ONLY items in this list; match "
            "the `counters` tags to the enemy team's traits below.\n"
            + "\n".join(_fmt_item(f) for f in catalog)
        )

        game_lines: list[str] = [
            f"DISTILLED REFERENCE (patch {version}) — use ONLY these grounded "
            "stats/traits; do not invent anything beyond them."
        ]
        if champs:
            game_lines.append("\nCHAMPIONS IN THIS GAME:")
            game_lines += [_fmt_champion(f) for f in champs]
        if runes:
            game_lines.append("\nRUNES (yours + enemy keystones):")
            game_lines += [_fmt_trait(f) for f in runes]
        if spells:
            game_lines.append("\nSUMMONER SPELLS (yours + enemies'):")
            game_lines += [_fmt_trait(f) for f in spells]

        return {"version": version, "item_block": item_block, "game_block": "\n".join(game_lines)}
    except Exception:
        log.exception("Advisor: failed to build Data Dragon reference; using fallback")
        return None


class ClaudeAdvisor:
    """Async wrapper around the Anthropic SDK with state-fingerprint caching."""

    def __init__(self, get_config):
        self._get_config = get_config
        self._cache: dict[str, AdvisorResponse] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._last_request_at: float = 0.0
        self._client_cache: tuple[str, anthropic.AsyncAnthropic] | None = None

    def _client_for(self, key: str) -> anthropic.AsyncAnthropic:
        if self._client_cache is not None and self._client_cache[0] == key:
            return self._client_cache[1]
        client = anthropic.AsyncAnthropic(api_key=key)
        self._client_cache = (key, client)
        return client

    def cached(self, fingerprint: str) -> AdvisorResponse | None:
        return self._cache.get(fingerprint)

    async def get_advice(
        self,
        snapshot: dict[str, Any],
        fingerprint: str,
        patch_version: str | None,
    ) -> AdvisorResponse:
        if fingerprint in self._cache:
            cached = self._cache[fingerprint]
            return AdvisorResponse(
                advice=cached.advice,
                requested_at=cached.requested_at,
                fingerprint=cached.fingerprint,
                patch_version=cached.patch_version,
                cached=True,
                model=cached.model,
                raw_text=cached.raw_text,
            )

        async with self._lock:
            if fingerprint in self._inflight:
                return await self._inflight[fingerprint]
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._inflight[fingerprint] = future

        try:
            response = await self._do_request(snapshot, fingerprint, patch_version)
            self._cache[fingerprint] = response
            future.set_result(response)
            return response
        except Exception as exc:
            log.exception("Claude advisor request failed")
            err_resp = AdvisorResponse(
                advice={},
                requested_at=time.time(),
                fingerprint=fingerprint,
                patch_version=patch_version,
                error=str(exc),
            )
            _write_trace(
                {
                    "ts": time.time(),
                    "fingerprint": fingerprint,
                    "patch": patch_version,
                    "error": str(exc),
                }
            )
            future.set_result(err_resp)
            return err_resp
        finally:
            self._inflight.pop(fingerprint, None)

    async def _do_request(
        self,
        snapshot: dict[str, Any],
        fingerprint: str,
        patch_version: str | None,
    ) -> AdvisorResponse:
        cfg = self._get_config()
        key = cfg.get("anthropic_api_key") or ""
        if not key:
            raise RuntimeError("No Anthropic API key configured")
        client = self._client_for(key)

        cooldown = float(cfg.get("advisor_cooldown_seconds", 30))
        now = time.time()
        wait = self._last_request_at + cooldown - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = time.time()

        model = cfg.get("claude_model") or "claude-opus-4-7"
        max_uses = int(cfg.get("web_search_max_uses", 3))

        user_payload = {
            "patch": patch_version,
            "game": snapshot.get("game", {}),
            "me": snapshot.get("me"),
            "allies": snapshot.get("allies", []),
            "enemies": snapshot.get("enemies", []),
        }
        user_message = (
            "Live game state below. Recommend items the user should build "
            "(core and counters against the enemy team's stat traits). "
            "Use web search to confirm current-patch core build and matchup item "
            "priorities. Return JSON matching the schema.\n\n```json\n"
            + json.dumps(user_payload, indent=2)
            + "\n```"
        )

        # Ground the request in distilled Data Dragon facts when available. The
        # per-patch item catalog goes in a cached system block (stable across
        # games); the game-specific champion/rune/spell facts go in the user
        # message. Missing facts -> reference is None -> identical to before.
        reference = build_reference(snapshot, patch_version, cfg)
        if reference:
            user_message = user_message + "\n\n" + reference["game_block"]
            system_param: Any = [
                {"type": "text", "text": SYSTEM_PROMPT},
                {
                    "type": "text",
                    "text": reference["item_block"],
                    "cache_control": {"type": "ephemeral"},
                },
            ]
        else:
            system_param = SYSTEM_PROMPT

        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=4096,
            system=system_param,
            messages=[{"role": "user", "content": user_message}],
            tools=[
                {"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses},
            ],
            output_config={"format": {"type": "json_schema", "schema": ADVICE_SCHEMA}},
        )

        request_started_at = time.time()
        log.info(
            "Claude request: model=%s fp=%s patch=%s champ=%s enemies=%s",
            model, fingerprint, patch_version,
            (snapshot.get("me") or {}).get("champion"),
            [e.get("champion") for e in snapshot.get("enemies", [])],
        )
        response = await client.messages.create(**kwargs)

        # With web_search, the model may emit text blocks before tool calls AND
        # after. The JSON-formatted answer is in the final text block, so walk
        # the content in reverse and parse the first one that's valid JSON.
        text = ""
        advice: dict[str, Any] = {}
        text_blocks = [
            b.text for b in response.content
            if getattr(b, "type", None) == "text" and getattr(b, "text", "")
        ]
        for candidate in reversed(text_blocks):
            text = candidate
            try:
                advice = json.loads(candidate)
                break
            except json.JSONDecodeError:
                start = candidate.find("{")
                end = candidate.rfind("}")
                if start != -1 and end > start:
                    try:
                        advice = json.loads(candidate[start : end + 1])
                        break
                    except json.JSONDecodeError:
                        continue
        if not advice and text_blocks:
            log.warning("Claude advisor: no valid JSON in %d text block(s)", len(text_blocks))

        usage = {}
        try:
            usage = response.usage.model_dump(mode="json")
        except Exception:
            pass

        elapsed = time.time() - request_started_at
        log.info(
            "Claude response: fp=%s stop=%s elapsed=%.1fs usage_in=%s usage_out=%s items=%d counters=%d tips=%d",
            fingerprint,
            getattr(response, "stop_reason", None),
            elapsed,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            len(advice.get("best_items", []) or []),
            len(advice.get("counter_items", []) or []),
            len(advice.get("tips", []) or []),
        )

        _write_trace(
            {
                "ts": time.time(),
                "elapsed_seconds": elapsed,
                "fingerprint": fingerprint,
                "patch": patch_version,
                "model": model,
                "request": {
                    "system": system_param,
                    "user_message": user_message,
                    "tools": kwargs["tools"],
                    "max_tokens": kwargs["max_tokens"],
                    "output_schema": ADVICE_SCHEMA,
                },
                "response": {
                    "stop_reason": getattr(response, "stop_reason", None),
                    "usage": usage,
                    "content": [_dump_block(b) for b in response.content],
                },
                "parsed_advice": advice,
                "raw_final_text": text,
            }
        )

        return AdvisorResponse(
            advice=advice,
            requested_at=time.time(),
            fingerprint=fingerprint,
            patch_version=patch_version,
            model=model,
            raw_text=text,
        )

    # -- pre-game (runes / spells / starting items) --------------------------

    async def get_pregame_advice(
        self, topic: str, champion: str, role: str, patch_version: str | None
    ) -> AdvisorResponse:
        """Cached pre-game setup advice. Re-asking the same combo spends no tokens."""
        cache_key = f"pregame:{topic}:{champion}:{role}:{patch_version}"
        if cache_key in self._cache:
            c = self._cache[cache_key]
            return AdvisorResponse(
                advice=c.advice, requested_at=c.requested_at, fingerprint=cache_key,
                patch_version=c.patch_version, cached=True, model=c.model, raw_text=c.raw_text,
            )

        async with self._lock:
            if cache_key in self._inflight:
                return await self._inflight[cache_key]
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._inflight[cache_key] = future

        try:
            resp = await self._do_pregame_request(topic, champion, role, patch_version, cache_key)
            self._cache[cache_key] = resp
            future.set_result(resp)
            return resp
        except Exception as exc:
            log.exception("Pre-game advisor request failed")
            err = AdvisorResponse(
                advice={}, requested_at=time.time(), fingerprint=cache_key,
                patch_version=patch_version, error=str(exc),
            )
            future.set_result(err)
            return err
        finally:
            self._inflight.pop(cache_key, None)

    async def _do_pregame_request(
        self, topic: str, champion: str, role: str, patch_version: str | None, cache_key: str
    ) -> AdvisorResponse:
        cfg = self._get_config()
        key = cfg.get("anthropic_api_key") or ""
        if not key:
            raise RuntimeError("No Anthropic API key configured")
        client = self._client_for(key)

        cooldown = float(cfg.get("advisor_cooldown_seconds", 30))
        wait = self._last_request_at + cooldown - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = time.time()

        model = cfg.get("claude_model") or "claude-opus-4-7"
        max_uses = int(cfg.get("web_search_max_uses", 3))
        topic_instr = PREGAME_TOPICS.get(topic, PREGAME_TOPICS["runes"])
        role_str = role or "their usual role"

        # Ground the champion's identity in distilled facts when available.
        version = patch_version or db.patch_summary(config.DB_PATH).get("version")
        champ_block = ""
        if cfg.get("advisor_use_datadragon", True) and version:
            try:
                cf = db.champion_facts(config.DB_PATH, version, champion)
                if cf:
                    champ_block = "\n\nGrounded champion profile (stat-level): " + _fmt_champion(cf)
            except Exception:
                log.exception("Pre-game: failed to load champion facts")

        user_message = (
            f"Champion: {champion}\nRole: {role_str}\nPatch: {version or 'current'}\n\n"
            f"{topic_instr}\n\nReturn JSON matching the schema." + champ_block
        )

        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=2048,
            system=PREGAME_SYSTEM,
            messages=[{"role": "user", "content": user_message}],
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}],
            output_config={"format": {"type": "json_schema", "schema": PREGAME_SCHEMA}},
        )

        started = time.time()
        log.info("Pre-game request: topic=%s champ=%s role=%s patch=%s model=%s",
                 topic, champion, role, version, model)
        response = await client.messages.create(**kwargs)
        advice, text = _parse_json_blocks(response)
        if not advice:
            log.warning("Pre-game advisor: no valid JSON for %s %s", topic, champion)

        usage = {}
        try:
            usage = response.usage.model_dump(mode="json")
        except Exception:
            pass
        elapsed = time.time() - started
        log.info("Pre-game response: champ=%s topic=%s stop=%s elapsed=%.1fs groups=%d",
                 champion, topic, getattr(response, "stop_reason", None), elapsed,
                 len(advice.get("groups", []) or []))

        _write_trace({
            "ts": time.time(), "elapsed_seconds": elapsed, "kind": "pregame",
            "topic": topic, "champion": champion, "role": role, "patch": version,
            "model": model,
            "request": {"system": PREGAME_SYSTEM, "user_message": user_message,
                        "tools": kwargs["tools"], "output_schema": PREGAME_SCHEMA},
            "response": {"stop_reason": getattr(response, "stop_reason", None),
                         "usage": usage, "content": [_dump_block(b) for b in response.content]},
            "parsed_advice": advice, "raw_final_text": text,
        })

        return AdvisorResponse(
            advice=advice, requested_at=time.time(), fingerprint=cache_key,
            patch_version=version, model=model, raw_text=text,
        )

    def clear_cache(self) -> None:
        # Drop only in-game matchup advice; keep cached pre-game answers so the
        # "Re-query" button doesn't force a paid re-ask of runes/spells/items.
        self._cache = {k: v for k, v in self._cache.items() if k.startswith("pregame:")}
