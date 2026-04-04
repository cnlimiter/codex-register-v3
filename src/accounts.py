"""
accounts.py — CRUD operations for the accounts table.
Import format is compatible with the original JS tool's JSON export.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
from loguru import logger

from src.db import DB_PATH


# ──────────────────────────────────────────────
# Core helpers
# ──────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: aiosqlite.Row) -> dict:
    d = dict(row)
    try:
        raw = json.loads(d.get("raw_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        raw = {}
    d["_raw"] = raw
    return d


# ──────────────────────────────────────────────
# Write operations
# ──────────────────────────────────────────────

async def upsert(account: dict) -> None:
    """Insert or update an account record (keyed by email)."""
    email = account.get("email", "").strip()
    if not email:
        logger.warning("[accounts] upsert called with empty email — skipped")
        return

    row = {
        "email":        email,
        "password":     account.get("password", ""),
        "status":       account.get("status", "created"),
        "first_name":   account.get("firstName", account.get("first_name", "")),
        "last_name":    account.get("lastName",  account.get("last_name",  "")),
        "provider":     account.get("provider", ""),
        "proxy":        account.get("proxy", ""),
        "created_at":   account.get("createdAt", account.get("created_at", _now_iso())),
        "raw_json":     json.dumps(account, ensure_ascii=False),
        "access_token":  account.get("access_token", ""),
        "refresh_token": account.get("refresh_token", ""),
        "account_id":    account.get("account_id", ""),
    }

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO accounts
                (email, password, status, first_name, last_name, provider, proxy,
                 created_at, raw_json, access_token, refresh_token, account_id)
            VALUES
                (:email, :password, :status, :first_name, :last_name, :provider, :proxy,
                 :created_at, :raw_json, :access_token, :refresh_token, :account_id)
            ON CONFLICT(email) DO UPDATE SET
                password      = excluded.password,
                status        = excluded.status,
                first_name    = excluded.first_name,
                last_name     = excluded.last_name,
                provider      = excluded.provider,
                proxy         = excluded.proxy,
                created_at    = excluded.created_at,
                raw_json      = excluded.raw_json,
                access_token  = excluded.access_token,
                refresh_token = excluded.refresh_token,
                account_id    = excluded.account_id
            """,
            row,
        )
        await db.commit()
    logger.debug(f"[accounts] upserted {email}")


async def delete(email: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM accounts WHERE email = ?", (email,))
        await db.commit()
    logger.info(f"[accounts] deleted {email}")


# ──────────────────────────────────────────────
# Read operations
# ──────────────────────────────────────────────

async def list_all(status_filter: Optional[str] = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status_filter:
            cur = await db.execute(
                "SELECT * FROM accounts WHERE status LIKE ? ORDER BY created_at DESC",
                (f"%{status_filter}%",),
            )
        else:
            cur = await db.execute("SELECT * FROM accounts ORDER BY created_at DESC")
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_emails() -> set[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT email FROM accounts")
        rows = await cur.fetchall()
    return {r[0] for r in rows}


# ──────────────────────────────────────────────
# Import / Export
# ──────────────────────────────────────────────

async def export_json(path: Path) -> int:
    rows = await list_all()
    js_list = []
    for r in rows:
        raw = r.get("_raw") or {}
        if not raw:
            raw = {
                "email":     r["email"],
                "password":  r["password"],
                "status":    r["status"],
                "firstName": r["first_name"],
                "lastName":  r["last_name"],
                "createdAt": r["created_at"],
            }
        js_list.append(raw)
    path.write_text(json.dumps(js_list, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(js_list)


async def export_csv(path: Path) -> int:
    rows = await list_all()
    fieldnames = [
        "email", "password", "status", "first_name", "last_name",
        "provider", "proxy", "created_at", "account_id",
        "access_token", "refresh_token",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fieldnames})
    return len(rows)


async def import_json(path: Path) -> tuple[int, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = [data]
    existing = await get_emails()
    added = skipped = 0
    for item in data:
        email = (item.get("email") or "").strip()
        if not email or "@" not in email:
            skipped += 1
            continue
        if email in existing:
            skipped += 1
            continue
        item.setdefault("createdAt", _now_iso())
        item.setdefault("status", "imported")
        await upsert(item)
        added += 1
    return added, skipped


async def import_text(path: Path) -> tuple[int, int]:
    """Import plain email[:password] lines or CSV lines."""
    lines = path.read_text(encoding="utf-8").splitlines()
    existing = await get_emails()
    added = skipped = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.replace(",", "|").replace(";", "|").replace("\t", "|").split("|")
        email = parts[0].strip()
        password = parts[1].strip() if len(parts) > 1 else ""
        if not email or "@" not in email:
            skipped += 1
            continue
        if email in existing:
            skipped += 1
            continue
        await upsert({"email": email, "password": password, "status": "imported"})
        added += 1
    return added, skipped

