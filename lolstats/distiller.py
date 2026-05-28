"""Distill raw Data Dragon records into compact, numbers-first "facts".

A cheap model (Haiku by default) reads the bulky HTML-laden Data Dragon JSON
already cached in SQLite and emits small, strict fact objects: exact item
numbers + the enemy traits each item counters, champion damage/CC/mobility
profiles, and rune/spell traits. These facts ground the Opus advisor so it can
reason about counters from real numbers instead of hallucinating ability text.

The work runs once per patch (eager, in the background), is batched and
prompt-cached to keep it to pennies, and is idempotent/resumable: only records
without an up-to-date fact row are processed, so re-runs are nearly free.

CLI:  python -m lolstats.distiller [--version <patch>] [--db <path>]
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import re
from pathlib import Path
from typing import Any

from . import db

log = logging.getLogger(__name__)

# Bump when the fact shapes / prompts change so stale rows get re-distilled.
DISTILL_SCHEMA_VERSION = 1

# Controlled vocabularies shared with the advisor so both stages speak the same
# language. OFFENSE = what a champion threatens / what an item grants.
OFFENSE_TRAITS = [
    "heavy_ad", "heavy_ap", "mixed", "burst", "dps", "poke", "true_damage",
    "max_hp_damage", "crit", "on_hit", "attack_speed", "healing", "shielding",
    "sustain", "hard_cc", "soft_cc", "mobility", "tanky", "range",
]
# COUNTER = what enemy trait an item answers.
COUNTER_TRAITS = [
    "anti_heal", "armor", "magic_resist", "ad_mitigation", "tenacity",
    "anti_shield", "anti_crit", "armor_pen", "magic_pen",
]

_KIND_TO_TABLE = {
    "item": "item_facts",
    "champion": "champion_facts",
    "rune": "rune_facts",
    "spell": "spell_facts",
}
_KIND_LABEL = {
    "item": "items",
    "champion": "champions",
    "rune": "runes",
    "spell": "summoner spells",
}
_MAX_TOKENS = {"item": 8192, "champion": 8192, "rune": 4096, "spell": 2048}

_distill_lock = asyncio.Lock()

# Kinds whose grammar-constrained schema the API has rejected during the current
# run (e.g. "Schema is too complex"). Once a kind lands here we stop sending
# ``output_config`` for it and emit prompt-only JSON, avoiding a doomed (and
# slow — grammar compilation can take minutes before it times out) first call on
# every remaining batch. Reset at the start of each :func:`distill_patch` run.
_SCHEMA_DISABLED: set[str] = set()


# --- HTML / data reduction (pure, testable without the SDK) ----------------

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str | None) -> str:
    """Drop Data Dragon's pseudo-HTML tags and collapse whitespace."""
    if not text:
        return ""
    out = _TAG_RE.sub(" ", text)
    out = html.unescape(out)
    return re.sub(r"\s+", " ", out).strip()


def is_relevant_item(data: dict[str, Any]) -> bool:
    """Keep only items a player can actually buy on Summoner's Rift.

    Excludes consumables, trinkets, champion-locked items, and anything not
    purchasable or off-map, so the catalog the advisor sees stays clean.
    """
    gold = data.get("gold") or {}
    if not gold.get("purchasable", False):
        return False
    if (gold.get("total") or 0) <= 0:
        return False
    maps = data.get("maps") or {}
    if maps and maps.get("11") is False:  # 11 = Summoner's Rift
        return False
    tags = data.get("tags") or []
    if "Consumable" in tags or "Trinket" in tags:
        return False
    if data.get("requiredChampion"):
        return False
    return True


def _prepare_item(rec: dict[str, Any]) -> dict[str, Any] | None:
    data = rec["data"]
    if not is_relevant_item(data):
        return None
    gold = data.get("gold") or {}
    name = data.get("name") or rec["name"]
    model_input = {
        "id": str(rec["id"]),
        "name": name,
        "gold": gold.get("total"),
        "tags": data.get("tags") or [],
        "stats": data.get("stats") or {},
        "plaintext": data.get("plaintext") or "",
        "description": strip_html(data.get("description"))[:700],
    }
    finished = not (data.get("into"))
    return {
        "id": str(rec["id"]),
        "name": name,
        "finished": finished,
        "gold": gold.get("total"),
        "input": model_input,
    }


def _prepare_champion(rec: dict[str, Any]) -> dict[str, Any] | None:
    data = rec["data"]
    name = data.get("name") or rec["name"]
    stats = data.get("stats") or {}
    passive = data.get("passive") or {}
    spells = data.get("spells") or []
    model_input = {
        "id": str(rec["id"]),
        "name": name,
        "title": data.get("title"),
        "tags": data.get("tags") or [],
        "partype": data.get("partype"),
        "info": data.get("info") or {},
        "stats": {
            k: stats.get(k)
            for k in ("hp", "armor", "spellblock", "attackrange", "attackdamage", "movespeed")
        },
        "passive": {
            "name": passive.get("name", ""),
            "desc": strip_html(passive.get("description"))[:300],
        },
        "spells": [
            {
                "name": s.get("name", ""),
                "desc": strip_html(s.get("tooltip") or s.get("description"))[:300],
            }
            for s in spells[:4]
        ],
    }
    return {"id": str(rec["id"]), "name": name, "input": model_input}


def _prepare_rune(rec: dict[str, Any]) -> dict[str, Any] | None:
    data = rec["data"]
    if "slots" in data:  # a tree header row, not an actual rune
        return None
    name = data.get("name") or rec["name"]
    model_input = {
        "id": str(rec["id"]),
        "name": name,
        "shortDesc": strip_html(data.get("shortDesc"))[:300],
        "longDesc": strip_html(data.get("longDesc"))[:400],
    }
    return {"id": str(rec["id"]), "name": name, "input": model_input}


def _prepare_spell(rec: dict[str, Any]) -> dict[str, Any] | None:
    data = rec["data"]
    modes = data.get("modes") or []
    if modes and "CLASSIC" not in modes:  # ARAM/URF-only spells
        return None
    name = data.get("name") or rec["name"]
    model_input = {
        "id": str(rec["id"]),
        "name": name,
        "description": strip_html(data.get("description"))[:300],
    }
    return {"id": str(rec["id"]), "name": name, "input": model_input}


_PREPARE = {
    "item": _prepare_item,
    "champion": _prepare_champion,
    "rune": _prepare_rune,
    "spell": _prepare_spell,
}


# --- Structured-output schemas ---------------------------------------------

def _batch_schema(item_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"facts": {"type": "array", "items": item_schema}},
        "required": ["facts"],
        "additionalProperties": False,
    }


_ITEM_FACT = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        # Free-form numeric map (keys listed in the system prompt). Enumerating
        # all ~20 optional keys as fixed properties made the grammar-constrained
        # schema explode ("Schema is too complex" / "Grammar compilation timed
        # out"); an open object keeps the same {key: number} shape but a tiny
        # grammar.
        "stats": {
            "type": "object",
            "additionalProperties": {"type": "number"},
        },
        "effects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "number"},
                    "unit": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["name", "notes"],
                "additionalProperties": False,
            },
        },
        "counters": {"type": "array", "items": {"type": "string", "enum": COUNTER_TRAITS}},
        "grants": {"type": "array", "items": {"type": "string", "enum": OFFENSE_TRAITS}},
    },
    "required": ["id", "name", "stats", "effects", "counters", "grants"],
    "additionalProperties": False,
}

_CHAMPION_FACT = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "traits": {"type": "array", "items": {"type": "string", "enum": OFFENSE_TRAITS}},
        "damage": {
            "type": "object",
            "properties": {
                "ad_pct": {"type": "number"},
                "ap_pct": {"type": "number"},
                "true_pct": {"type": "number"},
            },
            "additionalProperties": False,
        },
        "sustain": {"type": "boolean"},
        "cc": {"type": "array", "items": {"type": "string", "enum": ["hard_cc", "soft_cc"]}},
        "mobility": {"type": "string", "enum": ["low", "med", "high"]},
        "range": {"type": "string", "enum": ["melee", "ranged"]},
        "notes": {"type": "string"},
    },
    "required": ["id", "name", "traits", "damage", "mobility", "range"],
    "additionalProperties": False,
}

_TRAIT_FACT = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "traits": {
            "type": "array",
            "items": {"type": "string", "enum": OFFENSE_TRAITS + COUNTER_TRAITS},
        },
        "notes": {"type": "string"},
    },
    "required": ["id", "name", "traits"],
    "additionalProperties": False,
}

_BATCH_SCHEMA = {
    "item": _batch_schema(_ITEM_FACT),
    "champion": _batch_schema(_CHAMPION_FACT),
    "rune": _batch_schema(_TRAIT_FACT),
    "spell": _batch_schema(_TRAIT_FACT),
}


# --- System prompts (static -> cacheable) ----------------------------------

_OFFENSE_LIST = ", ".join(OFFENSE_TRAITS)
_COUNTER_LIST = ", ".join(COUNTER_TRAITS)

_SYSTEM = {
    "item": (
        "You are a League of Legends data distiller. Convert each raw item record "
        "into a compact, numbers-first fact object. Use ONLY the data provided — "
        "never invent numbers or effects.\n"
        "For each item:\n"
        "- id, name: echo exactly from the input.\n"
        "- stats: numeric core stats granted (include only keys that apply): ad, ap, "
        "hp, armor, mr, as_pct, crit_pct, ability_haste, lethality, mpen_flat, "
        "mpen_pct, ms_flat, ms_pct, heal_shield_power, omnivamp, lifesteal, mana, "
        "mana_regen, hp5, mp5, tenacity_pct. Read from the stats map and description.\n"
        "- effects: parse the description's passive/active into numbers, one object "
        "each {name, value, unit, notes}. Capture grievous-wounds % + duration, "
        "%max-HP damage, armor/MR shred amount + duration, shield value, tenacity %, "
        "on-hit damage, etc. notes <= 20 words.\n"
        f"- counters: enemy traits this item answers, from EXACTLY this set: {_COUNTER_LIST}. "
        "Example: Bramble Vest -> [anti_heal, armor]; Mercury's Treads -> [tenacity, magic_resist].\n"
        f"- grants: traits it gives its buyer, from EXACTLY this set: {_OFFENSE_LIST}.\n"
        "Return one object per input item in the facts array."
    ),
    "champion": (
        "You are a League of Legends data distiller. Convert each champion record "
        "into a compact fact object using ONLY the data provided (tags, info ratings, "
        "base stats, ability text). Do not invent specifics.\n"
        "For each champion:\n"
        "- id, name: echo exactly.\n"
        "- tags: copy the input tags.\n"
        f"- traits: from EXACTLY this set: {_OFFENSE_LIST}. Choose those that describe the "
        "champion's threat profile (e.g. heavy_ad, healing, hard_cc, max_hp_damage, "
        "mobility, poke), using the info attack/magic ratings and ability text.\n"
        "- damage: rough source split as percentages summing ~100: {ad_pct, ap_pct, true_pct}.\n"
        "- sustain: true if the kit has notable healing/lifesteal/drain.\n"
        "- cc: subset of [hard_cc, soft_cc] the kit provides.\n"
        "- mobility: low | med | high (dashes/blinks/speed-ups).\n"
        "- range: melee | ranged (attackrange >= 300 is ranged).\n"
        "- notes: <= 20 words, stat/trait level only — NO ability mechanics.\n"
        "Return one object per input champion in the facts array."
    ),
    "rune": (
        "You are a League of Legends data distiller. Convert each rune into a fact "
        "object {id, name, traits, notes} using ONLY the provided text.\n"
        f"- traits: from EXACTLY this set: {_OFFENSE_LIST}, {_COUNTER_LIST}.\n"
        "- notes: <= 15 words, stat/trait level only.\n"
        "Echo id and name exactly. Return one object per input rune in the facts array."
    ),
    "spell": (
        "You are a League of Legends data distiller. Convert each summoner spell into "
        "a fact object {id, name, traits, notes} using ONLY the provided text.\n"
        f"- traits: from EXACTLY this set: {_OFFENSE_LIST}, {_COUNTER_LIST}.\n"
        "- notes: <= 15 words.\n"
        "Echo id and name exactly. Return one object per input spell in the facts array."
    ),
}


def _system_blocks(kind: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": _SYSTEM[kind], "cache_control": {"type": "ephemeral"}}]


# --- Model calls ------------------------------------------------------------

def _parse_facts(response: Any) -> list[dict[str, Any]]:
    """Pull the `facts` array out of the model's structured response."""
    text_blocks = [
        b.text
        for b in response.content
        if getattr(b, "type", None) == "text" and getattr(b, "text", "")
    ]
    for candidate in reversed(text_blocks):
        for blob in (candidate, _slice_json(candidate)):
            if not blob:
                continue
            try:
                parsed = json.loads(blob)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("facts"), list):
                return parsed["facts"]
            if isinstance(parsed, list):
                return parsed
    return []


def _slice_json(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return None


async def _call_model(
    client: Any,
    model: str,
    kind: str,
    records: list[dict[str, Any]],
    max_tokens: int,
    use_schema: bool = True,
) -> dict[str, dict[str, Any]]:
    """One model call; returns {id: fact_object}. Raises if nothing parses.

    When ``use_schema`` is true the request is grammar-constrained to the batch
    JSON schema. Some schemas are too large for the grammar compiler (it returns
    a 400 "Schema is too complex" / "Grammar compilation timed out"); callers
    fall back to ``use_schema=False``, which asks for the same JSON in prose and
    relies on :func:`_parse_facts` to extract it.
    """
    user = (
        f"Distill the following {len(records)} League of Legends {_KIND_LABEL[kind]} "
        "into fact objects. Return one object per input, echoing its exact `id`. "
        "Base every value on the data provided.\n\n```json\n"
        + json.dumps(records, ensure_ascii=False)
        + "\n```"
    )
    kwargs: dict[str, Any] = dict(
        model=model,
        max_tokens=max_tokens,
        system=_system_blocks(kind),
        messages=[{"role": "user", "content": user}],
    )
    if use_schema:
        kwargs["output_config"] = {
            "format": {"type": "json_schema", "schema": _BATCH_SCHEMA[kind]}
        }
    else:
        user += (
            '\n\nReturn ONLY a JSON object of the form {"facts": [ ... ]} containing '
            "one fact object per input item, and nothing else — no prose, no markdown "
            "code fences."
        )
        kwargs["messages"] = [{"role": "user", "content": user}]
    response = await client.messages.create(**kwargs)
    out: dict[str, dict[str, Any]] = {}
    for fact in _parse_facts(response):
        fid = str(fact.get("id", ""))
        if fid:
            out[fid] = fact
    if not out:
        raise ValueError(f"no facts parsed for {kind} batch of {len(records)}")
    return out


def _short_err(exc: BaseException) -> str:
    msg = str(exc).strip().replace("\n", " ")
    return msg[:200] if msg else exc.__class__.__name__


async def _call_safe(
    client: Any,
    model: str,
    kind: str,
    records: list[dict[str, Any]],
    max_tokens: int,
    use_schema: bool,
) -> tuple[dict[str, dict[str, Any]] | None, BaseException | None]:
    """Run :func:`_call_model`, returning ``(facts, None)`` or ``(None, exc)``."""
    try:
        return await _call_model(client, model, kind, records, max_tokens, use_schema), None
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a value
        return None, exc


async def _process_batch(
    db_path: Path,
    version: str,
    kind: str,
    client: Any,
    model: str,
    prepared: list[dict[str, Any]],
    sem: asyncio.Semaphore,
) -> int:
    table = _KIND_TO_TABLE[kind]
    max_tokens = _MAX_TOKENS[kind]
    records = [p["input"] for p in prepared]
    async with sem:
        use_schema = kind not in _SCHEMA_DISABLED
        facts_by_id, err = await _call_safe(client, model, kind, records, max_tokens, use_schema)
        # A grammar-constrained call that errors is almost always the schema
        # being rejected; drop the schema for this kind and retry as plain JSON.
        if err is not None and use_schema:
            _SCHEMA_DISABLED.add(kind)
            log.warning(
                "Distiller: %s structured-output schema rejected (%s); "
                "falling back to prompt-only JSON for remaining %s batches",
                kind, _short_err(err), kind,
            )
            use_schema = False
            facts_by_id, err = await _call_safe(
                client, model, kind, records, max_tokens, use_schema
            )
        if err is not None:
            log.warning(
                "Distiller: %s batch of %d failed (%s), retrying per-record",
                kind, len(records), _short_err(err),
            )
            facts_by_id = {}
            for p in prepared:
                sub, sub_err = await _call_safe(
                    client, model, kind, [p["input"]], max_tokens, use_schema
                )
                if sub_err is None and sub:
                    facts_by_id.update(sub)
                else:
                    log.error(
                        "Distiller: gave up on %s %s (%s)",
                        kind, p["id"], _short_err(sub_err) if sub_err else "no facts parsed",
                    )
    facts_by_id = facts_by_id or {}

    rows: list[dict[str, Any]] = []
    for p in prepared:
        fact = facts_by_id.get(str(p["id"]))
        if fact is None:
            continue
        fact["id"] = p["id"]
        fact["name"] = p["name"]
        if p.get("gold") is not None:
            fact["gold"] = p["gold"]
        row: dict[str, Any] = {
            "id": p["id"],
            "name": p["name"],
            "schema_version": DISTILL_SCHEMA_VERSION,
            "facts": fact,
        }
        if "finished" in p:
            row["finished"] = p["finished"]
        rows.append(row)
    return db.upsert_facts(db_path, table, version, rows)


def _collect_pending(db_path: Path, version: str, kind: str) -> list[dict[str, Any]]:
    """Relevance-filtered records that still need distilling for ``kind``.

    Irrelevant source rows (consumables, tree headers, ARAM-only spells, …) are
    dropped here, so a fully-distilled catalog reports zero pending even though
    those rows never get a facts row. This is what keeps re-runs free.
    """
    pending = db.pending_facts(db_path, _KIND_TO_TABLE[kind], version, DISTILL_SCHEMA_VERSION)
    prepare = _PREPARE[kind]
    return [p for rec in pending if (p := prepare(rec)) is not None]


async def _distill_kind(
    db_path: Path,
    version: str,
    kind: str,
    client: Any,
    model: str,
    batch_size: int,
    sem: asyncio.Semaphore,
    prepared: list[dict[str, Any]],
) -> int:
    if not prepared:
        return 0
    batches = [prepared[i : i + batch_size] for i in range(0, len(prepared), batch_size)]
    log.info(
        "Distiller: %s — %d to distill in %d batches", kind, len(prepared), len(batches)
    )
    tasks = [
        _process_batch(db_path, version, kind, client, model, batch, sem)
        for batch in batches
    ]
    written = await asyncio.gather(*tasks)
    return sum(written)


async def distill_patch(db_path: Path, version: str, get_config) -> dict[str, Any]:
    """Eagerly distill every missing fact for `version`. Idempotent + resumable."""
    cfg = get_config()
    if not cfg.get("distiller_enabled", True):
        return {"version": version, "skipped": "disabled"}
    key = cfg.get("anthropic_api_key") or ""
    if not key:
        log.info("Distiller: no Anthropic API key, skipping patch %s", version)
        return {"version": version, "skipped": "no_api_key"}

    import anthropic  # lazy: keeps pure helpers importable without the SDK

    model = cfg.get("distiller_model") or "claude-haiku-4-5"
    batch_size = max(1, int(cfg.get("distill_batch_size", 15)))
    concurrency = max(1, int(cfg.get("distill_max_concurrency", 4)))

    async with _distill_lock:
        _SCHEMA_DISABLED.clear()
        # Compute the relevance-filtered work once, under the lock. When nothing
        # is pending we return immediately WITHOUT opening an API client — a
        # completed patch costs zero tokens to "re-distill", so the eager
        # startup / patch-watcher / manual triggers are all safe to fire freely.
        pending_by_kind = {
            kind: _collect_pending(db_path, version, kind)
            for kind in ("item", "champion", "rune", "spell")
        }
        total_pending = sum(len(v) for v in pending_by_kind.values())
        if total_pending == 0:
            log.info(
                "Distiller: patch %s already fully distilled; nothing to do "
                "(no API calls)", version,
            )
            return {
                "version": version,
                "model": model,
                "skipped": "up_to_date",
                "written": {k: 0 for k in pending_by_kind},
                "facts_counts": db.facts_counts(db_path, version),
            }

        sem = asyncio.Semaphore(concurrency)
        written: dict[str, int] = {}
        async with anthropic.AsyncAnthropic(api_key=key) as client:
            for kind, prepared in pending_by_kind.items():
                written[kind] = await _distill_kind(
                    db_path, version, kind, client, model, batch_size, sem, prepared
                )
        total = sum(written.values())
        if total:
            log.info("Distiller: patch %s wrote %d facts %s", version, total, written)
        return {
            "version": version,
            "model": model,
            "written": written,
            "facts_counts": db.facts_counts(db_path, version),
        }


def distill_status(db_path: Path, version: str | None, get_config) -> dict[str, Any]:
    """Per-kind distillation progress for the UI (counts + % complete).

    ``eligible`` is the number of source records the distiller actually targets
    (after relevance filtering, e.g. only purchasable items). ``done`` counts
    rows distilled at the current schema version; ``pending`` is the eligible
    remainder. Cheap to call: it only loads source JSON for not-yet-distilled
    rows.
    """
    cfg = get_config()
    status: dict[str, Any] = {
        "version": version,
        "enabled": bool(cfg.get("distiller_enabled", True)),
        "model": cfg.get("distiller_model") or "claude-haiku-4-5",
        "has_api_key": bool(cfg.get("anthropic_api_key")),
        "running": _distill_lock.locked(),
        "schema_version": DISTILL_SCHEMA_VERSION,
        "kinds": {},
        "done": 0,
        "eligible": 0,
        "pct": 0.0,
    }
    if not version:
        return status

    done_counts = db.facts_counts(db_path, version, DISTILL_SCHEMA_VERSION)
    total_done = 0
    total_eligible = 0
    for kind, table in _KIND_TO_TABLE.items():
        done = done_counts.get(table, 0)
        pending = len(_collect_pending(db_path, version, kind))
        eligible = done + pending
        pct = round(100.0 * done / eligible, 1) if eligible else 100.0
        status["kinds"][kind] = {
            "label": _KIND_LABEL[kind],
            "done": done,
            "pending": pending,
            "eligible": eligible,
            "pct": pct,
        }
        total_done += done
        total_eligible += eligible

    status["done"] = total_done
    status["eligible"] = total_eligible
    status["pct"] = round(100.0 * total_done / total_eligible, 1) if total_eligible else 100.0
    status["complete"] = total_eligible > 0 and total_done >= total_eligible
    return status


def _main(argv: list[str] | None = None) -> int:
    from . import config

    parser = argparse.ArgumentParser(description="Distill Data Dragon into advisor facts.")
    parser.add_argument("--version", default=None, help="Patch to distill (default: latest cached).")
    parser.add_argument("--db", default=None, help="SQLite path (default: config DB_PATH).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    db_path = Path(args.db) if args.db else config.DB_PATH
    db.init(db_path)
    version = args.version
    if not version:
        version = db.patch_summary(db_path).get("version")
    if not version:
        print("No patch data cached; run a patch refresh first.")
        return 1

    result = asyncio.run(distill_patch(db_path, version, config.load))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
