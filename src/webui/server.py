"""
webui/server.py — FastAPI backend for the ChatGPT register WebUI.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger

import src.db as db_mod
import src.accounts as accounts_mod
import src.proxy_pool as proxy_pool_mod
import src.settings_db as settings_db
import src.upload as upload_mod
from src.mail import get_mail_client
from src.mail.imap import IMAPMailClient
from src.mail.outlook import OutlookMailClient
from src.browser.register import register_one

STATIC_DIR = Path(__file__).parent / "static"


# ── Lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db_mod.init()
    await settings_db.init()
    yield


app = FastAPI(title="ChatGPT Register WebUI", docs_url=None, redoc_url=None, lifespan=lifespan)


# ── Job registry ──────────────────────────────────────────────────────────

class _Job:
    def __init__(self, job_id: str, count: int, provider: str, engine: str, proxy_mode: str):
        self.id         = job_id
        self.count      = count
        self.provider   = provider
        self.engine     = engine
        self.proxy_mode = proxy_mode
        self.status     = "running"
        self.logs: list[str] = []
        self.results: list[dict] = []
        self.started    = time.time()
        self.task: Optional[asyncio.Task] = None

    def log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.logs.append(f"[{ts}] {msg}")

    def to_dict(self, full: bool = False) -> dict:
        d: dict[str, Any] = {
            "id":        self.id,
            "count":     self.count,
            "provider":  self.provider,
            "engine":    self.engine,
            "status":    self.status,
            "started":   self.started,
            "log_count": len(self.logs),
            "done":      len(self.results),
            "success":   sum(1 for r in self.results if r.get("status") == "注册完成"),
        }
        if full:
            d["logs"]    = self.logs
            d["results"] = self.results
        return d


_jobs: dict[str, _Job] = {}


# ── Background runner ─────────────────────────────────────────────────────

async def _run_job(job: _Job) -> None:
    try:
        cfg = await settings_db.build_config()
        cfg["engine"] = job.engine

        strategy     = cfg.get("proxy_strategy", "none")
        static_proxy = cfg.get("proxy_static") or None
        max_concurrent = int(cfg.get("max_concurrent", 2))
        sem = asyncio.Semaphore(max_concurrent)

        imap_raw = (cfg.get("mail") or {}).get("imap", [])
        out_raw  = (cfg.get("mail") or {}).get("outlook", [])

        # Detect new IMAP format: provider objects with "accounts" sub-list
        _is_new_imap = bool(imap_raw) and isinstance(imap_raw[0], dict) and "accounts" in imap_raw[0]
        provider_lower = job.provider.lower()
        _is_imap_provider = provider_lower.startswith("imap:") and _is_new_imap
        _is_outlook       = provider_lower.startswith("outlook")

        # Build shared client for API providers and old-format IMAP
        _shared_client = None
        if not _is_imap_provider and not _is_outlook:
            provider_base = job.provider.split(":")[0]
            mail_raw = (cfg.get("mail") or {}).get(provider_base, {})
            api_key  = "" if isinstance(mail_raw, list) else mail_raw.get("api_key", "")
            base_url = "" if isinstance(mail_raw, list) else mail_raw.get("base_url", "")
            _shared_client = get_mail_client(job.provider, api_key=api_key, base_url=base_url, cfg=cfg)

        def _get_mail_client(n: int, proxy: Optional[str] = None):
            if _is_imap_provider:
                _parts = job.provider.split(":")
                provider_idx = int(_parts[1])
                if provider_idx >= len(imap_raw):
                    raise ValueError(f"IMAP 服务商索引 {provider_idx} 不存在（共 {len(imap_raw)} 个）")
                prov     = imap_raw[provider_idx]
                accounts = prov.get("accounts", [])
                if not accounts:
                    raise ValueError(f"IMAP 服务商 {provider_idx} ({prov.get('name','?')}) 没有配置账户")

                # imap:N:M → fixed account M within provider N; imap:N → rotate
                if len(_parts) >= 3 and _parts[2].isdigit():
                    acc_idx = int(_parts[2])
                    if acc_idx >= len(accounts):
                        raise ValueError(
                            f"IMAP 服务商 {provider_idx} 账户索引 {acc_idx} 不存在"
                            f"（共 {len(accounts)} 个账户）"
                        )
                    acc = accounts[acc_idx]
                else:
                    acc = accounts[(n - 1) % len(accounts)]

                auth_type = prov.get("auth_type", "password")
                cred      = acc.get("credential", "")
                return IMAPMailClient(
                    email        = acc.get("email", ""),
                    password     = cred if auth_type == "password" else "",
                    host         = prov.get("host", ""),
                    port         = int(prov.get("port", 993)),
                    ssl          = bool(prov.get("ssl", True)),
                    folder       = prov.get("folder", "INBOX"),
                    use_alias    = prov.get("use_alias"),
                    auth_type    = auth_type,
                    access_token = cred if auth_type == "oauth2" else "",
                )
            elif _is_outlook:
                if not out_raw:
                    raise ValueError("没有配置 Outlook 账户")

                # outlook:N → fixed account N; outlook → rotate through all
                _parts = job.provider.lower().split(":")
                if len(_parts) >= 2 and _parts[1].isdigit():
                    out_idx = int(_parts[1])
                    if out_idx >= len(out_raw):
                        raise ValueError(
                            f"Outlook 账户索引 {out_idx} 不存在（共 {len(out_raw)} 个）"
                        )
                    acc = out_raw[out_idx]
                else:
                    acc = out_raw[(n - 1) % len(out_raw)]

                return OutlookMailClient(
                    email         = acc.get("email", ""),
                    client_id     = acc.get("client_id", ""),
                    tenant_id     = acc.get("tenant_id", "consumers"),
                    refresh_token = acc.get("refresh_token", ""),
                    access_token  = acc.get("access_token", ""),
                    fetch_method  = acc.get("fetch_method", "graph"),
                    # Account-level proxy takes priority; fall back to job proxy.
                    # In mainland China, Microsoft API endpoints require a proxy.
                    proxy         = acc.get("proxy") or proxy,
                )
            else:
                return _shared_client

        job.log(f"Starting {job.count} task(s) — engine={job.engine} provider={job.provider}")

        async def _one(n: int) -> None:
            async with sem:
                if job.status == "cancelled":
                    return

                proxy: Optional[str] = None
                if strategy == "static" and static_proxy:
                    proxy = static_proxy
                elif strategy == "pool":
                    proxy = await proxy_pool_mod.acquire()

                try:
                    mail_client = _get_mail_client(n, proxy)
                except Exception as exc:
                    job.log(f"Task {n}/{job.count} 邮件客户端错误: {exc}")
                    return

                job.log(f"Task {n}/{job.count} 启动  proxy={'yes' if proxy else 'none'}")
                try:
                    result = await register_one(
                        task_id   = f"{job.id}-{n}",
                        cfg       = cfg,
                        mail_client = mail_client,
                        proxy     = proxy,
                        log_fn    = lambda msg, _n=n: job.log(f"[任务{_n}] {msg}"),
                    )
                    await accounts_mod.upsert(result)
                    job.results.append(result)
                    st = result.get("status", "?")
                    job.log(f"Task {n}/{job.count} → {result.get('email', '?')} [{st}]")
                    if strategy == "pool" and proxy:
                        await proxy_pool_mod.report_result(proxy, st == "注册完成")
                except asyncio.CancelledError:
                    job.log(f"Task {n}/{job.count} 已取消")
                    raise
                except Exception as exc:
                    job.log(f"Task {n}/{job.count} 错误: {exc}")

        await asyncio.gather(
            *[asyncio.create_task(_one(i + 1)) for i in range(job.count)],
            return_exceptions=True,
        )
        if job.status != "cancelled":
            job.status = "done"
        d = job.to_dict()
        job.log(f"全部完成 — {d['success']}/{job.count} 成功")
    except asyncio.CancelledError:
        job.status = "cancelled"
        job.log("任务已被用户取消")
    except Exception as exc:
        job.status = "error"
        job.log(f"Fatal: {exc}")
        logger.exception(f"[webui] Job {job.id} fatal")


# ── Config API (DB-backed, general section) ───────────────────────────────

@app.get("/api/config")
async def api_get_config():
    """Return the DB general section (engine, headless, proxy, etc.)."""
    return await settings_db.get_section("general")


@app.post("/api/config")
async def api_set_config(request: Request):
    """Merge-update the DB general section."""
    body: dict = await request.json()
    existing = await settings_db.get_section("general")
    existing.update(body)
    await settings_db.set_section("general", existing)
    return {"ok": True}


# ── Settings API (DB-backed, non-common settings) ─────────────────────────

@app.get("/api/settings")
async def api_get_settings():
    return await settings_db.get_all()


@app.get("/api/settings/{section:path}")
async def api_get_settings_section(section: str):
    return await settings_db.get_section(section)


@app.post("/api/settings/{section:path}")
async def api_set_settings_section(section: str, request: Request):
    value = await request.json()
    await settings_db.set_section(section, value)
    return {"ok": True}


@app.get("/api/settings_merged")
async def api_settings_merged():
    """Return the fully merged SQLite-backed runtime config."""
    return await settings_db.build_config()


# ── Mail import helpers ───────────────────────────────────────────────────

def _parse_imap_text(text: str) -> list[dict]:
    """
    Parse bulk IMAP account text into account dicts.

    Supported formats (one account per non-blank, non-comment line):
      email<TAB>password[<TAB>host[<TAB>port[<TAB>ssl]]]
      email----password[----host[----port[----ssl]]]
      JSON array: [{email, password, host, ...}]
    """
    import json
    stripped = text.strip()
    if stripped.startswith("["):
        raw = json.loads(stripped)
        if not isinstance(raw, list):
            raise ValueError("JSON must be an array")
        return raw

    results = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Try "----" separator first, then tab, then whitespace
        if "----" in line:
            parts = [p.strip() for p in line.split("----")]
        elif "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        else:
            parts = line.split(None, 4)

        if len(parts) < 2:
            continue

        email    = parts[0]
        password = parts[1]
        host     = parts[2] if len(parts) > 2 else ""
        port_s   = parts[3] if len(parts) > 3 else "993"
        ssl_s    = parts[4] if len(parts) > 4 else "true"
        try:
            port = int(port_s)
        except ValueError:
            port = 993
        ssl = ssl_s.lower() not in ("false", "0", "no")

        acc: dict = {"email": email, "password": password, "port": port, "ssl": ssl,
                     "folder": "INBOX", "auth_type": "password", "access_token": ""}
        if host:
            acc["host"] = host
        results.append(acc)
    return results


def _parse_outlook_text(text: str) -> list[dict]:
    """
    Parse bulk Outlook account text into account dicts.

    Supported formats:
      JSON array: [{email, client_id, tenant_id, refresh_token, fetch_method}]
      四短线分隔 (one per line): email----password----client_id----refresh_token[----fetch_method]
      Pipe-separated (one per line): email|client_id|tenant_id|refresh_token[|fetch_method]
    """
    import json
    stripped = text.strip()
    if stripped.startswith("["):
        raw = json.loads(stripped)
        if not isinstance(raw, list):
            raise ValueError("JSON must be an array")
        return raw

    results = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if "----" in line:
            # Format: email----password----client_id----refresh_token[----fetch_method]
            parts = [p.strip() for p in line.split("----")]
            if len(parts) < 4:
                continue
            results.append({
                "email":         parts[0],
                "password":      parts[1],
                "client_id":     parts[2],
                "tenant_id":     "consumers",
                "refresh_token": parts[3],
                "access_token":  "",
                "fetch_method":  parts[4] if len(parts) > 4 else "graph",
            })
        else:
            # Pipe-separated: email|client_id|tenant_id|refresh_token[|fetch_method]
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 4:
                continue
            results.append({
                "email":         parts[0],
                "password":      "",
                "client_id":     parts[1],
                "tenant_id":     parts[2] or "consumers",
                "refresh_token": parts[3],
                "access_token":  "",
                "fetch_method":  parts[4] if len(parts) > 4 else "graph",
            })
    return results


@app.post("/api/mail/import/imap")
async def api_import_imap(request: Request):
    """Parse and append bulk IMAP accounts. Returns parsed list for preview."""
    body = await request.json()
    text = body.get("text", "")
    try:
        parsed = _parse_imap_text(text)
    except Exception as e:
        raise HTTPException(400, f"Parse error: {e}")
    return {"parsed": parsed, "count": len(parsed)}


@app.post("/api/mail/import/imap/accounts")
async def api_parse_imap_accounts(request: Request):
    """
    Parse simple email+credential text for the new provider-based IMAP format.
    Returns [{email, credential}] pairs.
    """
    body = await request.json()
    text = body.get("text", "").strip()
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "----" in line:
            parts = [p.strip() for p in line.split("----", 1)]
        elif "\t" in line:
            parts = line.split("\t", 1)
        else:
            parts = line.split(None, 1)
        if len(parts) >= 2:
            results.append({"email": parts[0].strip(), "credential": parts[1].strip()})
        elif len(parts) == 1 and "@" in parts[0]:
            results.append({"email": parts[0].strip(), "credential": ""})
    return {"parsed": results, "count": len(results)}


@app.post("/api/mail/import/imap/save")
async def api_import_imap_save(request: Request):
    """Append parsed IMAP accounts to the DB section."""
    body    = await request.json()
    new_acc = body.get("accounts", [])
    existing = await settings_db.get_section("mail.imap")
    if not isinstance(existing, list):
        existing = []
    # Deduplicate by email
    existing_emails = {a.get("email", "").lower() for a in existing}
    added = [a for a in new_acc if a.get("email", "").lower() not in existing_emails]
    await settings_db.set_section("mail.imap", existing + added)
    return {"added": len(added), "total": len(existing) + len(added)}


@app.post("/api/mail/import/outlook")
async def api_import_outlook(request: Request):
    """Parse bulk Outlook accounts. Returns parsed list for preview."""
    body = await request.json()
    text = body.get("text", "")
    try:
        parsed = _parse_outlook_text(text)
    except Exception as e:
        raise HTTPException(400, f"Parse error: {e}")
    return {"parsed": parsed, "count": len(parsed)}


@app.post("/api/mail/import/outlook/save")
async def api_import_outlook_save(request: Request):
    """Append parsed Outlook accounts to the DB section."""
    body     = await request.json()
    new_acc  = body.get("accounts", [])
    existing = await settings_db.get_section("mail.outlook")
    if not isinstance(existing, list):
        existing = []
    existing_emails = {a.get("email", "").lower() for a in existing}
    added = [a for a in new_acc if a.get("email", "").lower() not in existing_emails]
    await settings_db.set_section("mail.outlook", existing + added)
    return {"added": len(added), "total": len(existing) + len(added)}


# ── Accounts API ──────────────────────────────────────────────────────────

@app.get("/api/accounts")
async def api_accounts(status: str = "", limit: int = 200, offset: int = 0):
    rows = await accounts_mod.list_all(status or None)
    return {"total": len(rows), "items": rows[offset: offset + limit]}


@app.delete("/api/accounts/{email:path}")
async def api_delete_account(email: str):
    """Delete a single account by email."""
    await accounts_mod.delete(urllib.parse.unquote(email))
    return {"ok": True}


@app.post("/api/accounts/batch-delete")
async def api_batch_delete_accounts(request: Request):
    """Batch delete accounts. Pass {emails:[...]} or {select_all:true, status:'...'}."""
    body = await request.json()
    emails: list = body.get("emails", [])
    select_all: bool = body.get("select_all", False)
    status_filter: str = body.get("status", "")
    if select_all:
        rows = await accounts_mod.list_all(status_filter or None)
        emails = [r["email"] for r in rows]
    for email in emails:
        await accounts_mod.delete(email)
    return {"deleted": len(emails)}


@app.get("/api/accounts/stats")
async def api_account_stats():
    rows = await accounts_mod.list_all()
    counts: dict[str, int] = {}
    for r in rows:
        s = r.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
    counts["total"] = len(rows)
    return counts


@app.get("/api/accounts/export")
async def api_export(fmt: str = "json"):
    rows = await accounts_mod.list_all()
    if fmt == "csv":
        import io, csv
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=accounts.csv"},
        )
    return Response(
        content=json.dumps(rows, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=accounts.json"},
    )


@app.post("/api/accounts/export-selected")
async def api_export_selected(request: Request):
    """Export a filtered subset of accounts as CSV or JSON (blob download via fetch)."""
    import io, csv as csv_mod
    body = await request.json()
    emails: list = body.get("emails", [])
    select_all: bool = body.get("select_all", False)
    status_filter: str = body.get("status", "")
    fmt: str = body.get("fmt", "json")

    if select_all:
        rows = await accounts_mod.list_all(status_filter or None)
    else:
        all_rows = await accounts_mod.list_all()
        email_set = set(emails)
        rows = [r for r in all_rows if r.get("email") in email_set]

    # Strip internal _raw key
    clean = [{k: v for k, v in r.items() if k != "_raw"} for r in rows]

    if fmt == "csv":
        fieldnames = [
            "email", "password", "status", "first_name", "last_name",
            "provider", "proxy", "created_at", "account_id",
            "access_token", "refresh_token",
        ]
        buf = io.StringIO()
        w = csv_mod.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in fieldnames} for r in clean)
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=accounts_selected.csv"},
        )

    return Response(
        content=json.dumps(clean, ensure_ascii=False, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=accounts_selected.json"},
    )


# ── Upload API ────────────────────────────────────────────────────────────────

@app.post("/api/accounts/upload/newapi")
async def api_upload_newapi(request: Request):
    """Batch upload accounts to NewAPI channel manager."""
    body = await request.json()
    api_url = (body.get("api_url") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    if not api_url:
        raise HTTPException(400, "api_url is required")
    if not api_key:
        raise HTTPException(400, "api_key is required")
    return await upload_mod.batch_upload_newapi(
        emails=body.get("emails", []),
        api_url=api_url,
        api_key=api_key,
        channel_type=int(body.get("channel_type") or 1),
        channel_base_url=body.get("channel_base_url") or "",
        channel_models=body.get("channel_models") or "",
        select_all=bool(body.get("select_all", False)),
        status_filter=body.get("status") or "",
    )


@app.post("/api/accounts/upload/cpa")
async def api_upload_cpa(request: Request):
    """Batch upload accounts to CPA (Codex Protocol API)."""
    body = await request.json()
    api_url = (body.get("api_url") or "").strip()
    api_token = (body.get("api_token") or "").strip()
    if not api_url:
        raise HTTPException(400, "api_url is required")
    if not api_token:
        raise HTTPException(400, "api_token is required")
    return await upload_mod.batch_upload_cpa(
        emails=body.get("emails", []),
        api_url=api_url,
        api_token=api_token,
        select_all=bool(body.get("select_all", False)),
        status_filter=body.get("status") or "",
    )


@app.post("/api/accounts/upload/sub2api")
async def api_upload_sub2api(request: Request):
    """Batch upload accounts to Sub2API platform."""
    body = await request.json()
    api_url = (body.get("api_url") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    if not api_url:
        raise HTTPException(400, "api_url is required")
    if not api_key:
        raise HTTPException(400, "api_key is required")
    return await upload_mod.batch_upload_sub2api(
        emails=body.get("emails", []),
        api_url=api_url,
        api_key=api_key,
        concurrency=int(body.get("concurrency") or 3),
        priority=int(body.get("priority") or 50),
        select_all=bool(body.get("select_all", False)),
        status_filter=body.get("status") or "",
    )


@app.post("/api/accounts/upload/test")
async def api_upload_test_connection(request: Request):
    """Test connectivity to an upload platform."""
    body = await request.json()
    platform = body.get("platform", "newapi")
    api_url = (body.get("api_url") or "").strip()

    if platform == "newapi":
        api_key = (body.get("api_key") or "").strip()
        ok, msg = await upload_mod.test_newapi_connection(api_url, api_key)
    elif platform == "cpa":
        api_token = (body.get("api_token") or "").strip()
        ok, msg = await upload_mod.test_cpa_connection(api_url, api_token)
    elif platform == "sub2api":
        api_key = (body.get("api_key") or "").strip()
        ok, msg = await upload_mod.test_sub2api_connection(api_url, api_key)
    else:
        raise HTTPException(400, f"Unknown platform: {platform}")

    return {"ok": ok, "message": msg}


@app.post("/api/accounts/upload/batch")
async def api_upload_batch(request: Request):
    """Upload accounts to multiple configured endpoint targets in parallel."""
    body = await request.json()
    emails: list = body.get("emails", [])
    select_all: bool = bool(body.get("select_all", False))
    status_filter: str = body.get("status", "")
    targets: list = body.get("targets", [])  # [{platform, index}, ...]

    async def _run_target(target: dict) -> dict:
        platform = target.get("platform", "")
        index = int(target.get("index", 0))
        base = {"platform": platform, "index": index,
                "success_count": 0, "failed_count": 0, "skipped_count": 0, "details": []}
        try:
            configs = await settings_db.get_section(f"upload.{platform}")
            if not isinstance(configs, list) or index >= len(configs):
                return {**base, "name": f"{platform}#{index+1}", "error": "配置不存在"}
            cfg = configs[index]
            name = cfg.get("name") or f"{platform} #{index+1}"
            if platform == "newapi":
                r = await upload_mod.batch_upload_newapi(
                    emails=emails, api_url=cfg.get("api_url", ""),
                    api_key=cfg.get("api_key", ""),
                    channel_type=int(cfg.get("channel_type") or 1),
                    channel_base_url=cfg.get("channel_base_url", ""),
                    channel_models=cfg.get("channel_models", ""),
                    select_all=select_all, status_filter=status_filter,
                )
            elif platform == "cpa":
                r = await upload_mod.batch_upload_cpa(
                    emails=emails, api_url=cfg.get("api_url", ""),
                    api_token=cfg.get("api_token", ""),
                    select_all=select_all, status_filter=status_filter,
                )
            elif platform == "sub2api":
                r = await upload_mod.batch_upload_sub2api(
                    emails=emails, api_url=cfg.get("api_url", ""),
                    api_key=cfg.get("api_key", ""),
                    concurrency=int(cfg.get("concurrency") or 3),
                    priority=int(cfg.get("priority") or 50),
                    select_all=select_all, status_filter=status_filter,
                )
            else:
                return {**base, "name": platform, "error": f"未知平台: {platform}"}
            return {"platform": platform, "index": index, "name": name, **r}
        except Exception as e:
            return {**base, "name": f"{platform}#{index+1}", "error": str(e)}

    results = await asyncio.gather(*[_run_target(t) for t in targets])
    return {"targets": list(results)}


# ── Jobs API ──────────────────────────────────────────────────────────────

@app.post("/api/jobs")
async def api_start_job(request: Request):
    body: dict = await request.json()
    cfg = await settings_db.build_config()
    count    = int(body.get("count", 1))
    provider = body.get("provider") or cfg.get("mail_provider", "gptmail")
    engine   = body.get("engine")   or cfg.get("engine", "playwright")

    job_id = str(uuid.uuid4())[:8]
    job = _Job(job_id, count, provider, engine, cfg.get("proxy_strategy", "none"))
    _jobs[job_id] = job
    job.task = asyncio.create_task(_run_job(job))
    return {"job_id": job_id}


@app.get("/api/jobs")
async def api_list_jobs():
    return [j.to_dict() for j in reversed(list(_jobs.values()))]


@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job.to_dict(full=True)


@app.delete("/api/jobs/{job_id}")
async def api_delete_job(job_id: str):
    job = _jobs.pop(job_id, None)
    if job and job.task and not job.task.done():
        job.task.cancel()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel_job(job_id: str):
    """Cancel a running job without removing it from the list."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.status = "cancelled"
    if job.task and not job.task.done():
        job.task.cancel()
    job.log("⛔ 用户取消了任务")
    return {"ok": True}


@app.post("/api/jobs/batch-action")
async def api_batch_jobs_action(request: Request):
    """Batch cancel or delete jobs. Pass {action:'cancel'|'delete', ids:[...], select_all:bool}."""
    body = await request.json()
    action: str = body.get("action", "delete")
    ids: list = body.get("ids", [])
    select_all: bool = body.get("select_all", False)
    if select_all:
        ids = list(_jobs.keys())
    count = 0
    for job_id in ids:
        if action == "cancel":
            job = _jobs.get(job_id)
            if job and job.status == "running":
                job.status = "cancelled"
                if job.task and not job.task.done():
                    job.task.cancel()
                job.log("⛔ 用户批量取消")
                count += 1
        else:  # delete
            job = _jobs.pop(job_id, None)
            if job:
                if job.task and not job.task.done():
                    job.task.cancel()
                count += 1
    return {"affected": count}


# ── Proxies API ───────────────────────────────────────────────────────────

@app.get("/api/proxies")
async def api_proxies():
    return await proxy_pool_mod.list_all()


@app.post("/api/proxies")
async def api_add_proxy(request: Request):
    body: dict = await request.json()
    addr = body.get("address", "").strip()
    if not addr:
        raise HTTPException(400, "address required")
    await proxy_pool_mod.add(addr)
    return {"ok": True}


@app.delete("/api/proxies/{address:path}")
async def api_delete_proxy(address: str):
    await proxy_pool_mod.remove(urllib.parse.unquote(address))
    return {"ok": True}


@app.post("/api/proxies/batch-delete")
async def api_batch_delete_proxies(request: Request):
    """Batch delete proxies. Pass {addresses:[...]} or {select_all:true}."""
    body = await request.json()
    addresses: list = body.get("addresses", [])
    select_all: bool = body.get("select_all", False)
    if select_all:
        all_proxies = await proxy_pool_mod.list_all()
        addresses = [p["address"] for p in all_proxies]
    for addr in addresses:
        await proxy_pool_mod.remove(addr)
    return {"deleted": len(addresses)}


# ── SPA ───────────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/{path:path}")
async def serve_spa(path: str):
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return HTMLResponse(
            "<h1>WebUI not built yet.</h1><p>Run: <code>cd webui_frontend &amp;&amp; npm install &amp;&amp; npm run build</code></p>",
            status_code=503,
        )
    html = index.read_text(encoding="utf-8")
    return HTMLResponse(html)


# ── Start ─────────────────────────────────────────────────────────────────

def run(host: str = "0.0.0.0", port: int = 7860) -> None:
    uvicorn.run("src.webui.server:app", host=host, port=port, log_level="warning")
