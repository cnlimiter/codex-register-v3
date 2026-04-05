"""
config.py — SQLite-backed compatibility shim.

New runtime code should use ``src.settings_db`` directly.
This module remains only for legacy synchronous call-sites that still expect
``load()``, ``get()`` and ``set_key()`` helpers.
"""
from __future__ import annotations

import asyncio
from typing import Any

import src.settings_db as settings_db

_GENERAL_KEYS = {
    "engine", "headless", "slow_mo", "mobile",
    "max_concurrent", "mail_provider", "proxy_strategy", "proxy_static",
}

_SECTION_PREFIXES = [
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
    "general",
]


def _run_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError(
        "src.config is a synchronous compatibility shim. "
        "Inside async code, use src.settings_db directly."
    )


def _coerce_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def _resolve_key(key: str) -> tuple[str, list[str]]:
    if key == "enable_oauth":
        return "oauth", ["enabled"]
    if key in _GENERAL_KEYS:
        return "general", [key]
    for prefix in sorted(_SECTION_PREFIXES, key=len, reverse=True):
        if key == prefix:
            return prefix, []
        if key.startswith(prefix + "."):
            return prefix, key[len(prefix) + 1 :].split(".")
    raise KeyError(f"Unsupported config key: {key}")


def _nested_get(data: Any, parts: list[str], default: Any = None) -> Any:
    cur = data
    for part in parts:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def _nested_set(data: dict[str, Any], parts: list[str], value: Any) -> dict[str, Any]:
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value
    return data


def load() -> dict[str, Any]:
    """Return the merged runtime config from SQLite."""
    return _run_sync(settings_db.build_config())


def get(key: str, default: Any = None) -> Any:
    """Dot-notation getter against the SQLite-backed merged config."""
    return _nested_get(load(), key.split("."), default)


def set_key(key: str, value: Any) -> None:
    """Dot-notation setter against SQLite settings sections."""
    section, parts = _resolve_key(key)
    coerced = _coerce_value(value)
    if parts:
        current = _run_sync(settings_db.get_section(section))
        if not isinstance(current, dict):
            raise TypeError(f"Section {section!r} is not a dict; set the whole section instead.")
        updated = _nested_set(dict(current), parts, coerced)
    else:
        updated = coerced
    _run_sync(settings_db.set_section(section, updated))
