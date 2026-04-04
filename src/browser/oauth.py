"""
browser/oauth.py — Post-registration OAuth PKCE token acquisition.

After registration completes, the browser holds valid auth.openai.com cookies.
This module uses those cookies to complete a Codex OAuth2 PKCE flow without
re-entering credentials.

Flow:
  1. Generate PKCE code_verifier + code_challenge
  2. Register a Playwright route interceptor for http://localhost:1455/**
  3. Navigate the browser to /oauth/authorize — auth0 auto-authenticates via
     the existing registration-session cookies
  4. Handle the consent / workspace-select page if it appears (click Allow)
  5. Extract the authorization code from the intercepted callback URL
  6. POST /oauth/token to exchange code → access / refresh / id tokens
  7. Return a TokenResult dataclass
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs

import httpx
from loguru import logger
from playwright.async_api import Page

from src.browser.helpers import (
    human_move_and_click,
    wait_any_element,
    set_react_input,
    click_submit_or_text,
)

# ── OAuth constants (mirrors plan/chatgpt_register_sentinel.py defaults) ────
OAUTH_ISSUER       = "https://auth.openai.com"
OAUTH_CLIENT_ID    = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"
OAUTH_SCOPE        = "openid profile email offline_access"

# Selectors covering ALL pages in the OAuth redirect chain:
#   consent (Allow/Authorize), about-you (Continue), workspace/org (Continue),
#   terms agreement (Accept/Agree), etc.
# Priority: explicit Allow/Authorize first; generic Continue/Submit last.
_FLOW_SELECTORS: list[str] = [
    "button:has-text('Allow')",
    "button:has-text('Authorize')",
    "button[data-testid='allow-button']",
    "button[data-testid='consent-allow']",
    "button[data-action='allow']",
    "input[type='submit'][value*='Allow' i]",
    "input[type='submit'][value*='Authorize' i]",
    # Intermediate OAuth pages (about-you, workspace, org, age-verification, terms)
    "button:has-text('Continue')",
    "button:has-text('Accept')",
    "button:has-text('Agree')",
    "button:has-text('Next')",
    "button[type='submit']:not([disabled])",
]

# Keep the old name for backwards compatibility (used in unit tests / imports)
_CONSENT_SELECTORS = _FLOW_SELECTORS


# ── Token result model ───────────────────────────────────────────────────────

@dataclass
class TokenResult:
    access_token:  str
    refresh_token: str
    id_token:      str
    expires_in:    int
    email:         str = ""
    account_id:    str = ""
    expires_at:    str = ""

    @classmethod
    def from_response(cls, data: dict, email: str = "") -> "TokenResult":
        at      = data.get("access_token", "")
        payload = _decode_jwt(at)
        auth    = payload.get("https://api.openai.com/auth", {})
        acc_id  = auth.get("chatgpt_account_id", "")
        exp     = payload.get("exp", 0)
        expires_at = ""
        if exp:
            dt = datetime.fromtimestamp(exp, tz=timezone(timedelta(hours=8)))
            expires_at = dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        return cls(
            access_token  = at,
            refresh_token = data.get("refresh_token", ""),
            id_token      = data.get("id_token", ""),
            expires_in    = data.get("expires_in", 0),
            email         = email,
            account_id    = acc_id,
            expires_at    = expires_at,
        )

    def to_dict(self) -> dict:
        now = datetime.now(tz=timezone(timedelta(hours=8)))
        return {
            "type":          "codex",
            "email":         self.email,
            "account_id":    self.account_id,
            "access_token":  self.access_token,
            "refresh_token": self.refresh_token,
            "id_token":      self.id_token,
            "expired":       self.expires_at,
            "last_refresh":  now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        }


# ── PKCE helpers ─────────────────────────────────────────────────────────────

def _generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) as per RFC 7636."""
    verifier  = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _decode_jwt(token: str) -> dict:
    """Decode the payload section of a JWT (no signature verification)."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}


def _extract_code(url: str) -> Optional[str]:
    """Extract the 'code' query parameter from a URL, or None."""
    if not url or "code=" not in url:
        return None
    try:
        return parse_qs(urlparse(url).query).get("code", [None])[0]
    except Exception:
        return None


# ── Main entry point ─────────────────────────────────────────────────────────

async def acquire_tokens_via_browser(
    page: Page,
    email: str,
    password: str = "",
    first_name: str = "",
    last_name: str = "",
    birthday: Optional[dict] = None,
    proxy: Optional[str] = None,
    timeout: float = 45.0,
) -> Optional[TokenResult]:
    """
    Use the browser's existing authenticated session to perform a Codex PKCE
    OAuth2 authorization and acquire access / refresh / id tokens.

    The browser must already hold valid ``auth.openai.com`` cookies (i.e. the
    registration flow has just completed on the same page context).

    Args:
        page:       Playwright page with an active OpenAI auth session.
        email:      The registered e-mail (used to label the token result).
        password:   Account password — used if Auth0 shows the login page.
        first_name: First name — used to fill about-you form if encountered.
        last_name:  Last name — used to fill about-you form if encountered.
        birthday:   Dict {year, month, day} — used to fill about-you form.
        proxy:      Optional HTTP proxy URL for the token-exchange request.
        timeout:    Seconds to wait for the authorization code callback.

    Returns:
        ``TokenResult`` on success, ``None`` on any failure (always non-fatal).
    """
    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(24)

    authorize_url = (
        f"{OAUTH_ISSUER}/oauth/authorize?"
        + urlencode({
            "response_type":         "code",
            "client_id":             OAUTH_CLIENT_ID,
            "redirect_uri":          OAUTH_REDIRECT_URI,
            "scope":                 OAUTH_SCOPE,
            "code_challenge":        challenge,
            "code_challenge_method": "S256",
            "state":                 state,
        })
    )

    # ── Register localhost callback interceptor ──────────────────────────────
    captured: list[str] = []

    async def _intercept(route):
        url  = route.request.url
        code = _extract_code(url)
        if code:
            captured.append(code)
            logger.debug(f"[oauth] Callback intercepted — code len={len(code)}")
        try:
            await route.abort()   # localhost doesn't actually exist — just abort
        except Exception:
            pass

    await page.route("http://localhost:1455/**", _intercept)

    try:
        logger.info(f"[oauth] Starting PKCE OAuth flow for {email}")

        # ── Navigate to OAuth authorize endpoint ─────────────────────────────
        # The browser's auth.openai.com cookies let Auth0 skip re-authentication
        # and go straight to the consent / workspace-selection page.
        try:
            await page.goto(authorize_url, wait_until="commit", timeout=20_000)
        except Exception as e:
            err = str(e)
            # ERR_CONNECTION_REFUSED / ERR_ABORTED are expected when the browser
            # tries to load the localhost callback URL — the route intercept aborts
            # the request, which raises a navigation exception in Playwright.
            if any(s in err for s in ("ERR_CONNECTION_REFUSED", "ERR_ABORTED",
                                       "net::ERR", "NS_BINDING_ABORTED")):
                logger.debug("[oauth] Expected localhost navigation error — checking capture")
            else:
                logger.debug(f"[oauth] goto exception: {e}")

        if captured:
            logger.info(f"[oauth] Code immediately captured — exchanging for tokens")
            return await _exchange_code(captured[0], verifier, email, proxy)

        # ── Handle Auth0 login page (session not recognized / account incomplete) ─
        await asyncio.sleep(2.0)
        current_url = page.url
        logger.debug(f"[oauth] Post-goto URL: {current_url}")

        if "log-in" in current_url or "/login" in current_url:
            logger.info(f"[oauth] Auth0 login page detected — filling credentials to re-authenticate")

            # Step 1: Fill email
            email_result = await wait_any_element(
                page,
                ["input[type='email']", "input[name='email']", "input[name='username']",
                 "#username", "input[id*='email']"],
                timeout_ms=8_000,
            )
            if email_result:
                e_sel, _ = email_result
                await set_react_input(page, e_sel, email)
                logger.debug(f"[oauth] Filled email on login page")
                await asyncio.sleep(1.0)
                await click_submit_or_text(page, ["Continue", "Next", "继续", "Submit"])
                await asyncio.sleep(2.0)

            # Step 2: Fill password (may appear on same or next page)
            if password:
                pw_result = await wait_any_element(
                    page,
                    ["input[type='password']", "input[name='password']"],
                    timeout_ms=10_000,
                )
                if pw_result:
                    p_sel, _ = pw_result
                    await set_react_input(page, p_sel, password)
                    logger.debug(f"[oauth] Filled password on login page")
                    await asyncio.sleep(1.0)
                    await click_submit_or_text(
                        page, ["Continue", "Login", "Sign in", "Submit", "继续"]
                    )
                    await asyncio.sleep(3.0)
                else:
                    logger.warning("[oauth] Password input not found on login page")
            else:
                logger.warning("[oauth] No password provided — cannot log in via OAuth login page")

        # ── Handle consent / about-you / workspace pages (iterative click-through) ─
        for attempt in range(1, 8):
            if captured:
                logger.info(f"[oauth] Code captured (len={len(captured[0])}) — exchanging for tokens")
                return await _exchange_code(captured[0], verifier, email, proxy)

            logger.info(f"[oauth] Attempting OAuth flow click-through (try {attempt})")

            # When stuck on about-you, fill the profile form BEFORE clicking Continue
            if "about-you" in page.url:
                fn = first_name or "James"
                ln = last_name or "Smith"
                bd = birthday or {"year": 1990, "month": 6, "day": 15}
                logger.info(f"[oauth] About-you page in OAuth flow — filling profile ({fn} {ln})")
                await _fill_about_you_js(page, fn, ln, bd)
                await asyncio.sleep(1.5)

            # Wait for any of the flow selectors to appear
            element = await wait_any_element(page, _FLOW_SELECTORS, timeout_ms=8_000)
            if not element:
                logger.warning(f"[oauth] No elements found for click-through (try {attempt}) at {page.url}")
                continue

            # Click the detected button — this should advance the OAuth flow
            matched_sel, btn = element
            logger.info(f"[oauth] Clicking flow button (sel={matched_sel!r}) at {page.url}")
            await human_move_and_click(page, btn)

            # Wait briefly for potential redirects / URL changes
            await asyncio.sleep(3.0)

            # Check if we captured the authorization code
            if captured:
                logger.info(f"[oauth] Code captured (len={len(captured[0])}) — exchanging for tokens")
                return await _exchange_code(captured[0], verifier, email, proxy)

    finally:
        try:
            await page.unroute("http://localhost:1455/**")
        except Exception:
            pass

    logger.warning(f"[oauth] Failed to complete OAuth flow for {email} — code not captured")
    return None


# ── About-you profile fill helper ────────────────────────────────────────────

async def _fill_about_you_js(
    page: Page,
    first_name: str,
    last_name: str,
    birthday: dict,
) -> None:
    """
    Fill the auth.openai.com/about-you profile form using JavaScript.

    The about-you page currently has:
      - input[type='text', name='name']   — combined full name field
      - input[type='number', name='age']  — age in years (appears reactively after name is set)
      - 3 custom ARIA spinbuttons         — year / month / day birthday pickers
        (these are NOT regular <input> elements → not found by querySelectorAll('input'))

    Strategy:
      1. Fill the name input via React native setter
      2. Wait up to 2 s for the age input to appear (it's conditionally rendered)
      3. Fill the age input with the computed integer age
      4. Fill any birthday spinbuttons (native <input type='number'>) via direct JS
    """
    import json as _json
    from datetime import datetime as _dt

    full_name = _json.dumps(f"{first_name} {last_name}")

    # Compute integer age from birthday
    bd = birthday or {"year": 1990, "month": 1, "day": 1}
    today = _dt.now()
    age = today.year - bd["year"]
    if (today.month, today.day) < (bd["month"], bd["day"]):
        age -= 1
    age_str = _json.dumps(str(max(age, 1)))

    date_str = _json.dumps(
        f"{bd['year']}/{str(bd['month']).zfill(2)}/{str(bd['day']).zfill(2)}"
    )

    # ── Step 1: Fill visible non-control inputs (name + optionally age) ──
    result = await page.evaluate(f"""
        () => {{
            const BAD = new Set(['hidden','password','checkbox','radio',
                                  'submit','button','file','image','reset']);
            const inputs = Array.from(document.querySelectorAll('input')).filter(el => {{
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && !BAD.has(el.type);
            }});
            const nv = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            );
            const fire = (el, val) => {{
                nv.set.call(el, val);
                el.dispatchEvent(new Event('input',  {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                el.dispatchEvent(new Event('blur',   {{bubbles: true}}));
            }};
            if (inputs.length > 0) fire(inputs[0], {full_name});
            if (inputs.length > 1) {{
                // If second input is a number type it is the age field — fill with integer age.
                // Otherwise fall back to date string for date-type inputs.
                const val = inputs[1].type === 'number' ? {age_str} : {date_str};
                fire(inputs[1], val);
            }}
            return inputs.map(el => ({{type: el.type, name: el.name, id: el.id}}));
        }}
    """)
    logger.debug(f"[oauth] About-you JS fill — inputs found: {result}")

    # ── Step 2: Wait briefly for age input to appear (conditional render) ─
    await asyncio.sleep(1.5)

    # ── Step 3: Re-scan — age input may now be visible after name was set ─
    result2 = await page.evaluate(f"""
        () => {{
            const BAD = new Set(['hidden','password','checkbox','radio',
                                  'submit','button','file','image','reset']);
            const inputs = Array.from(document.querySelectorAll('input')).filter(el => {{
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && !BAD.has(el.type);
            }});
            const nv = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            );
            const fire = (el, val) => {{
                nv.set.call(el, val);
                el.dispatchEvent(new Event('input',  {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                el.dispatchEvent(new Event('blur',   {{bubbles: true}}));
            }};
            let filled = [];
            for (const el of inputs) {{
                if (el.name === 'name' || el.type === 'text') {{
                    if (!el.value) fire(el, {full_name});
                    filled.push('name:' + el.value.substring(0, 10));
                }} else if (el.type === 'number') {{
                    fire(el, {age_str});
                    filled.push('age:' + {age_str});
                }} else if (el.type === 'date') {{
                    fire(el, {date_str});
                    filled.push('date:' + {date_str});
                }}
            }}
            return filled;
        }}
    """)
    if result2:
        logger.debug(f"[oauth] About-you re-scan fill result: {result2}")

    # ── Step 4: Fill birthday spinbuttons ─────────────────────────────────
    # Use page.evaluate() for one-shot detection — avoids locator.evaluate() 30 s
    # per-element timeout that caused 4-minute hangs on every OAuth retry.
    try:
        sb_info = await page.evaluate("""
            () => {
                const sbs = Array.from(document.querySelectorAll('[role="spinbutton"]'));
                return sbs.map((el, i) => ({
                    idx:   i,
                    label: (el.getAttribute('aria-label') || '').toLowerCase(),
                    max:   parseInt(el.getAttribute('aria-valuemax') || '0', 10),
                    now:   parseInt(el.getAttribute('aria-valuenow') || el.innerText || '0', 10),
                }));
            }
        """)
        logger.debug(f"[oauth] Spinbutton info (one-shot): {sb_info}")

        if sb_info and len(sb_info) >= 3:
            def _detect_sb(info: dict) -> str:
                label = info.get("label", "")
                mx    = info.get("max", 0)
                if "year"  in label or mx > 200:        return "year"
                if "month" in label or (0 < mx <= 12): return "month"
                if "day"   in label or (12 < mx <= 31): return "day"
                return "unknown"

            field_order = [_detect_sb(sb) for sb in sb_info[:3]]
            if set(field_order) != {"year", "month", "day"}:
                field_order = ["month", "day", "year"]
            logger.debug(f"[oauth] Spinbutton order: {field_order}")

            from src.browser.helpers import fill_spinbutton
            for i, field in enumerate(field_order):
                val = bd.get(field, 1)
                await fill_spinbutton(page, i, val)
                logger.debug(f"[oauth] sb[{i}] {field}={val} done")
        else:
            logger.debug(f"[oauth] Only {len(sb_info) if sb_info else 0} spinbuttons found — skipping date fill")
    except Exception as _e:
        logger.debug(f"[oauth] Spinbutton fill skipped: {_e}")


# ── Token exchange ───────────────────────────────────────────────────────────

async def _exchange_code(
    code: str,
    verifier: str,
    email: str,
    proxy: Optional[str] = None,
) -> Optional[TokenResult]:
    """
    POST /oauth/token to exchange an authorization code for tokens.

    Uses httpx (async) with optional proxy support.
    """
    body = urlencode({
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  OAUTH_REDIRECT_URI,
        "client_id":     OAUTH_CLIENT_ID,
        "code_verifier": verifier,
    }).encode()

    # Build the AsyncClient kwargs; httpx >= 0.24 accepts proxy= directly.
    client_kwargs: dict = {"timeout": 30.0}
    if proxy:
        client_kwargs["proxy"] = proxy

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(
                f"{OAUTH_ISSUER}/oauth/token",
                content=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as exc:
        logger.error(f"[oauth] Token exchange request failed: {exc}")
        return None

    if resp.status_code != 200:
        logger.warning(
            f"[oauth] /oauth/token returned {resp.status_code}: {resp.text[:300]}"
        )
        return None

    try:
        data = resp.json()
    except Exception:
        logger.warning("[oauth] Token response body is not valid JSON")
        return None

    if not data.get("access_token"):
        logger.warning(
            f"[oauth] Token response missing access_token — keys: {list(data.keys())}"
        )
        return None

    result = TokenResult.from_response(data, email=email)
    logger.success(
        f"[oauth] ✅ Tokens acquired — email={email} "
        f"account_id={result.account_id} expires={result.expires_at}"
    )
    return result









