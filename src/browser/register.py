"""
browser/register.py — ChatGPT account registration state machine.

Mirrors plan/browser/tool.js _0x548_inner exactly:

  States: GOTO_SIGNUP → FILL_EMAIL → FILL_PASSWORD → WAIT_CODE → FILL_CODE → FILL_PROFILE → COMPLETE

Flow (from tool.js):
  1. Navigate to chatgpt.com/auth/login
     NextAuth 302-redirects → auth.openai.com (Auth0 Universal Login)
  2. GOTO_SIGNUP
     • Check if email input already visible (Auth0 may load immediately)
       → if yes: fill email inline, click Continue, wait for password
     • Else: look for "Sign up" link/button, click it, wait 3 s
  3. FILL_EMAIL  — wait for email input, fill, click Continue
  4. FILL_PASSWORD — wait for password input (≤60 s), fill, click Continue
  5. WAIT_CODE   — poll gptmail inbox for 6-digit code while OTP page loads
  6. FILL_CODE   — fill individual maxlength=1 boxes or one-time-code input
  7. FILL_PROFILE — fill firstName, lastName, birthday spinbuttons, click Agree
  8. COMPLETE    — URL no longer contains auth.openai.com

CLI dry-run:
    python -m src.browser.register --dry-run
"""
from __future__ import annotations

import asyncio
import random
import string
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from playwright.async_api import Page

from src.browser.engine import create_page
from src.browser.helpers import (
    click_submit_or_text,
    find_signup_button,
    human_move_and_click,
    is_error_page,
    is_visible,
    jitter_sleep,
    set_react_input,
    set_spinbutton,
    wait_any_element,
)
from src.mail.base import MailClient

# ── Constants ──────────────────────────────────────────────────────────────

LOGIN_URL   = "https://chatgpt.com/auth/login"
AUTH0_HOST  = "auth.openai.com"

MAX_RETRIES  = 5
# Fallback timeout constants — overridden at runtime by cfg["timeouts"] values.
CODE_TIMEOUT = 180   # seconds to poll for OTP e-mail (default; see timeouts.otp_code)

# Email selectors — mirrors tool.js GOTO_SIGNUP + FILL_EMAIL order
_EMAIL_SELECTORS = [
    "input[type='email']",
    "input[name='email']",
    "input[name='username']",
    "#username",
    "input[id*='email']",
    "input[autocomplete='email']",
    "input[inputmode='email']",
]

# Password selectors — mirrors tool.js _0x98d
_PASSWORD_SELECTORS = [
    "input[type='password']",
    "input[name='password']",
]

# OTP selectors — mirrors tool.js _0x98d wait loop + _0xbaf
_OTP_BOX_SELECTOR    = "input[type='text'][maxlength='1'], input[maxlength='1']"
_OTP_SINGLE_SELECTORS = [
    "input[autocomplete='one-time-code']",
    "input[name='code']",
    "input[id*='code']",
]

# Profile selectors — mirrors tool.js _0xcc0
_FNAME_SELECTORS = [
    "input[name='firstName']",
    "input[name='first_name']",
    "input[id*='firstName']",
    "input[id*='first-name']",
]
_LNAME_SELECTORS = [
    "input[name='lastName']",
    "input[name='last_name']",
    "input[id*='lastName']",
    "input[id*='last-name']",
]

# ── Name / birthday / password generators ─────────────────────────────────

_FIRST_NAMES = [
    "James", "John", "Robert", "Michael", "William", "David", "Richard",
    "Joseph", "Thomas", "Charles", "Mary", "Patricia", "Jennifer", "Linda",
    "Barbara", "Elizabeth", "Susan", "Jessica", "Sarah", "Karen", "Emma",
    "Olivia", "Ava", "Sophia", "Isabella", "Liam", "Noah", "Oliver",
    "Elijah", "Lucas",
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Thompson", "White", "Harris", "Clark",
]


def _gen_name() -> tuple[str, str]:
    return random.choice(_FIRST_NAMES), random.choice(_LAST_NAMES)


def _gen_birthday() -> dict:
    year  = datetime.now().year - 18 - random.randint(0, 30)
    month = random.randint(1, 12)
    day   = random.randint(1, 28)
    return {"year": year, "month": month, "day": day}


def _gen_password(length: int = 16) -> str:
    """
    Mirrors tool.js _0xae(16):
    guaranteed uppercase + lowercase + digit + special, then shuffled.
    """
    chars = string.ascii_uppercase + string.ascii_lowercase + string.digits + "!@#$%"
    parts = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice("!@#$%"),
    ]
    for _ in range(length - 4):
        parts.append(random.choice(chars))
    random.shuffle(parts)
    return "".join(parts)


def _gen_prefix(length: int = 12) -> str:
    chars = string.ascii_lowercase + string.digits
    return random.choice(string.ascii_lowercase) + "".join(
        random.choice(chars) for _ in range(length - 1)
    )


# ── Custom exceptions ──────────────────────────────────────────────────────

class RegistrationError(Exception):
    """Raised when a retryable registration step fails."""


class FatalRegistrationError(Exception):
    """Raised when registration cannot be retried (e.g. email creation failed)."""


# ── Public entry-point ─────────────────────────────────────────────────────

async def register_one(
    task_id: str,
    cfg: dict,
    mail_client: MailClient,
    proxy: Optional[str] = None,
) -> dict:
    """
    Run a single end-to-end ChatGPT registration mirroring tool.js flow.

    Returns a dict with at least:
        email, password, firstName, lastName, status, provider, proxy, createdAt
    """
    first_name, last_name = _gen_name()
    birthday  = _gen_birthday()
    password  = _gen_password()
    reg_cfg   = cfg.get("registration", {})
    prefix    = reg_cfg.get("prefix") or _gen_prefix()
    domain    = reg_cfg.get("domain") or None
    engine    = cfg.get("engine", "playwright")
    headless  = cfg.get("headless", True)
    slow_mo   = cfg.get("slow_mo", 0)
    timeouts  = cfg.get("timeouts", {})
    if not headless and slow_mo == 0:
        slow_mo = 80

    logger.info(f"[{task_id}] Creating e-mail via {cfg.get('mail_provider', 'gptmail')}")
    try:
        email = await mail_client.generate_email(prefix=prefix, domain=domain)
    except Exception as exc:
        logger.error(f"[{task_id}] E-mail creation failed: {exc}")
        return {"email": "", "status": "email_creation_failed", "error": str(exc)}

    account: dict = {
        "email":     email,
        "password":  password,
        "firstName": first_name,
        "lastName":  last_name,
        "birthday":  birthday,
        "status":    "starting",
        "provider":  cfg.get("mail_provider", "gptmail"),
        "proxy":     proxy or "",
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }

    async with create_page(engine=engine, proxy=proxy, headless=headless, slow_mo=slow_mo) as page:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(f"[{task_id}] Attempt {attempt}/{MAX_RETRIES} — {email}")
                await _state_machine(task_id, page, account, mail_client, timeouts)
                account["status"] = "注册完成"
                logger.success(f"[{task_id}] ✅ Done: {email}")

                # ── Post-registration: Codex OAuth token acquisition ──────────
                # The browser still holds valid auth.openai.com cookies from the
                # completed registration session.  Re-using the same page means
                # Auth0 can skip re-authentication and go straight to the PKCE
                # consent / workspace-select step.
                if cfg.get("enable_oauth", True):
                    try:
                        from src.browser.oauth import acquire_tokens_via_browser
                        token = await acquire_tokens_via_browser(
                            page=page,
                            email=email,
                            password=password,
                            first_name=first_name,
                            last_name=last_name,
                            birthday=birthday,
                            proxy=proxy,
                            timeouts=timeouts,
                        )
                        if token:
                            account.update(token.to_dict())
                            logger.success(
                                f"[{task_id}] 🔑 OAuth tokens acquired — "
                                f"account_id={token.account_id} "
                                f"expires={token.expires_at}"
                            )
                        else:
                            logger.warning(
                                f"[{task_id}] OAuth step returned None — "
                                "registration result saved without tokens"
                            )
                    except Exception as _oe:
                        logger.warning(
                            f"[{task_id}] OAuth step error (non-fatal): {_oe}"
                        )

                return account

            except RegistrationError as exc:
                logger.warning(f"[{task_id}] Retry {attempt}: {exc}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(3 * attempt)
                    try:
                        await page.goto(LOGIN_URL, wait_until="domcontentloaded",
                                        timeout=int(timeouts.get("page_load", 30) * 1000))
                    except Exception:
                        pass

            except Exception as exc:
                logger.error(f"[{task_id}] Unexpected error (attempt {attempt}): {exc}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(5)
                    try:
                        await page.goto(LOGIN_URL, wait_until="domcontentloaded",
                                        timeout=int(timeouts.get("page_load", 30) * 1000))
                    except Exception:
                        pass

        account["status"] = "failed"
        return account


# ── State machine ──────────────────────────────────────────────────────────

async def _state_machine(
    task_id: str,
    page: Page,
    account: dict,
    mail_client: MailClient,
    timeouts: dict,
) -> None:
    """
    Sequentially executes the 7-state flow matching tool.js _0x548_inner:
      GOTO_SIGNUP → FILL_EMAIL → FILL_PASSWORD → WAIT_CODE → FILL_CODE → FILL_PROFILE → COMPLETE
    """

    # ── STATE: GOTO_SIGNUP ────────────────────────────────────────────
    # tool.js: window.location.href = 'https://chatgpt.com/auth/login'
    # NextAuth 302-redirects → auth.openai.com Universal Login
    logger.info(f"[{task_id}] GOTO_SIGNUP → {LOGIN_URL}")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded",
                    timeout=int(timeouts.get("page_load", 30) * 1000))

    # Wait for Auth0 redirect (usually < 5 s)
    try:
        await page.wait_for_url(f"**{AUTH0_HOST}**",
                                timeout=int(timeouts.get("auth0_redirect", 8) * 1000))
    except Exception:
        pass

    # tool.js: await _0x1ae(0x7d0) — 2 s then check errors
    # Add jitter so timing pattern is less uniform
    await jitter_sleep(2.0, 0.5)
    await _assert_not_error(task_id, page)
    logger.debug(f"[{task_id}] GOTO_SIGNUP landed: {page.url}")

    # ── tool.js GOTO_SIGNUP: check if email input already visible ────
    # _vis('input[type="email"], input[name="email"], input[name="username"], #username')
    email_already_visible = await _find_visible_email_input(page)

    if email_already_visible:
        # Auth0 has already shown the email form — skip signup button lookup
        logger.info(f"[{task_id}] Email input already visible — proceeding to FILL_EMAIL")
        await jitter_sleep(0.5, 0.2)
    else:
        # Find and click the "Sign Up" button
        # tool.js: querySelector('a[href*="signup"], [data-testid="signup-link"]')
        #          then text-match /^(sign up|sign up for free|免费注册|create account)$/i
        logger.info(f"[{task_id}] Looking for Sign Up button — URL={page.url}")
        signup_btn = await find_signup_button(task_id, page)

        if not signup_btn:
            # Give it another 3 s (tool.js retry cycle is 1.5 s)
            await jitter_sleep(3.0, 0.8)
            signup_btn = await find_signup_button(task_id, page)

        if signup_btn:
            logger.info(f"[{task_id}] Clicking Sign Up button (human simulation)")
            # Use human-like mouse movement before click — Auth0 detects direct clicks
            await human_move_and_click(page, signup_btn)

            # tool.js: await _0x1ae(0xbb8) — 3 s after clicking signup
            await jitter_sleep(3.0, 0.8)
            await _assert_not_error(task_id, page)
            logger.debug(f"[{task_id}] After signup click: {page.url}")
        else:
            raise RegistrationError(
                f"Sign Up button not found after retrying. URL={page.url}"
            )

    # ── STATE: FILL_EMAIL ─────────────────────────────────────────────
    # tool.js: _0x1bf(email_selectors, 0x3a98) — wait up to 15 s
    logger.info(f"[{task_id}] FILL_EMAIL — URL={page.url}")
    email_result = await wait_any_element(
        page, _EMAIL_SELECTORS,
        timeout_ms=int(timeouts.get("email_input", 15) * 1000),
    )
    if not email_result:
        try:
            snippet = (await page.content())[:600]
            logger.debug(f"[{task_id}] Page snippet:\n{snippet}")
        except Exception:
            pass
        raise RegistrationError(f"Email input not found. URL={page.url}")

    matched_sel, email_el = email_result
    logger.debug(f"[{task_id}] Email input matched: {matched_sel!r}")

    # tool.js: await _0x1ae(0x3e8) — 1 s before fill, add jitter
    await jitter_sleep(1.0, 0.3)

    # tool.js: _0x1c0(ei, d.email) — React nativeSetter + events
    await set_react_input(page, matched_sel, account["email"])
    await jitter_sleep(0.5, 0.2)

    # Find submit button and use human click to avoid bot detection
    sub_loc = None
    try:
        sub = page.locator("button[type='submit']").first
        if await sub.is_visible():
            sub_loc = sub
    except Exception:
        pass

    if sub_loc:
        await human_move_and_click(page, sub_loc)
        submitted = True
    else:
        submitted = await click_submit_or_text(page, ["Continue", "继续", "Next", "Submit"])
    if not submitted:
        try:
            await email_el.press("Enter")
        except Exception:
            pass

    logger.debug(f"[{task_id}] Email submitted")

    # ── STATE: FILL_PASSWORD ──────────────────────────────────────────
    # tool.js: _0x98d(d) — polls up to 60 s for password input
    logger.info(f"[{task_id}] FILL_PASSWORD — waiting for password input (≤{timeouts.get('password_input', 60)} s)")
    pw_result = await wait_any_element(
        page, _PASSWORD_SELECTORS,
        timeout_ms=int(timeouts.get("password_input", 60) * 1000),
    )
    if not pw_result:
        raise RegistrationError(
            f"Password input not found after email submit. URL={page.url}"
        )
    await _assert_not_error(task_id, page)

    matched_pw_sel, pw_el = pw_result
    logger.debug(f"[{task_id}] Password input matched: {matched_pw_sel!r}")

    # tool.js: _0x1ae(0x1f4) — 0.5 s before fill, add jitter
    await jitter_sleep(0.5, 0.2)

    # tool.js: _0x1c0(pi, d.password)
    await set_react_input(page, matched_pw_sel, account["password"])
    logger.debug(f"[{task_id}] Password filled")

    # tool.js: _0x1ae(0x3e8) — 1 s then click submit (human simulation)
    await jitter_sleep(1.0, 0.3)
    pw_sub_loc = None
    try:
        pw_sub = page.locator("button[type='submit']").first
        if await pw_sub.is_visible():
            pw_sub_loc = pw_sub
    except Exception:
        pass

    if pw_sub_loc:
        await human_move_and_click(page, pw_sub_loc)
        submitted_pw = True
    else:
        submitted_pw = await click_submit_or_text(page, ["Continue", "继续", "Next", "Submit"])
    if not submitted_pw:
        try:
            await pw_el.press("Enter")
        except Exception:
            pass

    logger.debug(f"[{task_id}] Password submitted — waiting for OTP page")

    # ── STATE: WAIT_CODE ──────────────────────────────────────────────
    # tool.js: _0x98d wait loop — checks for input[maxlength="1"] or
    # autocomplete="one-time-code" every 1 s, up to 60 s
    logger.info(f"[{task_id}] WAIT_CODE — waiting for OTP inputs (≤{timeouts.get('otp_input', 60)} s)")
    otp_appeared = await _wait_for_otp_inputs(
        page, timeout_ms=int(timeouts.get("otp_input", 60) * 1000),
    )
    if not otp_appeared:
        raise RegistrationError(
            f"OTP input did not appear after password submit. URL={page.url}"
        )
    await _assert_not_error(task_id, page)
    logger.info(f"[{task_id}] OTP page loaded — polling gptmail inbox")

    # tool.js: _0xa9e — poll gptmail every 3 s up to 60 iterations
    await jitter_sleep(2.0, 0.5)  # _0x1ae(0x7d0)
    code = await mail_client.poll_code(
        account["email"],
        timeout=int(timeouts.get("otp_code", CODE_TIMEOUT)),
    )
    if not code:
        raise RegistrationError("OTP code not received within timeout")

    logger.info(f"[{task_id}] FILL_CODE → {code}")

    # ── STATE: FILL_CODE ──────────────────────────────────────────────
    # tool.js: _0xbaf(c, d) — fill individual digit boxes or single field
    await _fill_otp(page, code)
    await jitter_sleep(1.0, 0.3)  # _0x1ae(0x3e8)

    submitted_otp = await click_submit_or_text(
        page, ["Continue", "Verify", "Submit", "继续"]
    )
    if not submitted_otp:
        logger.debug(f"[{task_id}] No OTP submit button — likely auto-submitted on last digit")

    await jitter_sleep(3.0, 0.8)
    await _assert_not_error(task_id, page)
    logger.debug(f"[{task_id}] After OTP, URL={page.url}")

    # ── STATE: FILL_PROFILE ───────────────────────────────────────────
    # tool.js: _0xbaf waits up to 60 s for firstName input; then calls _0xcc0(d)
    logger.info(f"[{task_id}] Checking for FILL_PROFILE page")
    fname_result = await wait_any_element(
        page, _FNAME_SELECTORS,
        timeout_ms=int(timeouts.get("profile_detect", 15) * 1000),
    )
    if fname_result:
        logger.info(f"[{task_id}] FILL_PROFILE — URL={page.url}")
        await _fill_profile(task_id, page, account, timeouts)
    else:
        logger.debug(f"[{task_id}] No profile page — registration may be complete already")

    # ── STATE: COMPLETE ───────────────────────────────────────────────
    # tool.js: !u.includes('auth.openai.com') && !u.includes('/auth/')
    logger.info(f"[{task_id}] COMPLETE — waiting for chatgpt.com redirect")
    try:
        await page.wait_for_url("**/chatgpt.com/**",
                                timeout=int(timeouts.get("complete_redirect", 20) * 1000))
    except Exception:
        pass

    await asyncio.sleep(2.0)
    final_url = page.url
    logger.info(f"[{task_id}] Final URL={final_url}")

    if AUTH0_HOST in final_url or "/auth/" in final_url:
        logger.warning(
            f"[{task_id}] Still on auth page after completion: {final_url}"
        )


# ── Sub-routines ───────────────────────────────────────────────────────────

async def _assert_not_error(task_id: str, page: Page) -> None:
    """
    Mirrors tool.js error detection:
      • URL contains /api/auth/error
      • body text contains '糟糕', '出错了', 'Operation timed out', '操作超时'
    """
    if "/api/auth/error" in page.url:
        raise RegistrationError(
            f"NextAuth error page — bot-detected or OAuth config issue: {page.url}"
        )
    if await is_error_page(page):
        raise RegistrationError(f"Error page detected at {page.url}")


async def _find_visible_email_input(page: Page) -> bool:
    """
    Check if an email/username input is currently visible.
    Mirrors tool.js:
      _vis('input[type="email"], input[name="email"], input[name="username"], #username')
    """
    for sel in [
        "input[type='email']",
        "input[name='email']",
        "input[name='username']",
        "#username",
    ]:
        if await is_visible(page, sel):
            return True
    return False


async def _wait_for_otp_inputs(page: Page, timeout_ms: int = 60_000) -> bool:
    """
    Wait for OTP input elements to appear.
    Mirrors tool.js _0x98d inner loop:
      input[type="text"][maxlength="1"]  (≥ 4 means OTP page)
      input[autocomplete="one-time-code"]
      input[name="code"]
    """
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
    while asyncio.get_event_loop().time() < deadline:
        try:
            count = await page.locator(
                "input[type='text'][maxlength='1'], input[maxlength='1']"
            ).count()
            if count >= 4:
                return True
        except Exception:
            pass
        for sel in _OTP_SINGLE_SELECTORS:
            if await is_visible(page, sel):
                return True
        await asyncio.sleep(1.0)
    return False


async def _fill_otp(page: Page, code: str) -> None:
    """
    Fill OTP.
    Mirrors tool.js _0xbaf:
      If ≥ 6 individual maxlength=1 boxes → _0x1c0(ci[i], c[i]) each digit.
      Else → _0x1c0 on single autocomplete="one-time-code" or name="code" input.
    """
    boxes = page.locator("input[type='text'][maxlength='1'], input[maxlength='1']")
    count = await boxes.count()

    if count >= 4:
        # Individual digit boxes (Auth0 style)
        for i, ch in enumerate(code[:count]):
            box = boxes.nth(i)
            try:
                await box.click()
            except Exception:
                pass
            await box.fill(ch)
            await asyncio.sleep(0.1)  # _0x1ae(0x64)
    else:
        # Single OTP input
        for sel in _OTP_SINGLE_SELECTORS:
            ok = await set_react_input(page, sel, code)
            if ok:
                break


async def _fill_profile(task_id: str, page: Page, account: dict, timeouts: dict) -> None:
    """
    Fill name + birthday spinbuttons.
    Mirrors tool.js _0xcc0(d):
      • querySelector firstName/lastName inputs → _0x1c0
      • querySelectorAll('[role="spinbutton"]') → _0x1d1(sb, value)
      • querySelector 'button[type="submit"]' → click
    """
    bd = account["birthday"]
    _pf_ms = int(timeouts.get("profile_field", 5) * 1000)

    # Name fields
    fname_result = await wait_any_element(page, _FNAME_SELECTORS, timeout_ms=_pf_ms)
    lname_result = await wait_any_element(page, _LNAME_SELECTORS, timeout_ms=_pf_ms)

    if fname_result and lname_result:
        f_sel, _ = fname_result
        l_sel, _ = lname_result
        await set_react_input(page, f_sel, account["firstName"])
        await set_react_input(page, l_sel, account["lastName"])
    else:
        # Fallback: single full-name input
        name_result = await wait_any_element(
            page,
            ["input[name='name']", "input[name='fullName']", "input[id*='name']"],
            timeout_ms=max(3_000, _pf_ms // 2),
        )
        if name_result:
            n_sel, _ = name_result
            await set_react_input(
                page, n_sel,
                f"{account['firstName']} {account['lastName']}"
            )
        else:
            logger.warning(f"[{task_id}] Name inputs not found — skipping")

    await asyncio.sleep(0.5)  # _0x1ae(0x1f4)

    # Birthday spinbuttons: [role="spinbutton"] year / month / day
    spinbuttons = page.locator("[role='spinbutton']")
    sb_count    = await spinbuttons.count()

    if sb_count >= 3:
        logger.info(f"[{task_id}] Setting birthday via spinbuttons: {bd}")
        await set_spinbutton(page, spinbuttons.nth(0), bd["year"])
        await set_spinbutton(page, spinbuttons.nth(1), bd["month"])
        await set_spinbutton(page, spinbuttons.nth(2), bd["day"])
    else:
        # Fallback: date input
        date_str = (
            f"{bd['year']}/{str(bd['month']).zfill(2)}/{str(bd['day']).zfill(2)}"
        )
        date_result = await wait_any_element(
            page,
            ["input[type='date']", "input[name*='birth']", "input[id*='birth']"],
            timeout_ms=max(3_000, _pf_ms // 2),
        )
        if date_result:
            d_sel, _ = date_result
            await set_react_input(page, d_sel, date_str)

    await asyncio.sleep(0.5)

    # Submit profile
    submitted = await click_submit_or_text(
        page, ["Continue", "Agree", "同意", "Next", "Finish", "Done", "继续"]
    )
    if not submitted:
        logger.warning(f"[{task_id}] Profile submit button not found")

    await asyncio.sleep(2.0)
    logger.debug(f"[{task_id}] After profile submit, URL={page.url}")


# ── CLI dry-run ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    async def _dry_run() -> None:
        logger.info("[dry-run] Simulating tool.js registration state machine")
        states = [
            "GOTO_SIGNUP  — navigate chatgpt.com/auth/login, find & click Sign Up",
            "FILL_EMAIL   — wait email input, fill, click Continue",
            "FILL_PASSWORD — wait password input (≤60s), fill, click Continue",
            "WAIT_CODE    — poll gptmail for 6-digit OTP code",
            "FILL_CODE    — fill individual digit boxes, click Continue",
            "FILL_PROFILE — fill firstName/lastName + birthday spinbuttons",
            "COMPLETE     — verify redirect to chatgpt.com",
        ]
        for i, s in enumerate(states, 1):
            logger.info(f"[task-0] [{i}/{len(states)}] {s}")
            await asyncio.sleep(0.15)
        logger.success("[dry-run] State machine trace complete — no errors")

    asyncio.run(_dry_run())
