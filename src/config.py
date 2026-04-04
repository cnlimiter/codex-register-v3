"""
config.py — Runtime configuration manager (replaces GM_getValue / GM_setValue).
All settings are persisted in config.yaml at the project root.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

_DEFAULTS: dict[str, Any] = {
    "engine": "playwright",
    "headless": True,          # True = invisible batch mode; False = visible headed window
    "slow_mo": 0,              # extra ms between actions; 0 = auto (80 ms when headed)
    "max_concurrent": 2,
    "mail_provider": "gptmail",
    "mail": {
        "gptmail":  {"api_key": "", "base_url": "https://mail.chatgpt.org.uk"},
        "npcmail":  {"api_key": "", "base_url": "https://dash.xphdfs.me"},
        "yydsmail": {"api_key": "", "base_url": "https://maliapi.215.im/v1"},
    },
    "registration": {"prefix": "", "domain": ""},
    "proxy_strategy": "pool",
    "proxy_static": "",
    "team": {"url": "", "key": ""},
    "sync":  {"url": "", "key": ""},
    # Set to false to skip the post-registration Codex OAuth token step.
    "enable_oauth": True,
}


def load() -> dict[str, Any]:
    """Load config from config.yaml, merging with defaults."""
    if not CONFIG_PATH.exists():
        return dict(_DEFAULTS)
    with CONFIG_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return _deep_merge(_DEFAULTS, data)


def get(key: str, default: Any = None) -> Any:
    """Dot-notation getter.  e.g. get('mail.gptmail.api_key')"""
    cfg = load()
    parts = key.split(".")
    val: Any = cfg
    for part in parts:
        if isinstance(val, dict) and part in val:
            val = val[part]
        else:
            return default
    return val


def set_key(key: str, value: Any) -> None:
    """Dot-notation setter.  e.g. set_key('engine', 'camoufox')
    Automatically coerces integers, floats, and booleans from string input.
    """
    if isinstance(value, str):
        if value.lower() in ("true", "false"):
            value = value.lower() == "true"
        else:
            try:
                value = int(value)
            except ValueError:
                try:
                    value = float(value)
                except ValueError:
                    pass  # keep as string
    cfg = load()
    parts = key.split(".")
    d = cfg
    for part in parts[:-1]:
        d = d.setdefault(part, {})
    d[parts[-1]] = value
    _save(cfg)


def _save(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result

