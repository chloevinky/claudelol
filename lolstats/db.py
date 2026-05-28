"""SQLite-backed cache for per-patch Data Dragon data."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

_lock = threading.Lock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS patch_meta (
    version TEXT PRIMARY KEY,
    fetched_at INTEGER NOT NULL,
    locale TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS champions (
    version TEXT NOT NULL,
    key TEXT NOT NULL,           -- numeric id from Riot, as string
    id TEXT NOT NULL,            -- e.g. "Aatrox"
    name TEXT NOT NULL,
    title TEXT,
    tags TEXT,                   -- JSON array
    blurb TEXT,
    data TEXT NOT NULL,          -- full JSON
    PRIMARY KEY (version, id)
);
CREATE INDEX IF NOT EXISTS idx_champions_key ON champions(version, key);
CREATE INDEX IF NOT EXISTS idx_champions_name ON champions(version, name);

CREATE TABLE IF NOT EXISTS items (
    version TEXT NOT NULL,
    id TEXT NOT NULL,            -- numeric id, as string
    name TEXT NOT NULL,
    description TEXT,
    plaintext TEXT,
    tags TEXT,                   -- JSON array
    gold_total INTEGER,
    data TEXT NOT NULL,          -- full JSON
    PRIMARY KEY (version, id)
);
CREATE INDEX IF NOT EXISTS idx_items_name ON items(version, name);

CREATE TABLE IF NOT EXISTS runes (
    version TEXT NOT NULL,
    id TEXT NOT NULL,            -- rune id as string
    key TEXT,                    -- short key, e.g. "Domination"
    name TEXT NOT NULL,
    tree TEXT,                   -- parent tree name
    slot INTEGER,                -- 0-3 for runes, NULL for trees
    long_desc TEXT,
    short_desc TEXT,
    data TEXT NOT NULL,
    PRIMARY KEY (version, id)
);

CREATE TABLE IF NOT EXISTS summoner_spells (
    version TEXT NOT NULL,
    id TEXT NOT NULL,            -- e.g. "SummonerFlash"
    key TEXT NOT NULL,           -- numeric, as string
    name TEXT NOT NULL,
    description TEXT,
    data TEXT NOT NULL,
    PRIMARY KEY (version, id)
);
CREATE INDEX IF NOT EXISTS idx_spells_key ON summoner_spells(version, key);

-- Distilled "facts": compact, numbers-first objects produced by the cheap
-- model from the raw Data Dragon JSON above. One row per source record.
CREATE TABLE IF NOT EXISTS item_facts (
    version TEXT NOT NULL,
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    finished INTEGER,            -- 1 if a finished/buyable item, else 0
    schema_version INTEGER NOT NULL,
    facts TEXT NOT NULL,         -- distilled JSON
    PRIMARY KEY (version, id)
);

CREATE TABLE IF NOT EXISTS champion_facts (
    version TEXT NOT NULL,
    id TEXT NOT NULL,            -- e.g. "Aatrox"
    name TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    facts TEXT NOT NULL,
    PRIMARY KEY (version, id)
);
CREATE INDEX IF NOT EXISTS idx_champion_facts_name ON champion_facts(version, name);

CREATE TABLE IF NOT EXISTS rune_facts (
    version TEXT NOT NULL,
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    facts TEXT NOT NULL,
    PRIMARY KEY (version, id)
);

CREATE TABLE IF NOT EXISTS spell_facts (
    version TEXT NOT NULL,
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    facts TEXT NOT NULL,
    PRIMARY KEY (version, id)
);
"""


def init(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def connect(db_path: Path):
    with _lock:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def have_version(db_path: Path, version: str) -> bool:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT version FROM patch_meta WHERE version=?", (version,)
        ).fetchone()
    return row is not None


def upsert_meta(db_path: Path, version: str, locale: str, fetched_at: int) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO patch_meta(version, fetched_at, locale) VALUES(?,?,?)",
            (version, fetched_at, locale),
        )
        conn.commit()


def insert_champions(db_path: Path, version: str, rows: Iterable[dict[str, Any]]) -> None:
    with connect(db_path) as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO champions
               (version, key, id, name, title, tags, blurb, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    version,
                    str(c.get("key", "")),
                    c["id"],
                    c.get("name", c["id"]),
                    c.get("title"),
                    json.dumps(c.get("tags", [])),
                    c.get("blurb"),
                    json.dumps(c),
                )
                for c in rows
            ],
        )
        conn.commit()


def insert_items(db_path: Path, version: str, items: dict[str, dict[str, Any]]) -> None:
    with connect(db_path) as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO items
               (version, id, name, description, plaintext, tags, gold_total, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    version,
                    item_id,
                    item.get("name", ""),
                    item.get("description"),
                    item.get("plaintext"),
                    json.dumps(item.get("tags", [])),
                    (item.get("gold") or {}).get("total"),
                    json.dumps(item),
                )
                for item_id, item in items.items()
            ],
        )
        conn.commit()


def insert_runes(db_path: Path, version: str, trees: list[dict[str, Any]]) -> None:
    """Flatten the runesReforged tree structure into a single table."""
    rows: list[tuple] = []
    for tree in trees:
        tree_id = str(tree.get("id"))
        rows.append(
            (
                version,
                tree_id,
                tree.get("key"),
                tree.get("name", ""),
                tree.get("name"),
                None,
                tree.get("name"),
                tree.get("name"),
                json.dumps(tree),
            )
        )
        for slot_idx, slot in enumerate(tree.get("slots", [])):
            for rune in slot.get("runes", []):
                rows.append(
                    (
                        version,
                        str(rune.get("id")),
                        rune.get("key"),
                        rune.get("name", ""),
                        tree.get("name"),
                        slot_idx,
                        rune.get("longDesc"),
                        rune.get("shortDesc"),
                        json.dumps(rune),
                    )
                )
    with connect(db_path) as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO runes
               (version, id, key, name, tree, slot, long_desc, short_desc, data)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()


def insert_summoner_spells(
    db_path: Path, version: str, spells: dict[str, dict[str, Any]]
) -> None:
    with connect(db_path) as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO summoner_spells
               (version, id, key, name, description, data)
               VALUES (?,?,?,?,?,?)""",
            [
                (
                    version,
                    s_id,
                    str(s.get("key", "")),
                    s.get("name", ""),
                    s.get("description"),
                    json.dumps(s),
                )
                for s_id, s in spells.items()
            ],
        )
        conn.commit()


def champion_by_name(db_path: Path, version: str, name: str) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT data FROM champions WHERE version=? AND (name=? OR id=?) LIMIT 1",
            (version, name, name),
        ).fetchone()
    return json.loads(row["data"]) if row else None


def champion_summary(db_path: Path, version: str, name: str) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, name, title, tags, blurb FROM champions WHERE version=? AND (name=? OR id=?) LIMIT 1",
            (version, name, name),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "title": row["title"],
        "tags": json.loads(row["tags"] or "[]"),
        "blurb": row["blurb"],
    }


def all_champion_names(db_path: Path, version: str) -> list[str]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM champions WHERE version=? ORDER BY name", (version,)
        ).fetchall()
    return [r["name"] for r in rows]


def patch_summary(db_path: Path) -> dict[str, Any]:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT version, fetched_at, locale FROM patch_meta ORDER BY fetched_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {"version": None}
        counts = {}
        for table in ("champions", "items", "runes", "summoner_spells"):
            c = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE version=?", (row["version"],)
            ).fetchone()
            counts[table] = c["n"]
    return {
        "version": row["version"],
        "fetched_at": row["fetched_at"],
        "locale": row["locale"],
        "counts": counts,
    }


# --- Distilled facts -------------------------------------------------------
#
# Each facts table is populated by the cheap-model distiller. The mapping below
# is the single source of truth for which raw table feeds which facts table; it
# also gates the table name used in f-string SQL (so interpolation is safe).

FACTS_SOURCES: dict[str, str] = {
    "item_facts": "items",
    "champion_facts": "champions",
    "rune_facts": "runes",
    "spell_facts": "summoner_spells",
}


def upsert_facts(db_path: Path, table: str, version: str, rows: Iterable[dict[str, Any]]) -> int:
    """Batch-write distilled fact rows. Returns the number written.

    Each row is a dict with keys ``id``, ``name``, ``schema_version`` and
    ``facts`` (a dict or pre-serialized JSON string). ``item_facts`` rows may
    also carry a ``finished`` flag.
    """
    if table not in FACTS_SOURCES:
        raise ValueError(f"unknown facts table {table!r}")
    has_finished = table == "item_facts"
    payload: list[tuple] = []
    for r in rows:
        facts = r["facts"]
        if not isinstance(facts, str):
            facts = json.dumps(facts)
        if has_finished:
            payload.append(
                (
                    version,
                    str(r["id"]),
                    r.get("name", ""),
                    1 if r.get("finished") else 0,
                    int(r["schema_version"]),
                    facts,
                )
            )
        else:
            payload.append(
                (version, str(r["id"]), r.get("name", ""), int(r["schema_version"]), facts)
            )
    if not payload:
        return 0
    with connect(db_path) as conn:
        if has_finished:
            conn.executemany(
                f"""INSERT OR REPLACE INTO {table}
                   (version, id, name, finished, schema_version, facts)
                   VALUES (?,?,?,?,?,?)""",
                payload,
            )
        else:
            conn.executemany(
                f"""INSERT OR REPLACE INTO {table}
                   (version, id, name, schema_version, facts)
                   VALUES (?,?,?,?,?)""",
                payload,
            )
        conn.commit()
    return len(payload)


def pending_facts(
    db_path: Path, table: str, version: str, schema_version: int
) -> list[dict[str, Any]]:
    """Source records that have no up-to-date facts row (drives resumable distill).

    A record is pending when it has no facts row for this version, or its stored
    ``schema_version`` differs from the current one. Returns ``{id, name, data}``
    with ``data`` parsed from the raw Data Dragon JSON.
    """
    if table not in FACTS_SOURCES:
        raise ValueError(f"unknown facts table {table!r}")
    source = FACTS_SOURCES[table]
    with connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT s.id AS id, s.name AS name, s.data AS data
            FROM {source} s
            LEFT JOIN {table} f
              ON f.version = s.version AND f.id = s.id AND f.schema_version = ?
            WHERE s.version = ? AND f.id IS NULL
            """,
            (schema_version, version),
        ).fetchall()
    return [{"id": r["id"], "name": r["name"], "data": json.loads(r["data"])} for r in rows]


def facts_counts(db_path: Path, version: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    with connect(db_path) as conn:
        for table in FACTS_SOURCES:
            c = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE version=?", (version,)
            ).fetchone()
            counts[table] = c["n"]
    return counts


def item_facts_catalog(
    db_path: Path, version: str, finished_only: bool = False
) -> list[dict[str, Any]]:
    """Compact list of distilled item facts for injection into the advisor."""
    sql = "SELECT id, name, finished, facts FROM item_facts WHERE version=?"
    params: list[Any] = [version]
    if finished_only:
        sql += " AND finished=1"
    sql += " ORDER BY name"
    with connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        facts = json.loads(r["facts"])
        facts.setdefault("id", r["id"])
        facts.setdefault("name", r["name"])
        facts["finished"] = bool(r["finished"])
        out.append(facts)
    return out


def _facts_by_name(
    db_path: Path, table: str, version: str, name: str
) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            f"SELECT facts FROM {table} WHERE version=? AND (name=? OR id=?) LIMIT 1",
            (version, name, name),
        ).fetchone()
    return json.loads(row["facts"]) if row else None


def champion_facts(db_path: Path, version: str, name: str) -> dict[str, Any] | None:
    return _facts_by_name(db_path, "champion_facts", version, name)


def rune_facts(db_path: Path, version: str, name: str) -> dict[str, Any] | None:
    return _facts_by_name(db_path, "rune_facts", version, name)


def spell_facts(db_path: Path, version: str, name: str) -> dict[str, Any] | None:
    return _facts_by_name(db_path, "spell_facts", version, name)
