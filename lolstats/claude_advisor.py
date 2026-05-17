"""Ask Claude for live build advice using web search.

We send a compact game-state JSON, plus the current patch version, to Claude
with the `web_search_20260209` tool. The model is instructed to return advice
as a JSON object via `output_config.format`. Results are cached by a state
fingerprint to avoid hammering the API on tiny state changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import anthropic

log = logging.getLogger(__name__)


ADVICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One-line tactical summary (under 80 chars).",
        },
        "power_spike": {
            "type": "string",
            "description": "Where your champ is in its curve right now (e.g. 'weak laning, scales after 2 items').",
        },
        "best_items": {
            "type": "array",
            "description": "3-6 items to build right now, in priority order.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "reason": {"type": "string", "description": "Under 15 words."},
                },
                "required": ["name", "reason"],
                "additionalProperties": False,
            },
        },
        "counter_items": {
            "type": "array",
            "description": "Items to buy against specific enemies. 2-5 entries.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "against": {"type": "string", "description": "Enemy champion or threat."},
                    "reason": {"type": "string", "description": "Under 15 words."},
                },
                "required": ["name", "against", "reason"],
                "additionalProperties": False,
            },
        },
        "tips": {
            "type": "array",
            "description": "2-4 short tactical tips. Under 18 words each.",
            "items": {"type": "string"},
        },
        "lane_matchup": {
            "type": "string",
            "description": "Lane matchup difficulty: 'favored', 'even', or 'losing', plus a short reason.",
        },
    },
    "required": ["summary", "best_items", "counter_items", "tips"],
    "additionalProperties": False,
}


SYSTEM_PROMPT = """You are a League of Legends in-game coach. The user is currently \
in a live match and needs concise, actionable advice. Use the web_search tool to find \
up-to-date build / counter / matchup information for the current patch when needed \
(e.g. u.gg, op.gg, mobalytics, lolalytics, official patch notes).

Rules:
- Keep every "reason" / "tip" under ~15 words. Pretend the user has 5 seconds to read.
- Prefer concrete item names from the current patch over generic stat advice.
- Pick counter-items that target specific threats on the enemy team (heavy AD/AP/healing/CC/burst).
- If their build is already optimal, say so in `summary` and recommend the next slot.
- Never invent items. If unsure, search the web."""


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


class ClaudeAdvisor:
    """Async wrapper around the Anthropic SDK with state-fingerprint caching."""

    def __init__(self, get_config):
        self._get_config = get_config
        self._cache: dict[str, AdvisorResponse] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._last_request_at: float = 0.0

    @property
    def _client(self) -> anthropic.AsyncAnthropic | None:
        cfg = self._get_config()
        key = cfg.get("anthropic_api_key") or ""
        if not key:
            return None
        return anthropic.AsyncAnthropic(api_key=key)

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
        client = self._client
        if client is None:
            raise RuntimeError("No Anthropic API key configured")

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
            "Live game state below. Please use web search to look up build / "
            "counter / matchup info for the *current patch* if you're not sure, "
            "then return advice as JSON matching the schema.\n\n```json\n"
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

        log.info("Claude advisor: requesting advice (model=%s, fp=%s)", model, fingerprint)
        response = await client.messages.create(**kwargs)

        text = ""
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text = block.text
                break

        advice: dict[str, Any] = {}
        if text:
            try:
                advice = json.loads(text)
            except json.JSONDecodeError:
                start = text.find("{")
                end = text.rfind("}")
                if start != -1 and end > start:
                    try:
                        advice = json.loads(text[start : end + 1])
                    except json.JSONDecodeError:
                        log.warning("Claude advisor: response was not valid JSON")

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
