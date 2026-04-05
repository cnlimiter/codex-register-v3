"""
mail/__init__.py — Factory for all mail service clients.
"""
from __future__ import annotations

from typing import Optional

from src.mail.base import MailClient
from src.mail.gptmail import GPTMailClient
from src.mail.npcmail import NPCMailClient
from src.mail.yydsmail import YYDSMailClient
from src.mail.imap import IMAPMailClient, MultiIMAPMailClient
from src.mail.outlook import OutlookMailClient, MultiOutlookMailClient

__all__ = [
    "MailClient", "GPTMailClient", "NPCMailClient",
    "YYDSMailClient", "IMAPMailClient", "MultiIMAPMailClient",
    "OutlookMailClient", "MultiOutlookMailClient",
    "get_mail_client",
]


def get_mail_client(
    provider: str,
    api_key: str = "",
    base_url: str = "",
    cfg: Optional[dict] = None,
) -> MailClient:
    """
    Return the appropriate MailClient for *provider*.

    Parameters
    ----------
    provider : Provider name, e.g. 'gptmail', 'imap', 'imap:0', 'outlook', 'outlook:1'.
    api_key  : API key for HTTP-based providers (gptmail / npcmail / yydsmail).
    base_url : Base URL for HTTP-based providers.
    cfg      : Full merged config dict (from settings_db.build_config()).
               Required for IMAP/Outlook providers because all runtime config
               is now stored in SQLite.
    """
    def _get_cfg() -> dict:
        if cfg is not None:
            return cfg
        raise ValueError(
            "DB-backed config is required for this provider. "
            "Load it with settings_db.build_config() first."
        )

    match provider.lower():
        case "gptmail":
            return GPTMailClient(
                api_key=api_key or "gpt-test",
                **({"base_url": base_url} if base_url else {}),
            )
        case "npcmail":
            return NPCMailClient(
                api_key=api_key,
                **({"base_url": base_url} if base_url else {}),
            )
        case "yydsmail":
            return YYDSMailClient(
                api_key=api_key,
                **({"base_url": base_url} if base_url else {}),
            )

        # ── IMAP ──────────────────────────────────────────────────────────
        case _ if provider.lower() == "imap" or provider.lower().startswith("imap:"):
            _parts = provider.split(":", 1)
            _index: Optional[int] = None
            if len(_parts) == 2 and _parts[1].isdigit():
                _index = int(_parts[1])

            _imap_raw = (_get_cfg().get("mail") or {}).get("imap", [])
            if isinstance(_imap_raw, dict):
                _imap_raw = [_imap_raw]

            _clients = [
                IMAPMailClient(
                    email        = c.get("email", ""),
                    password     = c.get("password", ""),
                    host         = c.get("host", ""),
                    port         = int(c.get("port", 993)),
                    ssl          = bool(c.get("ssl", True)),
                    folder       = c.get("folder", "INBOX"),
                    use_alias    = c.get("use_alias"),
                    auth_type    = c.get("auth_type", "password"),
                    access_token = c.get("access_token", ""),
                )
                for c in _imap_raw
                if c.get("email")
            ]

            if not _clients:
                raise ValueError(
                    "No valid IMAP accounts configured. "
                    "Add at least one entry under Settings → IMAP 账户."
                )

            if _index is not None:
                if _index >= len(_clients):
                    raise ValueError(
                        f"imap:{_index} out of range — "
                        f"{len(_clients)} account(s) configured (index 0–{len(_clients) - 1})"
                    )
                return _clients[_index]

            return _clients[0] if len(_clients) == 1 else MultiIMAPMailClient(_clients)

        # ── Outlook / Hotmail ─────────────────────────────────────────────
        case _ if provider.lower() in ("outlook", "hotmail") or provider.lower().startswith("outlook:"):
            _parts = provider.split(":", 1)
            _index = None
            if len(_parts) == 2 and _parts[1].isdigit():
                _index = int(_parts[1])

            _out_raw = (_get_cfg().get("mail") or {}).get("outlook", [])
            if isinstance(_out_raw, dict):
                _out_raw = [_out_raw]

            _clients_out = [
                OutlookMailClient(
                    email         = c.get("email", ""),
                    client_id     = c.get("client_id", ""),
                    tenant_id     = c.get("tenant_id", "consumers"),
                    refresh_token = c.get("refresh_token", ""),
                    access_token  = c.get("access_token", ""),
                    fetch_method  = c.get("fetch_method", "graph"),
                )
                for c in _out_raw
                if c.get("email") and c.get("client_id")
            ]

            if not _clients_out:
                raise ValueError(
                    "No valid Outlook accounts configured. "
                    "Add at least one entry under Settings → Outlook/Hotmail."
                )

            if _index is not None:
                if _index >= len(_clients_out):
                    raise ValueError(
                        f"outlook:{_index} out of range — "
                        f"{len(_clients_out)} account(s) configured"
                    )
                return _clients_out[_index]

            return _clients_out[0] if len(_clients_out) == 1 else MultiOutlookMailClient(_clients_out)

        case _:
            raise ValueError(f"Unknown mail provider: {provider!r}")



