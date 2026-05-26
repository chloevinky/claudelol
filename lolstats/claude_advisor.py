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
- NEVER describe how a specific enemy ability works. You have not been told \
the current patch's ability text and you must not invent it. If you don't \
know, do not write it down.
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

        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
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
                    "system": SYSTEM_PROMPT,
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

    def clear_cache(self) -> None:
        self._cache.clear()
