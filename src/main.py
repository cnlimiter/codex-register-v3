"""
main.py — Typer CLI + asyncio concurrent registration runner.

Commands
--------
register   --count N  --engine playwright|camoufox  --provider gptmail|npcmail|yydsmail
           [--headed]  [--slow-mo N]
list       [--status filter]
export     --format json|csv  --output path
import-accounts  file
import-proxies   file
config     set KEY VALUE
db         init
"""
from __future__ import annotations

import asyncio
import json
import sys
import warnings
from pathlib import Path
from typing import Any, Optional

# ── Windows ProactorEventLoop ResourceWarning suppression ─────────────────
# On Windows, when asyncio.run() closes the event loop, pending pipe transports
# are GC-ed with already-closed fds, triggering a spurious
#   "Exception ignored … ValueError: I/O operation on closed pipe"
# This is a CPython bug (tracked in bpo-23309 / gh-103472) that does NOT
# affect correctness.  Filter it out here so it doesn't pollute the log.
if sys.platform == "win32":
    # Suppress spurious Windows ProactorEventLoop shutdown noise.
    # Neither of these affects correctness — they fire during GC after the
    # event loop is already closed (CPython bpo-23309 / gh-103472).
    warnings.filterwarnings(
        "ignore",
        message="unclosed transport",
        category=ResourceWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message="Task was destroyed but it is pending",
        category=RuntimeWarning,
    )

import typer
from loguru import logger
from rich.console import Console
from rich.table import Table

import src.db as db_mod
import src.accounts as accounts_mod
import src.proxy_pool as proxy_pool_mod
import src.settings_db as settings_db
from src.mail import get_mail_client
from src.browser.register import register_one

app    = typer.Typer(help="ChatGPT headless auto-registration bot", add_completion=False)
console = Console()

# ── Logging setup ─────────────────────────────────────────────────────────

logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    level="INFO",
    colorize=True,
)
logger.add(
    "register.log",
    rotation="10 MB",
    retention="7 days",
    level="DEBUG",
    encoding="utf-8",
)


# ── Helpers ───────────────────────────────────────────────────────────────

def _ensure_db() -> None:
    _run(db_mod.init())


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


def _coerce_value(value: str) -> Any:
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


def _resolve_config_target(key: str) -> tuple[str, list[str]]:
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


def _nested_get(data: Any, key: str) -> Any:
    cur = data
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise KeyError(key)
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


# ── Commands ──────────────────────────────────────────────────────────────

@app.command()
def register(
    count: int = typer.Option(1,  "--count",    "-n", help="Number of accounts to register"),
    engine: str = typer.Option("", "--engine",  "-e", help="Browser engine: playwright | camoufox"),
    provider: str = typer.Option("", "--provider", "-p", help="Mail provider: gptmail | npcmail | yydsmail | imap"),
    concurrency: int = typer.Option(0, "--concurrency", "-c", help="Max parallel browsers (0 = use config)"),
    proxy: str = typer.Option("", "--proxy", "-x", help="Static proxy URL to use for this run, e.g. http://user:pass@host:port"),
    headed: bool = typer.Option(False, "--headed/--headless", help="Run with a visible browser window (headed mode)"),
    slow_mo: int = typer.Option(-1, "--slow-mo", help="Extra ms delay between actions in headed mode (-1 = auto: 80 ms)"),
) -> None:
    """Register N ChatGPT accounts concurrently."""
    _ensure_db()
    _run(_register_async(count, engine, provider, concurrency, proxy, headed, slow_mo))


@app.command()
def list_accounts(
    status: str = typer.Option("", "--status", "-s", help="Filter by status substring"),
) -> None:
    """List all stored accounts."""
    _ensure_db()
    rows = _run(accounts_mod.list_all(status or None))
    if not rows:
        console.print("[yellow]No accounts found.[/yellow]")
        return

    tbl = Table(title=f"Accounts ({len(rows)})", show_lines=False)
    tbl.add_column("Email",      style="cyan",  no_wrap=True)
    tbl.add_column("Password",   style="white")
    tbl.add_column("Status",     style="green")
    tbl.add_column("Provider",   style="magenta")
    tbl.add_column("Created",    style="dim")

    for r in rows:
        tbl.add_row(
            r.get("email", ""),
            r.get("password", ""),
            r.get("status", ""),
            r.get("provider", ""),
            r.get("created_at", "")[:19],
        )
    console.print(tbl)


@app.command()
def export(
    fmt: str  = typer.Option("json", "--format", "-f", help="Output format: json | csv"),
    output: str = typer.Option("", "--output",  "-o", help="Output file path"),
) -> None:
    """Export accounts to JSON or CSV."""
    _ensure_db()
    out_path = Path(output) if output else Path(f"accounts_export.{fmt}")
    if fmt == "csv":
        n = _run(accounts_mod.export_csv(out_path))
    else:
        n = _run(accounts_mod.export_json(out_path))
    console.print(f"[green]Exported {n} accounts → {out_path}[/green]")


@app.command("import-accounts")
def import_accounts(
    file: Path = typer.Argument(..., help="JSON / CSV / TXT file to import"),
) -> None:
    """Import accounts from a file (JSON array, CSV, or email:password lines)."""
    _ensure_db()
    suffix = file.suffix.lower()
    if suffix == ".json":
        added, skipped = _run(accounts_mod.import_json(file))
    else:
        added, skipped = _run(accounts_mod.import_text(file))
    console.print(f"[green]Imported {added} accounts[/green] (skipped {skipped})")


@app.command("import-proxies")
def import_proxies(
    file: Path = typer.Argument(Path("proxies.txt"), help="Proxy list file"),
) -> None:
    """Load proxies from a text file into the database."""
    _ensure_db()
    n = _run(proxy_pool_mod.load_from_file(file))
    console.print(f"[green]Loaded {n} proxies from {file}[/green]")
    count = _run(proxy_pool_mod.active_count())
    console.print(f"Active proxies in pool: {count}")


@app.command("config")
def config_cmd(
    action: str = typer.Argument(..., help="Action: set | get | show"),
    key: str    = typer.Argument("",  help="Dot-notation key, e.g. engine"),
    value: str  = typer.Argument("",  help="Value to set"),
) -> None:
    """Get or set SQLite-backed configuration values."""
    _ensure_db()
    if action == "set":
        try:
            section, parts = _resolve_config_target(key)
        except KeyError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)

        coerced = _coerce_value(value)
        if parts:
            current = _run(settings_db.get_section(section))
            if not isinstance(current, dict):
                console.print(f"[red]Section {section!r} is not a dict; set the whole section instead.[/red]")
                raise typer.Exit(1)
            updated = _nested_set(dict(current), parts, coerced)
        else:
            updated = coerced

        _run(settings_db.set_section(section, updated))
        console.print(f"[green]Set {key} = {coerced!r}[/green]")
    elif action == "get":
        cfg = _run(settings_db.build_config())
        try:
            console.print(_nested_get(cfg, key))
        except KeyError:
            console.print(f"[red]Unknown config key: {key!r}[/red]")
            raise typer.Exit(1)
    elif action == "show":
        console.print_json(json.dumps(_run(settings_db.build_config()), indent=2, ensure_ascii=False))
    else:
        console.print(f"[red]Unknown action {action!r}. Use: set | get | show[/red]")


@app.command("db")
def db_cmd(action: str = typer.Argument("init", help="Action: init")) -> None:
    """Database management commands."""
    if action == "init":
        _run(db_mod.init())
        console.print(f"[green]DB ready at {db_mod.DB_PATH}[/green]")
    else:
        console.print(f"[red]Unknown action: {action}[/red]")


@app.command("webui")
def webui_cmd(
    host: str = typer.Option("0.0.0.0", "--host", "-H", help="Bind host"),
    port: int = typer.Option(7860, "--port",  "-p", help="Bind port"),
) -> None:
    """Start the WebUI server (React + FastAPI)."""
    from src.webui.server import run
    console.print(f"[bold green]WebUI starting → http://{'localhost' if host == '0.0.0.0' else host}:{port}[/bold green]")
    run(host=host, port=port)


# ── Concurrent runner ─────────────────────────────────────────────────────

async def _register_async(
    count: int,
    engine_override: str,
    provider_override: str,
    concurrency_override: int,
    proxy_override: str = "",
    headed_override: bool = False,
    slow_mo_override: int = -1,
) -> None:
    cfg = await settings_db.build_config()

    engine   = engine_override   or cfg.get("engine",        "playwright")
    provider = provider_override or cfg.get("mail_provider", "gptmail")
    max_concurrent = int(concurrency_override or cfg.get("max_concurrent", 3))

    # ── Headed / headless resolution ────────────────────────────────────
    # CLI --headed flag overrides the config file value
    headless: bool = not headed_override if headed_override else cfg.get("headless", True)
    # slow_mo: -1 means "auto" (80 ms in headed, 0 in headless)
    if slow_mo_override >= 0:
        slow_mo = slow_mo_override
    else:
        slow_mo = cfg.get("slow_mo", 0)
    if not headless and slow_mo == 0:
        slow_mo = 80  # default human-pacing in headed mode

    # Merge resolved settings into cfg for register_one
    cfg["engine"]   = engine
    cfg["headless"] = headless
    cfg["slow_mo"]  = slow_mo

    # Strip optional ':index' suffix (e.g. "imap:0" → "imap") for config-dict lookup.
    provider_base = provider.split(":")[0]
    mail_cfg = (cfg.get("mail") or {}).get(provider_base, {})
    # IMAP config is a list of account dicts — it has no api_key/base_url.
    if isinstance(mail_cfg, list):
        api_key  = ""
        base_url = ""
    else:
        api_key  = mail_cfg.get("api_key", "")
        base_url = mail_cfg.get("base_url", "")

    try:
        mail_client = get_mail_client(provider, api_key=api_key, base_url=base_url, cfg=cfg)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    # ── Proxy strategy resolution ───────────────────────────────────────
    # Priority: --proxy CLI flag > proxy_strategy=static > proxy_strategy=pool > none
    strategy = cfg.get("proxy_strategy", "none")
    static_proxy: Optional[str] = (
        proxy_override                       # --proxy flag takes top priority
        or (cfg.get("proxy_static", "") if strategy == "static" else None)
    ) or None

    use_pool   = (strategy == "pool") and not static_proxy
    use_static = bool(static_proxy)

    if use_static:
        strategy_label = f"static ({static_proxy})"
    elif use_pool:
        strategy_label = "pool"
    else:
        strategy_label = "none"

    sem = asyncio.Semaphore(max_concurrent)

    mode_label = "[red]HEADED[/red]" if not headless else "headless"
    console.print(
        f"[bold cyan]Starting {count} registration(s) "
        f"[engine={engine}, mode={mode_label}, provider={provider}, "
        f"concurrency={max_concurrent}, proxy={strategy_label}, slow_mo={slow_mo}ms][/bold cyan]"
    )

    async def _one(task_id: int) -> dict:
        async with sem:
            proxy: Optional[str] = None

            if use_static:
                proxy = static_proxy
                logger.info(f"[task-{task_id}] Using static proxy: {proxy}")
            elif use_pool:
                proxy = await proxy_pool_mod.acquire()
                if proxy:
                    logger.info(f"[task-{task_id}] Using pool proxy: {proxy}")
                else:
                    logger.warning(f"[task-{task_id}] No proxy available — running without proxy")

            result = await register_one(
                task_id=f"task-{task_id}",
                cfg=cfg,
                mail_client=mail_client,
                proxy=proxy,
            )
            await accounts_mod.upsert(result)

            if use_pool and proxy:
                success = result.get("status") == "注册完成"
                await proxy_pool_mod.report_result(proxy, success)

            status_icon = "✅" if result.get("status") == "注册完成" else "❌"
            console.print(
                f"  {status_icon} [bold]{result.get('email', 'N/A')}[/bold]"
                f"  status={result.get('status')}"
            )
            return result

    tasks = [asyncio.create_task(_one(i + 1)) for i in range(count)]
    done  = await asyncio.gather(*tasks, return_exceptions=True)

    ok  = sum(1 for r in done if isinstance(r, dict) and r.get("status") == "注册完成")
    fail = count - ok
    console.print(
        f"\n[bold]Summary:[/bold] {ok} succeeded, {fail} failed out of {count} total"
    )


# ── Entry-point ───────────────────────────────────────────────────────────

def _run(coro):
    """
    Drop-in replacement for asyncio.run() with proper Windows cleanup.

    Fixes two Windows-specific noise sources that do NOT affect correctness:

    1. "Task was destroyed but it is pending!" — printed by asyncio's default
       exception handler when Playwright's internal Connection.run() task is
       still alive at loop.close() time.  Fix: cancel all pending tasks and
       let them finish before closing; also install a quiet exception handler
       that suppresses this specific message if any survive.

    2. "Exception ignored … ValueError: I/O operation on closed pipe" — fired
       from _ProactorBasePipeTransport.__del__ after the loop is already closed.
       Fix: cancelling tasks first drains most pipe handles; the warnings filter
       at module level catches any that slip through GC.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # ── Quiet exception handler ───────────────────────────────────────────
    # asyncio calls loop.call_exception_handler() (not warnings.warn) for
    # "Task was destroyed" and some transport errors — install a filter here.
    _SUPPRESS = (
        "Task was destroyed but it is pending",
        "Exception ignored in",
        "I/O operation on closed pipe",
        "pipe transport",
    )

    def _quiet_handler(loop, context):
        msg = context.get("message", "")
        if any(s in msg for s in _SUPPRESS):
            return  # swallow known-harmless Windows shutdown noise
        loop.default_exception_handler(context)

    loop.set_exception_handler(_quiet_handler)

    try:
        return loop.run_until_complete(coro)
    finally:
        # ── Step 1: cancel every pending task (e.g. playwright Connection.run()) ──
        try:
            pending = asyncio.all_tasks(loop)
            if pending:
                for t in pending:
                    t.cancel()
                # Give cancelled tasks one cycle to run their CancelledError handlers
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
        except Exception:
            pass

        # ── Step 2: drain async-generators and thread-pool executor ──────────
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
        except Exception:
            pass

        loop.close()
        asyncio.set_event_loop(None)


if __name__ == "__main__":
    app()


