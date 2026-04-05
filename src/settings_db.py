"""
settings_db.py — All runtime and WebUI configuration stored in SQLite.
"""
from __future__ import annotations

import copy
import json
from typing import Any

import aiosqlite

from src.db import DB_PATH

# ── Sections managed in DB ────────────────────────────────────────────────

_SECTIONS = [
    "general",       # engine, headless, mobile, concurrency, proxy, mail_provider
    "mail.gptmail",
    "mail.npcmail",
    "mail.yydsmail",
    "mail.imap",
    "mail.outlook",
    "registration",
    "team",
    "sync",
    "oauth",
    "mouse",
    "timeouts",
    "timing",
]

_DEFAULTS: dict[str, Any] = {
    "general": {
        "engine":         "playwright",
        "headless":       True,
        "slow_mo":        0,
        "mobile":         False,
        "max_concurrent": 2,
        "mail_provider":  "gptmail",
        "proxy_strategy": "none",
        "proxy_static":   "",
    },
    "mail.gptmail":  {"api_key": "", "base_url": "https://mail.chatgpt.org.uk"},
    "mail.npcmail":  {"api_key": "", "base_url": "https://dash.xphdfs.me"},
    "mail.yydsmail": {"api_key": "", "base_url": "https://maliapi.215.im/v1"},
    "mail.imap":     [],
    "mail.outlook":  [],
    "registration": {"prefix": "", "domain": ""},
    "team": {"url": "", "key": ""},
    "sync": {"url": "", "key": ""},
    "oauth": {"enabled": True, "timeout": 45},
    "mouse": {
        "human_simulation": True,
        "steps_min":        4,
        "steps_max":        8,
        "step_delay_min":   0.003,
        "step_delay_max":   0.010,
        "hover_min":        0.02,
        "hover_max":        0.08,
    },
    "timeouts": {
        "page_load":            30,
        "auth0_redirect":       8,
        "email_input":          15,
        "password_input":       60,
        "otp_input":            60,
        "otp_code":             180,
        "profile_detect":       15,
        "profile_field":        5,
        "complete_redirect":    20,
        "oauth_navigate":       20,
        "oauth_flow_element":   8,
        "oauth_login_email":    8,
        "oauth_login_password": 10,
        "oauth_token_exchange": 30,
        "oauth_total":          45,
    },
    "timing": {
        "post_nav":      1.0,
        "pre_fill":      0.5,
        "post_click":    1.5,
        "post_complete": 1.0,
    },
}


# ── Internal helpers ──────────────────────────────────────────────────────

async def _ensure_table() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                section TEXT PRIMARY KEY,
                value   TEXT NOT NULL DEFAULT '{}'
            )
        """)
        await db.commit()


# ── Public API ────────────────────────────────────────────────────────────

async def init() -> None:
    """Ensure the settings table exists."""
    await _ensure_table()


async def init_from_yaml() -> None:
    """
    Backward-compatible alias.

    All runtime configuration now lives in SQLite.
    """
    await init()


async def get_section(section: str) -> Any:
    """Return a single settings section (or default if not stored)."""
    await _ensure_table()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE section = ?", (section,)
        ) as cur:
            row = await cur.fetchone()
    return json.loads(row[0]) if row else _DEFAULTS.get(section, {})


async def set_section(section: str, value: Any) -> None:
    """Persist a settings section to DB."""
    await _ensure_table()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (section, value) VALUES (?, ?)",
            (section, json.dumps(value, ensure_ascii=False)),
        )
        await db.commit()


async def get_all() -> dict[str, Any]:
    """Return all settings sections (DB values override defaults)."""
    await _ensure_table()
    result = {k: v for k, v in _DEFAULTS.items()}
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT section, value FROM settings") as cur:
            rows = await cur.fetchall()
    for section, value_json in rows:
        result[section] = json.loads(value_json)
    return result


async def build_config() -> dict[str, Any]:
    """
    Build the complete runtime config dict entirely from DB defaults + stored
    SQLite sections.
    """
    db = await get_all()

    cfg: dict[str, Any] = copy.deepcopy(db.get("general", {}))
    cfg["mail"] = {
        "gptmail":  copy.deepcopy(db.get("mail.gptmail",  _DEFAULTS["mail.gptmail"])),
        "npcmail":  copy.deepcopy(db.get("mail.npcmail",  _DEFAULTS["mail.npcmail"])),
        "yydsmail": copy.deepcopy(db.get("mail.yydsmail", _DEFAULTS["mail.yydsmail"])),
        "imap":     copy.deepcopy(db.get("mail.imap",     _DEFAULTS["mail.imap"])),
        "outlook":  copy.deepcopy(db.get("mail.outlook",  _DEFAULTS["mail.outlook"])),
    }
    cfg["registration"] = copy.deepcopy(db.get("registration", _DEFAULTS["registration"]))
    cfg["team"]         = copy.deepcopy(db.get("team",         _DEFAULTS["team"]))
    cfg["sync"]         = copy.deepcopy(db.get("sync",         _DEFAULTS["sync"]))
    cfg["mouse"]        = copy.deepcopy(db.get("mouse",        _DEFAULTS["mouse"]))
    cfg["timeouts"]     = copy.deepcopy(db.get("timeouts",     _DEFAULTS["timeouts"]))
    cfg["timing"]       = copy.deepcopy(db.get("timing",       _DEFAULTS["timing"]))

    oauth_db = copy.deepcopy(db.get("oauth", _DEFAULTS["oauth"]))
    cfg["oauth"] = oauth_db
    cfg["enable_oauth"] = oauth_db.get("enabled", True)
    cfg["timeouts"]["oauth_total"] = oauth_db.get("timeout", cfg["timeouts"].get("oauth_total", 45))

    return cfg

