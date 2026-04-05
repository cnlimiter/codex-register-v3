"""
mail/imap.py — Generic IMAP mailbox client (supports multiple accounts, alias mode, OAuth2).

Unlike the API-based providers (gptmail / yydsmail), this client connects
directly to any standard IMAP server using the account's own credentials.

**Alias mode** (auto-enabled for qq.com and gmail.com mailboxes):
    generate_email() returns ``local+{random8}@domain`` instead of the bare
    address.  All aliases land in the same inbox; poll_code() filters by the
    ``To:`` / ``Delivered-To:`` headers so concurrent registrations sharing one
    mailbox receive the right code.

Configuration in SQLite settings:
    mail_provider: imap
    mail:
      imap:
        - email:    user@gmail.com
          password: app-password        # Gmail: use app-specific password
          host:     imap.gmail.com
          port:     993                 # 993 = IMAPS (SSL), 143 = STARTTLS
          ssl:      true
          folder:   INBOX
          # use_alias: true            # override auto-detect (omit → auto)
        - email:    user2@qq.com
          password: qq-app-password
          host:     imap.qq.com
          port:     993
          ssl:      true
          folder:   INBOX

CLI smoke-test:
    python -m src.mail.imap
"""
from __future__ import annotations

import asyncio
import base64
import email as email_lib
import random
import re
import string
import time
from email.header import decode_header, make_header
from typing import Optional

import aioimaplib
from loguru import logger

from src.mail.base import MailClient

# ── Constants ─────────────────────────────────────────────────────────────

_ALIAS_DOMAINS: frozenset[str] = frozenset({"qq.com", "gmail.com"})

# Well-known IMAP hosts auto-detected from email domain.
_AUTO_HOSTS: dict[str, str] = {
    "gmail.com":    "imap.gmail.com",
    "qq.com":       "imap.qq.com",
    "foxmail.com":  "imap.qq.com",
    "163.com":      "imap.163.com",
    "126.com":      "imap.126.com",
    "yeah.net":     "imap.yeah.net",
    "hotmail.com":  "imap-mail.outlook.com",
    "outlook.com":  "imap-mail.outlook.com",
    "live.com":     "imap-mail.outlook.com",
    "msn.com":      "imap-mail.outlook.com",
}

_CODE_RE          = re.compile(r"\b(\d{6})\b")
_CODE_FALLBACK_RE = re.compile(r"\b(\d{4,8})\b")


# ── Helpers ───────────────────────────────────────────────────────────────

def _extract_code(text: str) -> Optional[str]:
    """Return the first 6-digit (or 4–8 digit fallback) numeric code in *text*."""
    m = _CODE_RE.search(text)
    if m:
        return m.group(1)
    m = _CODE_FALLBACK_RE.search(text)
    return m.group(1) if m else None


def _decode_str(raw) -> str:
    """Decode an RFC-2047 encoded email header value to a plain string."""
    try:
        return str(make_header(decode_header(raw or "")))
    except Exception:
        return str(raw or "")


def _extract_text(msg: email_lib.message.Message) -> str:
    """Walk a parsed email message and concatenate all text parts."""
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in ("text/plain", "text/html"):
                try:
                    charset = part.get_content_charset() or "utf-8"
                    payload = part.get_payload(decode=True)
                    if payload:
                        parts.append(payload.decode(charset, errors="replace"))
                except Exception:
                    pass
    else:
        try:
            charset = msg.get_content_charset() or "utf-8"
            payload = msg.get_payload(decode=True)
            if payload:
                parts.append(payload.decode(charset, errors="replace"))
        except Exception:
            pass
    return " ".join(parts)


def _random_alias(length: int = 8) -> str:
    """Return a random alphanumeric string for use as a '+alias' suffix."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def _make_xoauth2_token(email: str, access_token: str) -> str:
    """Build the base64-encoded XOAUTH2 SASL token for IMAP authentication."""
    raw = f"user={email}\x01auth=Bearer {access_token}\x01\x01"
    return base64.b64encode(raw.encode()).decode()


# ── Single-account IMAP client ────────────────────────────────────────────

class IMAPMailClient(MailClient):
    """
    Generic IMAP client that wraps a single existing mailbox.

    Parameters
    ----------
    email        : Full e-mail address used for login.
    password     : IMAP password (ignored when auth_type='oauth2').
    host         : IMAP server. Auto-detected from domain when empty.
    port         : 993 = IMAPS (SSL), 143 = STARTTLS.
    ssl          : True → IMAPS; False → plain/STARTTLS.
    folder       : Mailbox folder (default 'INBOX').
    use_alias    : None = auto-detect (qq.com/gmail.com), True/False = override.
    auth_type    : 'password' (default) or 'oauth2' (XOAUTH2).
    access_token : Bearer token required when auth_type='oauth2'.
    """

    def __init__(
        self,
        email: str,
        password: str = "",
        host: str = "",
        port: int = 993,
        ssl: bool = True,
        folder: str = "INBOX",
        use_alias: Optional[bool] = None,
        auth_type: str = "password",
        access_token: str = "",
    ) -> None:
        self._email        = email
        self._password     = password
        self._host         = host or _AUTO_HOSTS.get(email.split("@")[-1].lower() if "@" in email else "", "")
        self._port         = port
        self._ssl          = ssl
        self._folder       = folder
        self._auth_type    = auth_type
        self._access_token = access_token

        if use_alias is None:
            domain    = email.split("@")[-1].lower() if "@" in email else ""
            use_alias = domain in _ALIAS_DOMAINS
        self._use_alias: bool = use_alias

    # ── generate ─────────────────────────────────────────────────────────

    async def generate_email(
        self,
        prefix: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> str:
        """
        Return a usable registration address.

        * **Alias mode** (qq.com / gmail.com or ``use_alias: true``):
          Returns ``local+{random8}@domain``.  The inbox still receives all
          messages sent to any ``+alias`` variant.
        * **Standard mode**: Returns the configured address as-is.
        """
        if self._use_alias:
            local, _, dom = self._email.partition("@")
            # Strip any pre-existing alias suffix before adding a new one.
            local = local.split("+")[0]
            alias_email = f"{local}+{_random_alias()}@{dom}"
            logger.info(f"[IMAP] Alias mode — using {alias_email} (inbox: {self._email})")
            return alias_email

        logger.info(f"[IMAP] Using fixed mailbox: {self._email}")
        return self._email

    # ── poll ─────────────────────────────────────────────────────────────

    async def poll_code(self, email: str, timeout: int = 120) -> Optional[str]:
        """
        Connect to the IMAP server, repeatedly search for unseen messages
        and return the first OTP code found in subject + body text.

        When *email* differs from the configured mailbox address (i.e., an
        alias was used), only messages whose ``To:`` / ``Delivered-To:``
        header contains *email* are considered — this prevents concurrent
        tasks sharing the same inbox from stealing each other's codes.
        """
        deadline      = time.monotonic() + timeout
        seen_uids: set[str] = set()
        poll_interval = 4   # seconds between IMAP searches

        # If an alias was used, filter incoming messages by To: header.
        filter_to: Optional[str] = (
            email.lower()
            if email.lower() != self._email.lower()
            else None
        )

        logger.info(
            f"[IMAP] Polling {self._folder} on {self._host}:{self._port} "
            f"for {email} (timeout={timeout}s, alias_filter={filter_to is not None})"
        )

        while time.monotonic() < deadline:
            imap = None
            try:
                # ── Connect & authenticate ────────────────────────────────
                if self._ssl:
                    imap = aioimaplib.IMAP4_SSL(
                        host=self._host, port=self._port,
                        timeout=15,
                    )
                else:
                    imap = aioimaplib.IMAP4(
                        host=self._host, port=self._port,
                        timeout=15,
                    )

                await imap.wait_hello_from_server()
                if self._auth_type == "oauth2":
                    token = _make_xoauth2_token(self._email, self._access_token)
                    ok, _ = await imap.authenticate("XOAUTH2", lambda x: token.encode())
                else:
                    ok, _ = await imap.login(self._email, self._password)
                if ok != "OK":
                    logger.warning(f"[IMAP] Login failed: {ok}")
                    await asyncio.sleep(poll_interval)
                    continue

                ok, _ = await imap.select(self._folder)
                if ok != "OK":
                    logger.warning(f"[IMAP] SELECT {self._folder} failed: {ok}")
                    await asyncio.sleep(poll_interval)
                    continue

                # ── Search for all UNSEEN messages ────────────────────────
                ok, data = await imap.search("UNSEEN")
                if ok != "OK":
                    await asyncio.sleep(poll_interval)
                    continue

                uid_list: list[str] = []
                if data and data[0]:
                    raw = data[0]
                    if isinstance(raw, bytes):
                        raw = raw.decode()
                    uid_list = [u for u in raw.split() if u]

                # Filter UIDs we've already checked
                new_uids = [u for u in uid_list if u not in seen_uids]
                seen_uids.update(uid_list)

                for uid in new_uids:
                    ok, msg_data = await imap.fetch(uid, "(RFC822)")
                    if ok != "OK":
                        continue

                    # aioimaplib returns a list; find the raw bytes entry
                    raw_bytes: Optional[bytes] = None
                    for part in msg_data:
                        if isinstance(part, bytes) and len(part) > 100:
                            raw_bytes = part
                            break

                    if not raw_bytes:
                        continue

                    msg = email_lib.message_from_bytes(raw_bytes)

                    # ── Alias filtering: check To: / Delivered-To: headers ─
                    if filter_to is not None:
                        to_hdr          = _decode_str(msg.get("To", "")).lower()
                        delivered_to_hdr = _decode_str(msg.get("Delivered-To", "")).lower()
                        if filter_to not in to_hdr and filter_to not in delivered_to_hdr:
                            logger.debug(
                                f"[IMAP] uid={uid} skipped — "
                                f"To: {to_hdr[:60]!r} doesn't match {filter_to!r}"
                            )
                            continue

                    subject  = _decode_str(msg.get("Subject", ""))
                    body     = _extract_text(msg)
                    combined = f"{subject} {body}"

                    logger.debug(f"[IMAP] uid={uid} subject={subject[:60]!r}")
                    code = _extract_code(combined)
                    if code:
                        logger.info(f"[IMAP] Code {code} found in uid={uid}")
                        await imap.logout()
                        return code

            except asyncio.TimeoutError:
                logger.warning("[IMAP] Connection timed out — retrying")
            except OSError as exc:
                logger.warning(f"[IMAP] Network error: {exc}")
            except Exception as exc:
                logger.warning(f"[IMAP] Unexpected error: {exc}")
            finally:
                if imap is not None:
                    try:
                        await imap.logout()
                    except Exception:
                        pass

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval, remaining))

        logger.warning(f"[IMAP] Timed out waiting for code ({email})")
        return None


# ── Multi-account IMAP client ─────────────────────────────────────────────

class MultiIMAPMailClient(MailClient):
    """
    Wraps multiple :class:`IMAPMailClient` instances and randomly selects one
    per ``generate_email()`` call.

    The chosen client is stored per-alias-email so that a subsequent
    ``poll_code(alias_email)`` always queries the correct inbox — safe for
    concurrent registrations that share a single ``MultiIMAPMailClient`` instance.
    """

    def __init__(self, clients: list[IMAPMailClient]) -> None:
        if not clients:
            raise ValueError("MultiIMAPMailClient requires at least one account")
        self._clients = clients
        # Maps generated alias email → the IMAPMailClient that owns it.
        self._routing: dict[str, IMAPMailClient] = {}

    async def generate_email(
        self,
        prefix: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> str:
        client = random.choice(self._clients)
        addr   = await client.generate_email(prefix, domain)
        # Store mapping so poll_code() knows which inbox to check.
        self._routing[addr.lower()] = client
        return addr

    async def poll_code(self, email: str, timeout: int = 120) -> Optional[str]:
        client = self._routing.get(email.lower())
        if client is None:
            # Fallback: should not normally happen; pick random client.
            logger.warning(
                f"[IMAP] No routing entry for {email!r} — "
                "falling back to random client"
            )
            client = random.choice(self._clients)
        return await client.poll_code(email, timeout)


# ── CLI smoke-test ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import src.settings_db as settings_db

    async def _main() -> None:
        cfg      = await settings_db.build_config()
        imap_raw = (cfg.get("mail") or {}).get("imap", [])

        # Backward compat: single dict in config
        if isinstance(imap_raw, dict):
            imap_raw = [imap_raw]

        valid = [c for c in imap_raw if c.get("email")]
        if not valid:
            print(
                "No IMAP accounts found in SQLite settings.\n"
                "Open the WebUI and add entries under Settings → IMAP 账户 first.\n"
            )
            sys.exit(1)

        clients = [
            IMAPMailClient(
                email     = c["email"],
                password  = c["password"],
                host      = c["host"],
                port      = int(c.get("port", 993)),
                ssl       = bool(c.get("ssl", True)),
                folder    = c.get("folder", "INBOX"),
                use_alias = c.get("use_alias"),
            )
            for c in valid
        ]

        client = MultiIMAPMailClient(clients) if len(clients) > 1 else clients[0]
        addr   = await client.generate_email()
        print(f"Mailbox / alias: {addr}")
        print("Waiting 30 s for a verification code …")
        code = await client.poll_code(addr, timeout=30)
        print(f"Code: {code or '(none received)'}")

    asyncio.run(_main())

