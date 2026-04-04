"""
db.py — SQLite schema initialisation via aiosqlite.
Run directly:  python -m src.db
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
from loguru import logger

DB_PATH = Path(__file__).parent.parent / "accounts.db"

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS accounts (
        email         TEXT PRIMARY KEY,
        password      TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'created',
        first_name    TEXT NOT NULL DEFAULT '',
        last_name     TEXT NOT NULL DEFAULT '',
        provider      TEXT NOT NULL DEFAULT '',
        proxy         TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL DEFAULT '',
        raw_json      TEXT NOT NULL DEFAULT '{}',
        access_token  TEXT NOT NULL DEFAULT '',
        refresh_token TEXT NOT NULL DEFAULT '',
        account_id    TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proxies (
        address    TEXT PRIMARY KEY,
        fail_count INTEGER NOT NULL DEFAULT 0,
        last_used  REAL    NOT NULL DEFAULT 0,
        is_active  INTEGER NOT NULL DEFAULT 1
    )
    """,
]

# Idempotent migrations for pre-existing DBs that lack the token columns.
_MIGRATIONS = [
    "ALTER TABLE accounts ADD COLUMN access_token  TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE accounts ADD COLUMN refresh_token TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE accounts ADD COLUMN account_id    TEXT NOT NULL DEFAULT ''",
]


async def init() -> None:
    """Create tables if they do not exist and run schema migrations."""
    async with aiosqlite.connect(DB_PATH) as db:
        for ddl in _DDL:
            await db.execute(ddl)
        # Apply migrations idempotently — SQLite raises OperationalError for
        # duplicate columns, which we silently ignore.
        for migration in _MIGRATIONS:
            try:
                await db.execute(migration)
            except Exception:
                pass
        await db.commit()
    logger.info(f"DB initialized at {DB_PATH}")


if __name__ == "__main__":
    asyncio.run(init())
    print(f"DB initialized at {DB_PATH}")

