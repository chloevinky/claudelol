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
