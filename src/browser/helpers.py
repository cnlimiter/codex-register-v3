"""
browser/helpers.py — Low-level DOM interaction utilities.

All functions accept a playwright Page and work with both
camoufox (Firefox) and Chromium contexts.
"""
from __future__ import annotations

import asyncio
import random
from typing import Optional

from loguru import logger
from typing import Literal
from playwright.async_api import Page, Locator, TimeoutError as PWTimeoutError

# ── React-compatible input fill ───────────────────────────────────────────

_REACT_INPUT_JS = """
(args) => {
    const [selector, value] = args;
    const el = document.querySelector(selector);
    if (!el) return false;
    el.focus();
    // Trigger React's synthetic onChange via nativeInputValueSetter
    const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    setter.call(el, value);
    el.dispatchEvent(new Event('input',  { bubbles: true, composed: true }));
    el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    // Also dispatch key events for each character (some forms require it)
    for (const ch of value) {
        el.dispatchEvent(new KeyboardEvent('keydown',  {key: ch, bubbles: true}));
        el.dispatchEvent(new KeyboardEvent('keypress', {key: ch, bubbles: true}));
        el.dispatchEvent(new KeyboardEvent('keyup',    {key: ch, bubbles: true}));
    }
    el.dispatchEvent(new Event('blur', { bubbles: true }));
    el.focus();
    return true;
}
"""


async def set_react_input(page: Page, selector: str, value: str) -> bool:
    """
    Fill an input element in a way that triggers React's onChange.
    Falls back to playwright's built-in fill() if the JS approach fails.
    """
    try:
        ok = await page.evaluate(_REACT_INPUT_JS, [selector, value])
        if ok:
            return True
    except Exception as exc:
        logger.debug(f"[helpers] JS fill failed for {selector!r}: {exc}")

    # Fallback: use Playwright locator directly
    try:
        el = page.locator(selector).first
        await el.fill(value)
        return True
    except Exception as exc:
        logger.warning(f"[helpers] fill fallback failed for {selector!r}: {exc}")
        return False


# ── Element waiting ───────────────────────────────────────────────────────

async def wait_element(
    page: Page,
    selector: str,
    timeout_ms: int = 20_000,
    state: Literal["attached", "detached", "hidden", "visible"] = "visible",
) -> Optional[Locator]:
    """Wait for selector and return its Locator, or None on timeout."""
    try:
        await page.wait_for_selector(selector, state=state, timeout=timeout_ms)
        return page.locator(selector).first
    except PWTimeoutError:
        return None


async def wait_any_element(
    page: Page,
    selectors: list[str],
    timeout_ms: int = 20_000,
) -> Optional[tuple[str, Locator]]:
    """
    Wait for whichever selector appears first (visibility-checked).
    Returns (matched_selector, locator) or None on timeout.
    Each selector is tried individually so compound CSS is never needed.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    while loop.time() < deadline:
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible():
                    return sel, el
            except Exception:
                pass
        await asyncio.sleep(0.4)
    return None


async def fill_spinbutton(page: Page, index: int, target: int) -> None:
    """
    Locate the spinbutton at *index* (0-based) in the DOM and adjust it to
    *target* value using ArrowUp/ArrowDown keys.

    This is a convenience wrapper over set_spinbutton() that selects the
    nth spinbutton by index rather than requiring a pre-built Locator.
    Called by oauth._fill_about_you_js() during the post-registration
    OAuth flow's about-you profile form filling.
    """
    locator = page.locator("[role='spinbutton']").nth(index)
    await set_spinbutton(page, locator, target)


# ── CLI dry-run ────────────────────────────────────────────────────────────

async def find_button_by_texts(page: Page, texts: list[str]) -> Optional[Locator]:
    """
    Find the first visible button/link whose text matches one of *texts*
    (case-insensitive, partial match).
    Searches button, a, [role='button'], div, span — mirroring JS querySelectorAll.
    """
    for text in texts:
        for tag in ("button", "a", "[role='button']", "div", "span"):
            try:
                loc = page.locator(f"{tag}:has-text('{text}')").first
                if await loc.is_visible():
                    return loc
            except Exception:
                pass
    return None


async def click_button_by_texts(page: Page, texts: list[str]) -> bool:
    btn = await find_button_by_texts(page, texts)
    if btn:
        await btn.click()
        return True
    return False


async def click_submit_or_text(page: Page, texts: list[str]) -> bool:
    """
    Click a submit/continue button.
    Priority 1: button[type='submit']  (mirrors JS: document.querySelector('button[type="submit"]'))
    Priority 2: text-based search (find_button_by_texts)
    Priority 3: Enter press on active element
    """
    # Priority 1: visible submit button
    try:
        sub = page.locator("button[type='submit']").first
        if await sub.is_visible():
            await sub.click()
            return True
    except Exception:
        pass

    # Priority 2: text-based
    return await click_button_by_texts(page, texts)


async def find_signup_button(task_id: str, page: Page) -> Optional[Locator]:
    """
    Find the Sign Up entry-point using multiple strategies.
    Mirrors JS _0x548_inner GOTO_SIGNUP detection order:
      data-testid → href*signup → exact text (all elements) → partial text
    """
    # Strategy 1: data-testid (fastest when present)
    for testid in ("signup-link", "signup-button", "create-account", "register-button"):
        try:
            loc = page.locator(f"[data-testid='{testid}']").first
            if await loc.is_visible():
                logger.debug(f"[{task_id}] signup via data-testid={testid}")
                return loc
        except Exception:
            pass

    # Strategy 2: anchor with signup/register in href
    # auth.openai.com uses '/u/signup' sub-path; also match generic patterns.
    try:
        loc = page.locator(
            "a[href*='u/signup'], a[href*='signup'], a[href*='register'], a[href*='create-account']"
        ).first
        if await loc.is_visible():
            logger.debug(f"[{task_id}] signup via href")
            return loc
    except Exception:
        pass

    # Strategy 3: exact text match across ALL element types (mirrors JS regex)
    exact_texts = [
        "Sign up", "Sign Up", "Sign up for free",
        "Create account", "Create Account",
        "Get started", "Register",
        "注册", "免费注册",
    ]
    for text in exact_texts:
        for tag in ("button", "a", "[role='button']", "div", "span", "p"):
            try:
                loc = page.locator(tag).get_by_text(text, exact=True).first
                if await loc.is_visible():
                    logger.debug(f"[{task_id}] signup via exact text={text!r}")
                    return loc
            except Exception:
                pass

    # Strategy 4: partial text fallback
    logger.debug(f"[{task_id}] signup falling back to partial text search")
    return await find_button_by_texts(page, ["Sign up", "Create account", "注册"])


# ── Spinbutton (year / month / day) ──────────────────────────────────────

async def set_spinbutton(page: Page, locator: Locator, target: int) -> None:
    """
    Adjust a spinbutton (role='spinbutton') to reach *target* by pressing
    ArrowUp / ArrowDown keys, mirroring the original JS _0x1d1 logic.
    """
    await locator.click()
    await asyncio.sleep(0.2)

    # Read current value
    current_str: str = await locator.evaluate(
        "el => el.getAttribute('aria-valuenow') || el.textContent || '0'"
    )
    try:
        current = int("".join(c for c in current_str if c.isdigit() or c == "-"))
    except ValueError:
        current = 0

    diff = target - current
    key  = "ArrowUp" if diff > 0 else "ArrowDown"
    for _ in range(abs(diff)):
        await locator.press(key)
        await asyncio.sleep(0.04)

    # Confirm the value
    await locator.press("Tab")


# ── Error page detection ──────────────────────────────────────────────────

_ERROR_PHRASES = [
    "糟糕", "出错了", "Operation timed out", "操作超时",
    "Something went wrong", "error occurred",
    "Access denied", "403 Forbidden",
]


async def is_error_page(page: Page) -> bool:
    try:
        text = await page.evaluate("() => document.body?.innerText || ''")
        return any(phrase.lower() in text.lower() for phrase in _ERROR_PHRASES)
    except Exception:
        return False


# ── Visibility check ──────────────────────────────────────────────────────

async def is_visible(page: Page, selector: str) -> bool:
    try:
        return await page.locator(selector).first.is_visible()
    except Exception:
        return False


# ── Human-like interaction ────────────────────────────────────────────────

async def jitter_sleep(base: float, jitter: float = 0.3) -> None:
    """Sleep for base ± jitter seconds to mimic human reaction time."""
    await asyncio.sleep(base + random.uniform(-jitter, jitter))


async def human_move_and_click(page: Page, locator: Locator) -> None:
    """
    Move the mouse to a locator via a curved path with random jitter,
    then click — mimicking human cursor behavior that bot-detection looks for.

    Auth0 / Cloudflare track mouse history before a click; a direct
    playwright .click() with no prior movement is a strong bot signal.
    """
    try:
        box = await locator.bounding_box()
        if not box:
            await locator.click()
            return

        # Random landing point within middle 60% of element
        target_x = box["x"] + box["width"]  * random.uniform(0.2, 0.8)
        target_y = box["y"] + box["height"] * random.uniform(0.2, 0.8)

        # Start from a plausible "previous" cursor position
        start_x = random.randint(200, 900)
        start_y = random.randint(150, 600)

        # Move in N micro-steps with ease-in-out + slight per-step noise
        steps = random.randint(8, 16)
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3.0 - 2.0 * t)          # smoothstep easing
            x = start_x + (target_x - start_x) * t + random.uniform(-2, 2)
            y = start_y + (target_y - start_y) * t + random.uniform(-2, 2)
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.008, 0.025))

        # Brief human "hover" pause before pressing
        await asyncio.sleep(random.uniform(0.05, 0.18))
        await page.mouse.click(target_x, target_y)

    except Exception as exc:
        logger.debug(f"[helpers] human_move_and_click fallback: {exc}")
        await locator.click()


