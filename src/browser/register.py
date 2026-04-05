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

# Transient network error substrings that should trigger a retry rather than abort.
_NETWORK_ERRORS = (
    "NS_ERROR_NET_RESET",
    "NS_ERROR_CONNECTION_REFUSED",
    "NS_ERROR_NET_TIMEOUT",
    "NS_BINDING_ABORTED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_REFUSED",
    "ERR_TIMED_OUT",
    "ERR_EMPTY_RESPONSE",
    "net::ERR",
    "TimeoutError",
)

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


class SkipRegistrationError(FatalRegistrationError):
    """Raised when OTP verification page appears instead of password — skip this email."""


class EmailAlreadyRegisteredError(FatalRegistrationError):
    """Raised when Auth0 redirects to /log-in/password — email already has an account."""


# ── Network-safe navigation ────────────────────────────────────────────────

async def _safe_goto(
    task_id: str,
    page: Page,
    url: str,
    *,
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 60_000,
) -> None:
    """
    Navigate to *url*, converting transient network errors (NS_ERROR_NET_RESET,
    ERR_CONNECTION_RESET, etc.) into ``RegistrationError`` so the outer retry
    loop handles them gracefully with proper back-off instead of treating them
    as fatal "Unexpected error" events.
    """
    try:
        await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
    except Exception as exc:
        err = str(exc)
        if any(s in err for s in _NETWORK_ERRORS):
            raise RegistrationError(
                f"Transient network error navigating to {url}: {exc}"
            ) from exc
        raise


# ── Public entry-point ─────────────────────────────────────────────────────

async def register_one(
    task_id: str,
    cfg: dict,
    mail_client: MailClient,
    proxy: Optional[str] = None,
    log_fn=None,
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
    mobile    = bool(cfg.get("mobile", False))
    if not headless and slow_mo == 0:
        slow_mo = 80

    # ── Backward-compat: merge legacy 'timeout' (singular) keys into 'timeouts' ──
    # Older config shapes may still expose `timeout:` (singular); runtime reads
    # `timeouts:` (plural). Map the known keys so existing persisted settings
    # keep working.
    _legacy = cfg.get("timeout", {})
    if _legacy:
        _KEY_MAP = {
            "email_input":    "email_input",
            "password_input": "password_input",
            "otp_input":      "otp_input",
            "profile_input":  "profile_detect",
            "code_poll":      "otp_code",
            "page_load":      "page_load",
        }
        timeouts = dict(timeouts)   # don't mutate cfg in-place
        for old_k, new_k in _KEY_MAP.items():
            if old_k in _legacy and new_k not in timeouts:
                timeouts[new_k] = _legacy[old_k]

    logger.info(f"[{task_id}] Creating e-mail via {cfg.get('mail_provider', 'gptmail')}")
    if log_fn:
        log_fn("步骤 1/7: 申请注册邮箱…")
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

    async with create_page(engine=engine, proxy=proxy, headless=headless, slow_mo=slow_mo, mobile=mobile) as page:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(f"[{task_id}] Attempt {attempt}/{MAX_RETRIES} — {email}")
                if log_fn:
                    log_fn(f"步骤 2/7: 尝试注册 {attempt}/{MAX_RETRIES}，邮箱: {email}")
                await _state_machine(task_id, page, account, mail_client, timeouts, log_fn=log_fn)
                account["status"] = "注册完成"
                logger.success(f"[{task_id}] ✅ Done: {email}")

                # ── Post-registration: Codex OAuth token acquisition ──────────
                # The browser still holds valid auth.openai.com cookies from the
                # completed registration session.  Re-using the SAME page means
                # Auth0 can skip re-authentication, and the fingerprint (desktop
                # or mobile, depending on the top-level `mobile` config) is
                # already set — no need for a separate mobile context.
                if cfg.get("enable_oauth", True):
                    try:
                        from src.browser.oauth import acquire_tokens_via_browser
                        if log_fn:
                            log_fn("步骤 8/8: 获取 OAuth 访问令牌…")
                        token = await acquire_tokens_via_browser(
                            page=page,
                            email=email,
                            password=password,
                            first_name=first_name,
                            last_name=last_name,
                            birthday=birthday,
                            proxy=proxy,
                            timeouts=timeouts,
                            mail_client=mail_client,
                            mobile=False,   # page already carries the correct fingerprint
                            log_fn=log_fn,
                        )
                        if token:
                            account.update(token.to_dict())
                            logger.success(
                                f"[{task_id}] 🔑 OAuth tokens acquired — "
                                f"account_id={token.account_id} "
                                f"expires={token.expires_at}"
                            )
                            if log_fn:
                                log_fn(f"[OAuth] ✅ 令牌获取成功 account_id={token.account_id}")
                        else:
                            logger.warning(
                                f"[{task_id}] OAuth step returned None — "
                                "registration result saved without tokens"
                            )
                            if log_fn:
                                log_fn("[OAuth] ⚠️ 令牌获取失败，注册结果已保存（无令牌）")
                    except Exception as _oe:
                        logger.warning(
                            f"[{task_id}] OAuth step error (non-fatal): {_oe}"
                        )
                        if log_fn:
                            log_fn(f"[OAuth] ⚠️ 令牌获取异常（非致命）: {_oe}")

                return account

            except EmailAlreadyRegisteredError as exc:
                logger.warning(f"[{task_id}] ⚠️ 邮箱已注册，跳过: {account['email']}")
                if log_fn:
                    log_fn(f"⚠️ 跳过：该邮箱已注册 ({account['email']})")
                account["status"] = "already_registered"
                account["error"] = str(exc)
                return account

            # except SkipRegistrationError as exc:
            #     logger.warning(f"[{task_id}] Skipped (OTP detected): {exc}")
            #     if log_fn:
            #         log_fn(f"⚠️ 跳过：检测到 OTP 验证登录，无密码注册流程 ({email})")
            #     account["status"] = "skipped_otp_verify"
            #     account["error"] = str(exc)
            #     return account

            except RegistrationError as exc:
                logger.warning(f"[{task_id}] Retry {attempt}/{MAX_RETRIES}: {exc}")
                if attempt < MAX_RETRIES:
                    # Exponential back-off: 10 s, 20 s, 30 s, 40 s … capped at 60 s
                    backoff = min(10 * attempt, 60)
                    logger.info(f"[{task_id}] Waiting {backoff}s before retry …")
                    await asyncio.sleep(backoff)
                    try:
                        await _safe_goto(task_id, page, LOGIN_URL,
                                         timeout_ms=int(timeouts.get("page_load", 60) * 1000))
                    except Exception:
                        pass

            except Exception as exc:
                err_str = str(exc)
                is_network = any(s in err_str for s in _NETWORK_ERRORS)
                if is_network:
                    logger.warning(f"[{task_id}] Network error (attempt {attempt}): {exc}")
                else:
                    logger.error(f"[{task_id}] Unexpected error (attempt {attempt}): {exc}")
                if attempt < MAX_RETRIES:
                    backoff = min(10 * attempt, 60) if is_network else min(5 * attempt, 30)
                    logger.info(f"[{task_id}] Waiting {backoff}s before retry …")
                    await asyncio.sleep(backoff)
                    try:
                        await _safe_goto(task_id, page, LOGIN_URL,
                                         timeout_ms=int(timeouts.get("page_load", 60) * 1000))
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
    log_fn=None,
) -> None:
    """
    Sequentially executes the 7-state flow matching tool.js _0x548_inner:
      GOTO_SIGNUP → FILL_EMAIL → FILL_PASSWORD → WAIT_CODE → FILL_CODE → FILL_PROFILE → COMPLETE
    """
    def _step(n: int, msg: str) -> None:
        logger.info(f"[{task_id}] [{n}/7] {msg}")
        if log_fn:
            log_fn(f"步骤 {n}/7: {msg}")

    # ── STATE: GOTO_SIGNUP ────────────────────────────────────────────
    # tool.js: window.location.href = 'https://chatgpt.com/auth/login'
    # NextAuth 302-redirects → auth.openai.com Universal Login
    _step(1, f"导航到注册入口 {LOGIN_URL}")
    await _safe_goto(task_id, page, LOGIN_URL,
                     timeout_ms=int(timeouts.get("page_load", 60) * 1000))

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
        logger.info(f"[{task_id}] Looking for Sign Up button — URL={page.url}")
        _step(2, "查找并点击「注册」入口")
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
    _step(3, f"填写邮箱 {account['email']}")
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
    # After email submit, detect if page shows password input OR OTP verification.
    # Some accounts (or certain Auth0 tenant configurations) skip the password
    # step entirely and go straight to OTP — we continue the registration in
    # that case rather than aborting.
    _step(4, "等待密码输入框（或直接进入 OTP 验证环节）")
    logger.info(f"[{task_id}] FILL_PASSWORD — waiting for password or OTP (≤{timeouts.get('password_input', 60)} s)")

    detected = await _wait_for_password_or_otp(
        page, timeout_ms=int(timeouts.get("password_input", 60) * 1000),
    )

    if detected == "already_registered":
        raise EmailAlreadyRegisteredError(
            f"邮箱 {account['email']} 已注册（Auth0 跳转至登录密码页 /log-in/password）"
        )
    elif detected == "otp":
        # Auth0 skipped the password step and went straight to OTP verification.
        # This is a valid registration path — continue to WAIT_CODE directly.
        _step(4, "Auth0 直接跳转到 OTP 验证（无密码步骤），继续填写验证码")
        logger.info(
            f"[{task_id}] OTP page detected immediately after email "
            "(no password step) — jumping to WAIT_CODE"
        )
        # otp_already_visible = True  → skip _wait_for_otp_inputs below
        skip_pw_to_otp = True
    elif detected == "none":
        raise RegistrationError(
            f"Password input not found after email submit. URL={page.url}"
        )
    else:
        skip_pw_to_otp = False

    if not skip_pw_to_otp:
        # detected == "password"
        pw_result = await wait_any_element(
            page, _PASSWORD_SELECTORS, timeout_ms=3000,
        )
        if not pw_result:
            raise RegistrationError(f"Password input not found (post-detect). URL={page.url}")
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
    _step(5, f"等待验证码邮件（轮询收件箱，超时 {timeouts.get('otp_code', 180)}s）")

    if skip_pw_to_otp:
        # OTP inputs are already visible — no need to wait for them to appear
        logger.info(f"[{task_id}] WAIT_CODE — OTP inputs already visible (no-password path)")
    else:
        logger.info(f"[{task_id}] WAIT_CODE — waiting for OTP inputs (≤{timeouts.get('otp_input', 60)} s)")
        otp_appeared = await _wait_for_otp_inputs(
            page, timeout_ms=int(timeouts.get("otp_input", 60) * 1000),
        )
        if not otp_appeared:
            raise RegistrationError(
                f"OTP input did not appear after password submit. URL={page.url}"
            )
    await _assert_not_error(task_id, page)
    logger.info(f"[{task_id}] OTP page loaded — polling mail inbox")

    # tool.js: _0xa9e — poll gptmail every 3 s up to 60 iterations
    # If no code arrives within timeout, click the "Resend" button and retry
    # (up to _RESEND_MAX extra attempts).
    _RESEND_MAX = 2
    code = None
    for _resend_attempt in range(_RESEND_MAX + 1):
        await jitter_sleep(2.0, 0.5)  # _0x1ae(0x7d0)
        code = await mail_client.poll_code(
            account["email"],
            timeout=int(timeouts.get("otp_code", CODE_TIMEOUT)),
        )
        if code:
            break

        if _resend_attempt < _RESEND_MAX:
            logger.warning(
                f"[{task_id}] 验证码未收到（第 {_resend_attempt + 1}/{_RESEND_MAX} 次重发）"
            )
            if log_fn:
                log_fn(f"⚠️ 验证码超时，尝试重新发送（{_resend_attempt + 1}/{_RESEND_MAX}）…")
            resent = await _click_resend_button(task_id, page)
            if not resent:
                logger.warning(f"[{task_id}] 未找到重发按钮，放弃重试")
                break
            # Brief wait for the new email to be sent before polling again
            await asyncio.sleep(3.0)

    if not code:
        raise RegistrationError("OTP code not received within timeout (including resend retries)")

    logger.info(f"[{task_id}] FILL_CODE → {code}")
    _step(6, f"填写验证码 {code}")

    # ── STATE: FILL_CODE ──────────────────────────────────────────────
    # tool.js: _0xbaf(c, d) — fill individual digit boxes or single field
    # If Auth0 returns "Incorrect code", click Resend, fetch a NEW code from
    # mail (different from the last one), and retry.
    _OTP_WRONG_MAX = 2
    _otp_ok = False
    _otp_failure_reason: Optional[str] = None

    for _wrong_attempt in range(_OTP_WRONG_MAX + 1):
        if _wrong_attempt > 0:
            logger.warning(
                f"[{task_id}] OTP 被判定错误/过期，准备重发并拉取新验证码 "
                f"（第 {_wrong_attempt}/{_OTP_WRONG_MAX} 次）"
            )
            if log_fn:
                log_fn(f"⚠️ 验证码错误或已过期，重新发送并获取新验证码（{_wrong_attempt}/{_OTP_WRONG_MAX}）…")

            resent = await _click_resend_button(task_id, page)
            if not resent:
                _otp_failure_reason = "验证码错误后未找到重发按钮"
                logger.warning(f"[{task_id}] {_otp_failure_reason}")
                break

            await asyncio.sleep(3.0)  # allow the new email to be dispatched
            new_code = await _poll_fresh_code(
                task_id,
                mail_client,
                account["email"],
                previous_code=code,
                timeout=int(timeouts.get("otp_code", CODE_TIMEOUT)),
            )
            if not new_code:
                _otp_failure_reason = "重发后未收到新的验证码"
                logger.warning(f"[{task_id}] {_otp_failure_reason}")
                break

            code = new_code
            logger.info(f"[{task_id}] 新验证码 → {code}")
            _step(6, f"重新填写验证码 {code}")

        await _fill_otp(page, code)
        await jitter_sleep(1.0, 0.3)

        submitted_otp = await click_submit_or_text(
            page, ["Continue", "Verify", "Submit", "继续"]
        )
        if not submitted_otp:
            logger.debug(f"[{task_id}] No OTP submit button — likely auto-submitted on last digit")

        otp_result = await _classify_otp_submit_result(task_id, page)
        if otp_result == "accepted":
            _otp_ok = True
            break
        if otp_result == "incorrect":
            _otp_failure_reason = "Incorrect code"
            logger.warning(f"[{task_id}] Auth0 returned incorrect/expired OTP (attempt {_wrong_attempt + 1})")
            continue

        _otp_failure_reason = "OTP 提交后页面未前进，且未检测到明确错误提示"
        logger.warning(f"[{task_id}] {_otp_failure_reason}")
        break

    if not _otp_ok:
        raise RegistrationError(
            _otp_failure_reason or f"OTP 验证失败：超过 {_OTP_WRONG_MAX + 1} 次尝试"
        )

    await jitter_sleep(1.0, 0.3)
    await _assert_not_error(task_id, page)
    logger.debug(f"[{task_id}] After OTP, URL={page.url}")

    # ── STATE: FILL_PROFILE ───────────────────────────────────────────
    # tool.js: _0xbaf waits up to 60 s for firstName input; then calls _0xcc0(d)
    # NOTE: Auth0 /about-you page uses input[name='name'] (single full-name), not
    # separate firstName/lastName inputs.  Detect by URL first to avoid wasting
    # the full profile_detect timeout on a page that never has firstName inputs.
    logger.info(f"[{task_id}] Checking for FILL_PROFILE page")
    _profile_detect_ms = int(timeouts.get("profile_detect", 15) * 1000)
    on_about_you = "about-you" in page.url or "about_you" in page.url

    fname_result = None
    if not on_about_you:
        # Only look for separate firstName input when NOT on known about-you URL
        fname_result = await wait_any_element(
            page, _FNAME_SELECTORS, timeout_ms=_profile_detect_ms,
        )

    name_only_result = None
    if not fname_result:
        # Look for single name input (Auth0 /about-you layout)
        name_only_result = await wait_any_element(
            page,
            ["input[name='name']", "input[name='fullName']", "input[type='text']"],
            timeout_ms=5_000 if on_about_you else 3_000,
        )

    if fname_result or name_only_result or on_about_you:
        _step(7, "填写姓名和生日信息")
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

async def _wait_for_password_or_otp(page: Page, timeout_ms: int = 60_000) -> str:
    """
    After email submit, race between password input and OTP input.

    Returns:
        "password"          — normal password registration flow
        "otp"               — OTP/magic-link verification page (no password)
        "already_registered"— Auth0 redirected to /log-in/password (existing account)
        "none"              — neither appeared within timeout
    """
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000

    _otp_check_selectors = [
        "input[autocomplete='one-time-code']",
        "input[name='code']",
        "input[id*='code']",
    ]

    while asyncio.get_event_loop().time() < deadline:
        url = page.url.lower()

        # ── Detect already-registered: Auth0 login flow URL ──────────
        # When email is already registered, Auth0 redirects to the *login*
        # password page (/log-in/password) instead of the *signup* flow.
        # This must be checked BEFORE the password-selector check because
        # the login page also contains a password <input>.
        if "/log-in/password" in url:
            return "already_registered"

        # Check for password input first
        for sel in _PASSWORD_SELECTORS:
            try:
                if await is_visible(page, sel):
                    return "password"
            except Exception:
                pass

        # Check for OTP digit boxes (≥4 means OTP page)
        try:
            count = await page.locator(
                "input[type='text'][maxlength='1'], input[maxlength='1']"
            ).count()
            if count >= 4:
                return "otp"
        except Exception:
            pass

        # Check for single OTP field
        for sel in _otp_check_selectors:
            try:
                if await is_visible(page, sel):
                    return "otp"
            except Exception:
                pass

        # Check URL pattern for magic link / email verification login
        if any(kw in url for kw in ("magic", "email-link", "email-verify", "check-email")):
            return "otp"

        await asyncio.sleep(0.5)

    return "none"


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


async def _click_resend_button(task_id: str, page: Page) -> bool:
    """
    Look for a "Resend" / "Resend email" button on the Auth0 OTP page and click it.

    Returns True if a button was found and clicked, False otherwise.
    Tries Playwright text-based locators first (most reliable for Auth0), then
    falls back to common CSS selectors.
    """
    _text_variants = [
        "Resend email", "Resend", "Send again",
        "重新发送", "Didn't receive",
    ]
    # 1. Try Playwright get_by_role / get_by_text (most reliable)
    for text in _text_variants:
        for loc in [
            page.get_by_role("button", name=text, exact=False),
            page.get_by_role("link",   name=text, exact=False),
            page.get_by_text(text, exact=False),
        ]:
            try:
                if await loc.first.is_visible(timeout=800):
                    await loc.first.click()
                    logger.info(f"[{task_id}] 重发按钮已点击（文本: {text!r}）")
                    return True
            except Exception:
                pass

    # 2. CSS selector fallbacks
    _CSS_SELECTORS = [
        "[data-action-button-secondary]",
        "button[class*='resend']",
        "a[class*='resend']",
    ]
    for sel in _CSS_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=500):
                await loc.click()
                logger.info(f"[{task_id}] 重发按钮已点击（CSS: {sel!r}）")
                return True
        except Exception:
            pass

    logger.debug(f"[{task_id}] 未找到重发按钮（URL={page.url}）")
    return False


async def _is_otp_incorrect(page: Page) -> bool:
    """
    Return True if Auth0 is showing an "Incorrect code" / "Invalid code" error.

    Auth0 renders the validation error as text near the OTP inputs, usually
    inside an [role="alert"] / [aria-live] element or a visible <p>/<span>.
    We do a lightweight JS text scan to avoid slow Playwright waits.
    Returns False (i.e. "all good") if no error text is detected so the outer
    loop can proceed normally.
    """
    _ERROR_KEYWORDS = [
        "incorrect code",
        "invalid code",
        "wrong code",
        "code is incorrect",
        "code is invalid",
        "code has expired",
        "code expired",
        "验证码错误",
        "验证码无效",
        "验证码已过期",
        "输入的验证码不正确",
        "code entered is incorrect",
    ]

    try:
        visible_text: str = await page.evaluate("""
            () => {
                const picks = [
                    ...document.querySelectorAll('[role="alert"]'),
                    ...document.querySelectorAll('[aria-live]'),
                    ...document.querySelectorAll('[class*="error"],[class*="invalid"],[class*="alert"]'),
                    ...document.querySelectorAll('p, span, small'),
                ];
                return picks
                    .filter(el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    })
                    .map(el => (el.innerText || "").toLowerCase())
                    .join(" ");
            }
        """)
        if any(kw in visible_text for kw in _ERROR_KEYWORDS):
            return True
    except Exception:
        pass

    return False


async def _otp_inputs_present(page: Page) -> bool:
    """Return True if OTP input controls are still visible on the page."""
    try:
        count = await page.locator(_OTP_BOX_SELECTOR).count()
        if count >= 4:
            return True
    except Exception:
        pass

    for sel in _OTP_SINGLE_SELECTORS:
        try:
            if await is_visible(page, sel):
                return True
        except Exception:
            pass
    return False


async def _classify_otp_submit_result(task_id: str, page: Page, timeout_ms: int = 8_000) -> str:
    """
    Classify the result after submitting OTP.

    Returns:
      - "accepted": page advanced away from OTP (profile page, redirect, etc.)
      - "incorrect": explicit incorrect/invalid/expired-code message detected
      - "pending":  still on OTP page without a decisive signal
    """
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000

    while asyncio.get_event_loop().time() < deadline:
        if await _is_otp_incorrect(page):
            return "incorrect"

        url = page.url.lower()
        on_profile_url = "about-you" in url or "about_you" in url
        left_auth_flow = AUTH0_HOST not in url and "/auth/" not in url
        if on_profile_url or left_auth_flow:
            return "accepted"

        # Profile inputs becoming visible is also acceptance.
        for sel in [*_FNAME_SELECTORS, "input[name='name']", "input[name='fullName']"]:
            try:
                if await is_visible(page, sel):
                    logger.debug(f"[{task_id}] OTP accepted — profile selector visible: {sel}")
                    return "accepted"
            except Exception:
                pass

        # If OTP inputs disappeared and no error is shown, the page likely advanced.
        if not await _otp_inputs_present(page):
            return "accepted"

        await asyncio.sleep(0.5)

    return "pending"


async def _poll_fresh_code(
    task_id: str,
    mail_client: MailClient,
    email: str,
    *,
    previous_code: Optional[str],
    timeout: int,
) -> Optional[str]:
    """
    Poll mail until a code different from *previous_code* arrives.

    This protects flows where the provider may still expose the last OTP first.
    Outlook's seen-ID tracking already helps, but this keeps the logic safe for
    other MailClient implementations too.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        remaining = max(1, int(deadline - asyncio.get_event_loop().time()))
        chunk = min(15, remaining)
        fresh_code = await mail_client.poll_code(email, timeout=chunk)
        if fresh_code and fresh_code != previous_code:
            return fresh_code
        if fresh_code == previous_code:
            logger.debug(f"[{task_id}] 收到旧验证码 {fresh_code}，继续等待新验证码")
        await asyncio.sleep(1.0)
    return None


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
    Fill name + birthday spinbuttons on Auth0 /about-you page.

    Supports two name-field layouts:
      A) Separate firstName / lastName inputs  (old Auth0 style)
      B) Single input[name='name'] full-name   (current Auth0 /about-you)

    Spinbutton order is detected via aria-label + aria-valuemax — observed
    real-world order is month → day → year (not year → month → day).
    Mirrors oauth.py _fill_about_you_js() logic for consistency.
    """
    bd = account["birthday"]
    _pf_ms = int(timeouts.get("profile_field", 5) * 1000)
    full_name = f"{account['firstName']} {account['lastName']}"
    date_str  = f"{bd['year']}-{bd['month']:02d}-{bd['day']:02d}"

    # ── Step 1: Fill name field(s) ────────────────────────────────────────────
    fname_result = await wait_any_element(page, _FNAME_SELECTORS, timeout_ms=_pf_ms)
    lname_result = await wait_any_element(page, _LNAME_SELECTORS, timeout_ms=_pf_ms)

    if fname_result and lname_result:
        # Separate firstName / lastName inputs
        f_sel, _ = fname_result
        l_sel, _ = lname_result
        await set_react_input(page, f_sel, account["firstName"])
        await set_react_input(page, l_sel, account["lastName"])
        logger.info(f"[{task_id}] Name filled via separate first/last inputs")
    else:
        # Single full-name input (Auth0 /about-you)
        name_result = await wait_any_element(
            page,
            ["input[name='name']", "input[name='fullName']", "input[id*='name']"],
            timeout_ms=max(3_000, _pf_ms // 2),
        )
        if name_result:
            n_sel, _ = name_result
            await set_react_input(page, n_sel, full_name)
            logger.info(f"[{task_id}] Name filled via single name input {n_sel!r}: {full_name!r}")
        else:
            # Last resort: JS fill first visible text input
            await page.evaluate(f"""
            () => {{
                const BAD = new Set(['hidden','password','checkbox','radio',
                                     'submit','button','file','image','reset']);
                const inputs = Array.from(document.querySelectorAll('input')).filter(el => {{
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && !BAD.has(el.type);
                }});
                if (!inputs.length) return false;
                const nv = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value');
                nv.set.call(inputs[0], {repr(full_name)});
                inputs[0].dispatchEvent(new Event('input',  {{bubbles:true}}));
                inputs[0].dispatchEvent(new Event('change', {{bubbles:true}}));
                inputs[0].dispatchEvent(new Event('blur',   {{bubbles:true}}));
                return true;
            }}
            """)
            logger.warning(f"[{task_id}] Name inputs not found by selector — tried JS fallback")

    # Wait for conditional age/date inputs to appear after name is filled
    await asyncio.sleep(1.5)

    # ── Step 2: Fill birthday spinbuttons (label-aware, mirrors oauth.py) ─────
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
        logger.debug(f"[{task_id}] Profile spinbutton info: {sb_info}")

        if sb_info and len(sb_info) >= 3:
            def _detect_sb(info: dict) -> str:
                label = info.get("label", "")
                mx    = info.get("max", 0)
                if "year"  in label or mx > 200:         return "year"
                if "month" in label or (0 < mx <= 12):  return "month"
                if "day"   in label or (12 < mx <= 31): return "day"
                return "unknown"

            field_order = [_detect_sb(sb) for sb in sb_info[:3]]
            if set(field_order) != {"year", "month", "day"}:
                # Default observed order on Auth0 /about-you: month → day → year
                field_order = ["month", "day", "year"]
            logger.info(f"[{task_id}] Birthday spinbutton order: {field_order} — {bd}")

            for i, field in enumerate(field_order):
                val = bd.get(field, 1)
                await set_spinbutton(page, page.locator("[role='spinbutton']").nth(i), val)
                logger.debug(f"[{task_id}] Spinbutton[{i}] {field}={val} done")

        else:
            # Fewer than 3 spinbuttons — try <select> dropdowns first, then date input
            logger.debug(
                f"[{task_id}] Only {len(sb_info) if sb_info else 0} spinbutton(s) — "
                "trying <select> dropdowns then date input"
            )

            # ── Try <select> dropdown birthday pickers ────────────────────────
            _month_names = [
                "january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november", "december",
            ]
            select_filled: int = await page.evaluate("""
                ([month, day, year, monthNames]) => {
                    function fillSel(el, val, isMo) {
                        const strVal = String(val);
                        const padVal = strVal.padStart(2, '0');
                        const moName = isMo ? monthNames[val - 1] : '';
                        for (const opt of el.options) {
                            const v = opt.value.toLowerCase();
                            const t = opt.text.toLowerCase();
                            if (v === strVal || v === padVal ||
                                (isMo && (t.includes(moName) || v.includes(moName)))) {
                                el.value = opt.value;
                                el.dispatchEvent(new Event('change', {bubbles: true}));
                                el.dispatchEvent(new Event('input',  {bubbles: true}));
                                return true;
                            }
                        }
                        return false;
                    }
                    const selects = Array.from(document.querySelectorAll('select')).filter(el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    });
                    let filled = 0;
                    for (const el of selects) {
                        const hint = (
                            el.name + ' ' + el.id + ' ' +
                            (el.getAttribute('aria-label') || '') + ' ' +
                            (el.getAttribute('placeholder') || '')
                        ).toLowerCase();
                        const cnt = el.options.length;
                        let val, isMo = false;
                        if      (hint.includes('month') || cnt === 12 || cnt === 13) { val = month; isMo = true; }
                        else if (hint.includes('year')  || cnt > 50)                  { val = year;  }
                        else if (hint.includes('day')   || (cnt >= 28 && cnt <= 32))  { val = day;   }
                        else continue;
                        if (fillSel(el, val, isMo)) filled++;
                    }
                    return filled;
                }
            """, [bd["month"], bd["day"], bd["year"], _month_names])

            if select_filled and select_filled > 0:
                logger.info(f"[{task_id}] Birthday filled via {select_filled} <select> dropdown(s)")
            else:
                # Fall back to date input
                date_found = False
                for sel in ["input[type='date']", "input[name*='birth']", "input[id*='birth']"]:
                    if await is_visible(page, sel):
                        await set_react_input(page, sel, date_str)
                        logger.info(f"[{task_id}] Birthday via date selector {sel!r}: {date_str}")
                        date_found = True
                        break
                if not date_found:
                    logger.warning(
                        f"[{task_id}] Birthday input not found — skipping "
                        f"(selects_filled={select_filled})"
                    )

    except Exception as _e:
        logger.warning(f"[{task_id}] Spinbutton fill error: {_e}")

    await asyncio.sleep(0.5)

    # ── Step 3: Submit profile form ───────────────────────────────────────────
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
