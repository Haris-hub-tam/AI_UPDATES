"""SQLite storage: schema, automatic column upgrades and small helpers."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import settings

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# table -> ordered {column: type}. New columns added here are created
# automatically on existing databases (see migrate()).
SCHEMA: dict[str, dict[str, str]] = {
    "articles": {
        "id": "TEXT PRIMARY KEY",
        "url": "TEXT NOT NULL UNIQUE",
        "title": "TEXT NOT NULL DEFAULT ''",
        "summary": "TEXT NOT NULL DEFAULT ''",
        "source": "TEXT NOT NULL DEFAULT ''",
        "category": "TEXT NOT NULL DEFAULT ''",
        "published": "TEXT",
        "fetched": "TEXT",
        "image": "TEXT",
        "content": "TEXT",
        "word_count": "INTEGER NOT NULL DEFAULT 0",
        "fulltext_status": "TEXT",  # NULL=pending, feed, ok, failed, blocked, skipped
        "ai_tldr": "TEXT",
        "ai_points": "TEXT",  # JSON list
        "ai_why": "TEXT",
        "ai_status": "TEXT",  # NULL=pending, ok, failed
        "ai_model": "TEXT",
        "ai_error": "TEXT",
        "ai_attempts": "INTEGER NOT NULL DEFAULT 0",
        # French version of the AI summary (written in the same call)
        "ai_tldr_fr": "TEXT",
        "ai_points_fr": "TEXT",  # JSON list
        "ai_why_fr": "TEXT",
        # translations (translate.py): language of the source, titles and teasers
        "src_lang": "TEXT",  # en, fr, ... as detected
        "title_en": "TEXT",
        "title_fr": "TEXT",
        "teaser_en": "TEXT",
        "teaser_fr": "TEXT",
        "tr_status": "TEXT",  # NULL=pending, ok, failed
        "tr_attempts": "INTEGER NOT NULL DEFAULT 0",
        # full text translated on request from the article page
        "content_en": "TEXT",
        "content_fr": "TEXT",
        "content_tr_model": "TEXT",
    },
    "sources": {
        "name": "TEXT PRIMARY KEY",
        "url": "TEXT",
        "category": "TEXT",
        "last_success": "TEXT",
        "last_error": "TEXT",
        "last_error_at": "TEXT",
        "last_count": "INTEGER NOT NULL DEFAULT 0",
    },
    "models": {
        "id": "TEXT PRIMARY KEY",
        "name": "TEXT NOT NULL DEFAULT ''",
        "provider": "TEXT NOT NULL DEFAULT ''",
        "context_length": "INTEGER",
        "price_in": "REAL",
        "price_out": "REAL",
        "price_cached": "REAL",
        "max_output": "INTEGER",
        "input_modalities": "TEXT",
        "output_modalities": "TEXT",
        "tokenizer": "TEXT",
        "supported_params": "TEXT",
        "description": "TEXT",
        "created": "TEXT",
        "first_seen": "TEXT",
        "updated": "TEXT",
        "match_key": "TEXT",
    },
    "price_history": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "model_id": "TEXT NOT NULL",
        "price_in": "REAL",
        "price_out": "REAL",
        "recorded_at": "TEXT NOT NULL",
    },
    "benchmarks": {
        "id": "TEXT PRIMARY KEY",
        "slug": "TEXT",
        "name": "TEXT NOT NULL DEFAULT ''",
        "creator": "TEXT",
        "release_date": "TEXT",
        "intelligence": "REAL",
        "coding": "REAL",
        "agentic": "REAL",
        "evaluations": "TEXT",  # JSON {key: value}
        "price_in": "REAL",
        "price_out": "REAL",
        "speed": "REAL",
        "ttft": "REAL",
        "raw": "TEXT",
        "match_key": "TEXT",
        "updated": "TEXT",
    },
    "meta": {
        "key": "TEXT PRIMARY KEY",
        "value": "TEXT",
    },
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published)",
    "CREATE INDEX IF NOT EXISTS idx_articles_category ON articles(category)",
    "CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source)",
    "CREATE INDEX IF NOT EXISTS idx_price_history_model ON price_history(model_id, recorded_at)",
    "CREATE INDEX IF NOT EXISTS idx_models_match ON models(match_key)",
    "CREATE INDEX IF NOT EXISTS idx_bench_match ON benchmarks(match_key)",
]


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime(TS_FMT)


def to_ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(TS_FMT)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or settings.db_path()
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Create missing tables and add missing columns (safe to run every start)."""
    for table, cols in SCHEMA.items():
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            + ", ".join(f"{c} {t}" for c, t in cols.items())
            + ")"
        )
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in existing:
                # ALTER TABLE cannot add PRIMARY KEY/UNIQUE columns; strip those.
                typ_alter = re.sub(r"PRIMARY KEY( AUTOINCREMENT)?|UNIQUE", "", typ).strip()
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ_alter}")
    for stmt in INDEXES:
        conn.execute(stmt)
    conn.commit()


def init_db() -> None:
    conn = connect()
    try:
        migrate(conn)
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_meta_json(conn: sqlite3.Connection, key: str, default=None):
    raw = get_meta(conn, key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def set_meta_json(conn: sqlite3.Connection, key: str, value) -> None:
    set_meta(conn, key, json.dumps(value))
