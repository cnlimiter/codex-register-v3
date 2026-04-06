"""
upload.py — Batch upload accounts to third-party API management platforms.
Supports: NewAPI, CPA (Codex Protocol API), Sub2API
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import quote

import httpx
import aiosqlite

from src.db import DB_PATH

logger = logging.getLogger(__name__)

DEFAULT_NEWAPI_MODELS = (
    "gpt-5.4,gpt-5,gpt-5-codex,gpt-5-codex-mini,"
    "gpt-5.1,gpt-5.1-codex,gpt-5.1-codex-max,gpt-5.1-codex-mini,"
    "gpt-5.2,gpt-5.2-codex,gpt-5.3-codex,"
    "gpt-5-openai-compact,gpt-5-codex-openai-compact,"
    "gpt-5-codex-mini-openai-compact,gpt-5.1-openai-compact,"
    "gpt-5.1-codex-openai-compact,gpt-5.1-codex-max-openai-compact,"
    "gpt-5.1-codex-mini-openai-compact,gpt-5.2-openai-compact,"
    "gpt-5.2-codex-openai-compact,gpt-5.3-codex-openai-compact"
)


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _load_accounts_by_emails(emails: List[str]) -> List[dict]:
    if not emails:
        return []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" for _ in emails)
        cur = await db.execute(
            f"SELECT * FROM accounts WHERE email IN ({placeholders})",
            emails,
        )
        rows = await cur.fetchall()
    return [_enrich(dict(r)) for r in rows]


async def _load_accounts_by_status(status: Optional[str] = None) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            cur = await db.execute(
                "SELECT * FROM accounts WHERE status LIKE ? ORDER BY created_at DESC",
                (f"%{status}%",),
            )
        else:
            cur = await db.execute("SELECT * FROM accounts ORDER BY created_at DESC")
        rows = await cur.fetchall()
    return [_enrich(dict(r)) for r in rows]


def _enrich(d: dict) -> dict:
    """Parse raw_json into _raw field for extra token metadata."""
    raw: dict = {}
    try:
        raw = json.loads(d.get("raw_json") or "{}")
    except Exception:
        pass
    d["_raw"] = raw
    return d


# ── Shared ─────────────────────────────────────────────────────────────────────

async def _resolve_accounts(
    emails: List[str],
    select_all: bool,
    status_filter: str,
) -> List[dict]:
    if select_all:
        return await _load_accounts_by_status(status_filter or None)
    return await _load_accounts_by_emails(emails)


def _result_set() -> dict:
    return {"success_count": 0, "failed_count": 0, "skipped_count": 0, "details": []}


def _skip(results: dict, email: str, reason: str) -> None:
    results["skipped_count"] += 1
    results["details"].append({"email": email, "success": False, "error": reason})


def _ok(results: dict, email: str, msg: str = "上传成功") -> None:
    results["success_count"] += 1
    results["details"].append({"email": email, "success": True, "message": msg})


def _fail(results: dict, email: str, reason: str) -> None:
    results["failed_count"] += 1
    results["details"].append({"email": email, "success": False, "error": reason})


def _parse_http_error(resp: httpx.Response) -> str:
    try:
        detail = resp.json()
        if isinstance(detail, dict):
            return detail.get("message") or detail.get("msg") or f"HTTP {resp.status_code}"
    except Exception:
        pass
    return f"HTTP {resp.status_code}: {resp.text[:200]}"


# ── NewAPI ─────────────────────────────────────────────────────────────────────

async def _upload_one_newapi(
    acc: dict,
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    channel_type: int,
    channel_base_url: str,
    channel_models: str,
) -> Tuple[bool, str]:
    email = acc.get("email", "")
    access_token = acc.get("access_token", "")

    channel = {
        "auto_ban": 1,
        "name": email,
        "type": channel_type,
        "key": json.dumps(
            {"access_token": access_token, "account_id": email},
            ensure_ascii=True,
        ),
        "base_url": channel_base_url,
        "models": channel_models or DEFAULT_NEWAPI_MODELS,
        "multi_key_mode": "random",
        "group": "default",
        "groups": ["default"],
        "priority": 0,
        "weight": 0,
    }
    payload = json.dumps({"mode": "single", "channel": channel}, ensure_ascii=True).encode("utf-8")
    try:
        resp = await client.post(url, headers=headers, content=payload)
        if resp.status_code in (200, 201):
            return True, "上传成功"
        return False, f"上传失败: {_parse_http_error(resp)}"
    except Exception as e:
        return False, f"上传异常: {e}"


async def batch_upload_newapi(
    emails: List[str],
    api_url: str,
    api_key: str,
    channel_type: int = 1,
    channel_base_url: str = "",
    channel_models: str = "",
    select_all: bool = False,
    status_filter: str = "",
) -> dict:
    accounts = await _resolve_accounts(emails, select_all, status_filter)
    results = _result_set()

    endpoint = api_url.rstrip("/") + "/api/channel/"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "New-Api-User": "1",
        "Content-Type": "application/json; charset=utf-8",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        for acc in accounts:
            if not acc.get("access_token"):
                _skip(results, acc["email"], "缺少 access_token")
                continue
            ok, msg = await _upload_one_newapi(
                acc, client, endpoint, headers,
                channel_type or 1, channel_base_url, channel_models,
            )
            if ok:
                _ok(results, acc["email"])
            else:
                _fail(results, acc["email"], msg)

    return results


# ── CPA ────────────────────────────────────────────────────────────────────────

def _normalize_cpa_url(api_url: str) -> str:
    normalized = (api_url or "").strip().rstrip("/")
    lower = normalized.lower()
    if not normalized:
        return ""
    if lower.endswith("/auth-files"):
        return normalized
    if lower.endswith("/v0/management") or lower.endswith("/management"):
        return f"{normalized}/auth-files"
    if lower.endswith("/v0"):
        return f"{normalized}/management/auth-files"
    return f"{normalized}/v0/management/auth-files"


def _build_cpa_token_data(acc: dict) -> dict:
    raw = acc.get("_raw", {})
    expires_at = raw.get("expires_at") or raw.get("expiresAt") or ""
    if isinstance(expires_at, (int, float)):
        try:
            expires_at = datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S+08:00"
            )
        except Exception:
            expires_at = ""
    last_refresh = raw.get("last_refresh") or raw.get("lastRefresh") or ""
    return {
        "type": "codex",
        "email": acc.get("email", ""),
        "expired": expires_at,
        "id_token": raw.get("id_token") or raw.get("idToken") or "",
        "account_id": acc.get("account_id") or raw.get("account_id") or "",
        "access_token": acc.get("access_token", ""),
        "last_refresh": last_refresh,
        "refresh_token": acc.get("refresh_token") or raw.get("refresh_token") or "",
    }


async def _upload_one_cpa(
    acc: dict,
    client: httpx.AsyncClient,
    upload_url: str,
    api_token: str,
) -> Tuple[bool, str]:
    token_data = _build_cpa_token_data(acc)
    filename = f"{acc['email']}.json"
    file_content = json.dumps(token_data, ensure_ascii=False, indent=2).encode("utf-8")
    auth_headers = {"Authorization": f"Bearer {api_token}"}

    try:
        resp = await client.post(
            upload_url,
            headers=auth_headers,
            files={"file": (filename, file_content, "application/json")},
        )
        if resp.status_code in (200, 201):
            return True, "上传成功"
        # Fallback: raw JSON body upload
        if resp.status_code in (404, 405, 415):
            fallback_url = f"{upload_url}?name={quote(filename)}"
            resp2 = await client.post(
                fallback_url,
                headers={**auth_headers, "Content-Type": "application/json"},
                content=file_content,
            )
            if resp2.status_code in (200, 201):
                return True, "上传成功"
            resp = resp2
        return False, f"上传失败: {_parse_http_error(resp)}"
    except Exception as e:
        return False, f"上传异常: {e}"


async def batch_upload_cpa(
    emails: List[str],
    api_url: str,
    api_token: str,
    select_all: bool = False,
    status_filter: str = "",
) -> dict:
    accounts = await _resolve_accounts(emails, select_all, status_filter)
    results = _result_set()
    upload_url = _normalize_cpa_url(api_url)

    async with httpx.AsyncClient(timeout=30) as client:
        for acc in accounts:
            if not acc.get("access_token"):
                _skip(results, acc["email"], "缺少 access_token")
                continue
            ok, msg = await _upload_one_cpa(acc, client, upload_url, api_token)
            if ok:
                _ok(results, acc["email"])
            else:
                _fail(results, acc["email"], msg)

    return results


async def test_cpa_connection(api_url: str, api_token: str) -> Tuple[bool, str]:
    test_url = _normalize_cpa_url(api_url)
    headers = {"Authorization": f"Bearer {api_token}"}
    STATUS_MESSAGES = {
        200: (True,  "CPA 连接测试成功"),
        401: (False, "连接成功，但 API Token 无效"),
        403: (False, "连接成功，但服务端未启用远程管理或当前 Token 无权限"),
        404: (False, "未找到 CPA auth-files 接口，请检查 API URL"),
        503: (False, "连接成功，但服务端认证管理器不可用"),
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(test_url, headers=headers)
        ok, msg = STATUS_MESSAGES.get(resp.status_code, (False, f"异常状态码: {resp.status_code}"))
        return ok, msg
    except httpx.ConnectError as e:
        return False, f"无法连接到服务器: {e}"
    except httpx.TimeoutException:
        return False, "连接超时"
    except Exception as e:
        return False, f"连接测试失败: {e}"


# ── Sub2API ─────────────────────────────────────────────────────────────────────

def _to_unix_ts(expires_at_raw) -> int:
    if isinstance(expires_at_raw, (int, float)):
        return int(expires_at_raw)
    if isinstance(expires_at_raw, str) and expires_at_raw:
        try:
            dt = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except Exception:
            pass
    return 0


def _build_sub2api_item(acc: dict, concurrency: int, priority: int) -> Optional[dict]:
    if not acc.get("access_token"):
        return None
    raw = acc.get("_raw", {})
    expires_at = _to_unix_ts(raw.get("expires_at") or raw.get("expiresAt") or 0)
    return {
        "name": acc.get("email", ""),
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "access_token": acc["access_token"],
            "chatgpt_account_id": acc.get("account_id") or raw.get("account_id", ""),
            "chatgpt_user_id": "",
            "client_id": raw.get("client_id") or raw.get("clientId", ""),
            "expires_at": expires_at,
            "expires_in": 863999,
            "model_mapping": {
                "gpt-5.1": "gpt-5.1",
                "gpt-5.1-codex": "gpt-5.1-codex",
                "gpt-5.1-codex-max": "gpt-5.1-codex-max",
                "gpt-5.1-codex-mini": "gpt-5.1-codex-mini",
                "gpt-5.2": "gpt-5.2",
                "gpt-5.2-codex": "gpt-5.2-codex",
                "gpt-5.3": "gpt-5.3",
                "gpt-5.3-codex": "gpt-5.3-codex",
                "gpt-5.4": "gpt-5.4",
            },
            "organization_id": raw.get("workspace_id") or raw.get("workspaceId", ""),
            "refresh_token": acc.get("refresh_token") or raw.get("refresh_token", ""),
        },
        "extra": {},
        "concurrency": concurrency,
        "priority": priority,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }


async def batch_upload_sub2api(
    emails: List[str],
    api_url: str,
    api_key: str,
    concurrency: int = 3,
    priority: int = 50,
    select_all: bool = False,
    status_filter: str = "",
) -> dict:
    all_accounts = await _resolve_accounts(emails, select_all, status_filter)
    results = _result_set()

    account_items: List[dict] = []
    valid_emails: List[str] = []

    for acc in all_accounts:
        item = _build_sub2api_item(acc, concurrency, priority)
        if item is None:
            _skip(results, acc["email"], "缺少 access_token")
        else:
            account_items.append(item)
            valid_emails.append(acc["email"])

    if not account_items:
        return results

    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "data": {
            "type": "sub2api-data",
            "version": 1,
            "exported_at": exported_at,
            "proxies": [],
            "accounts": account_items,
        },
        "skip_default_group_bind": True,
    }
    url = api_url.rstrip("/") + "/api/v1/admin/accounts/data"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "Idempotency-Key": f"import-{exported_at}",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code in (200, 201):
            for email in valid_emails:
                _ok(results, email, f"批量上传成功（共 {len(valid_emails)} 个）")
        else:
            err = f"上传失败: {_parse_http_error(resp)}"
            for email in valid_emails:
                _fail(results, email, err)
    except Exception as e:
        err = f"上传异常: {e}"
        for email in valid_emails:
            _fail(results, email, err)

    return results


async def test_sub2api_connection(api_url: str, api_key: str) -> Tuple[bool, str]:
    url = api_url.rstrip("/") + "/api/v1/admin/accounts/data"
    headers = {"x-api-key": api_key}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code in (200, 201, 204, 405):
            return True, "Sub2API 连接测试成功"
        if resp.status_code == 401:
            return False, "连接成功，但 API Key 无效"
        if resp.status_code == 403:
            return False, "连接成功，但权限不足"
        return False, f"异常状态码: {resp.status_code}"
    except httpx.ConnectError as e:
        return False, f"无法连接到服务器: {e}"
    except httpx.TimeoutException:
        return False, "连接超时"
    except Exception as e:
        return False, f"连接测试失败: {e}"


async def test_newapi_connection(api_url: str, api_key: str) -> Tuple[bool, str]:
    url = api_url.rstrip("/") + "/api/channel/"
    headers = {"Authorization": f"Bearer {api_key}", "New-Api-User": "1"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code in (200, 201):
            return True, "NewAPI 连接测试成功"
        if resp.status_code == 401:
            return False, "连接成功，但 API Key 无效"
        if resp.status_code == 403:
            return False, "连接成功，但权限不足"
        return False, f"异常状态码: {resp.status_code}"
    except httpx.ConnectError as e:
        return False, f"无法连接到服务器: {e}"
    except httpx.TimeoutException:
        return False, "连接超时"
    except Exception as e:
        return False, f"连接测试失败: {e}"

