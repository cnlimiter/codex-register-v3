"""
test/test_outlook_imap.py — Outlook IMAP 连通性诊断工具

逐步测试:
  1. 从 DB 读取 Outlook 账户配置
  2. HTTPS 连通性 (login.microsoftonline.com:443)
  3. 刷新 access_token（Graph scope）
  4. 刷新 access_token（IMAP scope）
  5. TCP + SSL 连通性 (imap-mail.outlook.com:993)
  6. IMAP XOAUTH2 认证
  7. SELECT INBOX + SEARCH UNSEEN
  8. Graph API 读信测试

用法:
  uv run python test/test_outlook_imap.py              # 测试第 0 个账户
  uv run python test/test_outlook_imap.py --idx 1      # 测试第 1 个账户
  uv run python test/test_outlook_imap.py --all        # 测试所有账户
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
import sys
import time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx
import aioimaplib
from src.db import DB_PATH

# ── 常量 ──────────────────────────────────────────────────────────────────
_IMAP_HOST   = "imap-mail.outlook.com"
_IMAP_PORT   = 993
_AUTH_HOST   = "login.microsoftonline.com"
_TOKEN_URL   = f"https://{_AUTH_HOST}/{{tenant}}/oauth2/v2.0/token"
_SCOPE_GRAPH = "https://graph.microsoft.com/Mail.Read offline_access"
_SCOPE_IMAP  = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"

# ── 颜色输出 ──────────────────────────────────────────────────────────────
def _ok(m):   return f"\033[32m[OK] {m}\033[0m"
def _fail(m): return f"\033[31m[!!] {m}\033[0m"
def _warn(m): return f"\033[33m[!!] {m}\033[0m"
def _info(m): return f"\033[36m[..] {m}\033[0m"
def _head(m): return f"\n\033[1;34m{'='*62}\n  {m}\n{'='*62}\033[0m"


def _detect_proxy() -> str | None:
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        v = os.environ.get(k)
        if v:
            return v
    return None


async def _load_outlook_accounts() -> list[dict]:
    import src.settings_db as sdb
    try:
        await sdb.init()
        data = await sdb.get_section("mail.outlook")
        if isinstance(data, list) and data:
            return data
        if isinstance(data, dict) and data:
            return [data]
    except Exception as e:
        print(_warn(f"DB 读取失败: {e}"))
    return []


async def _token_req(client_id: str, tenant: str, refresh_tok: str, scope: str) -> dict:
    url = _TOKEN_URL.format(tenant=tenant or "consumers")
    # trust_env=False: 避免 Windows 系统代理导致 SSL 错误
    # verify=False: 跳过 certifi 证书验证
    async with httpx.AsyncClient(timeout=20, trust_env=False, verify=False) as c:
        r = await c.post(url, data={
            "client_id":     client_id,
            "grant_type":    "refresh_token",
            "refresh_token": refresh_tok,
            "scope":         scope,
        })
        r.raise_for_status()
        return r.json()


def _make_xoauth2(email: str, token: str) -> str:
    raw = f"user={email}\x01auth=Bearer {token}\x01\x01"
    return base64.b64encode(raw.encode()).decode()


async def _tcp_ssl_check(host: str, port: int, timeout: float = 10.0) -> float | None:
    """返回连接耗时 ms，失败返回 None。"""
    try:
        ctx = ssl.create_default_context()
        t0 = time.monotonic()
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx), timeout=timeout
        )
        ms = (time.monotonic() - t0) * 1000
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return ms
    except Exception:
        return None


async def diagnose_account(acc: dict, idx: int) -> bool:
    email         = acc.get("email", "")
    client_id     = acc.get("client_id", "")
    tenant        = acc.get("tenant_id", "consumers") or "consumers"
    refresh_token = acc.get("refresh_token", "")
    fetch_method  = acc.get("fetch_method", "graph")

    print(_head(f"账户 [{idx}]: {email}  (fetch_method={fetch_method})"))
    all_ok = True

    # ── [1] 配置完整性 ────────────────────────────────────────────────
    print("\n[1] 配置完整性检查")
    for field, val in [("email", email), ("client_id", client_id), ("refresh_token", refresh_token)]:
        if not val:
            print(_fail(f"{field} 为空"))
            all_ok = False
        else:
            disp = val if field == "email" else f"{val[:12]}… (len={len(val)})"
            print(_ok(f"{field} = {disp}"))
    if not all_ok:
        print(_fail("配置不完整，停止测试"))
        return False

    # ── [2] HTTPS 连通性 -> login.microsoftonline.com:443 ─────────────
    print(f"\n[2] HTTPS 连通性 -> {_AUTH_HOST}:443")
    ms = await _tcp_ssl_check(_AUTH_HOST, 443)
    if ms is not None:
        print(_ok(f"TCP+SSL 成功 ({ms:.0f}ms)"))
    else:
        print(_fail(f"无法连接 {_AUTH_HOST}:443"))
        print(_warn("  设置代理: HTTPS_PROXY=http://127.0.0.1:7890 uv run python ..."))
        all_ok = False

    # ── [3] Graph token 刷新 ──────────────────────────────────────────
    print("\n[3] 刷新 access_token（Graph scope）")
    graph_token = None
    try:
        t0 = time.monotonic()
        data = await _token_req(client_id, tenant, refresh_token, _SCOPE_GRAPH)
        elapsed = (time.monotonic() - t0) * 1000
        graph_token = data.get("access_token", "")
        exp = data.get("expires_in", 0)
        print(_ok(f"Graph token 获取成功 ({elapsed:.0f}ms, expires_in={exp}s)"))
        print(_info(f"  token 前缀: {graph_token[:20]}…"))
    except httpx.ConnectError as e:
        print(_fail(f"无法连接 login.microsoftonline.com"))
        print(_info(f"  ConnectError: {e!r}"))
        print(_warn("  设置代理: HTTPS_PROXY=http://127.0.0.1:7890 uv run python ..."))
        all_ok = False
    except httpx.HTTPStatusError as e:
        body = {}
        try:
            body = e.response.json()
        except Exception:
            pass
        code = body.get("error", "")
        desc = body.get("error_description", "")
        print(_fail(f"Graph token 失败 [{e.response.status_code}] {code}"))
        print(_info(f"  {desc[:250]}"))
        if "invalid_grant" in code:
            print(_warn("  refresh_token 已失效，请重新设备码授权"))
        all_ok = False
    except Exception as e:
        print(_fail(f"Graph token 异常 [{type(e).__name__}]: {e!r}"))
        all_ok = False

    # ── [4] IMAP token 刷新 ───────────────────────────────────────────
    print("\n[4] 刷新 access_token（IMAP scope）")
    imap_token = None
    try:
        t0 = time.monotonic()
        data = await _token_req(client_id, tenant, refresh_token, _SCOPE_IMAP)
        elapsed = (time.monotonic() - t0) * 1000
        imap_token = data.get("access_token", "")
        exp = data.get("expires_in", 0)
        print(_ok(f"IMAP token 获取成功 ({elapsed:.0f}ms, expires_in={exp}s)"))
        print(_info(f"  token 前缀: {imap_token[:20]}…"))
    except httpx.ConnectError:
        print(_fail("无法连接 login.microsoftonline.com (IMAP token)"))
        all_ok = False
    except httpx.HTTPStatusError as e:
        body = {}
        try:
            body = e.response.json()
        except Exception:
            pass
        code = body.get("error", "")
        desc = body.get("error_description", "")
        print(_fail(f"IMAP token 失败 [{e.response.status_code}] {code}"))
        print(_info(f"  {desc[:300]}"))
        if "AADSTS65005" in desc or "AADSTS70011" in desc:
            print(_warn(
                "  Azure 应用缺少 IMAP.AccessAsUser.All 权限\n"
                "    -> Azure 门户 -> API 权限 -> 添加权限 -> IMAP.AccessAsUser.All\n"
                "    -> 然后重新运行设备码授权"
            ))
        elif "invalid_grant" in code:
            print(_warn("  refresh_token 未含 IMAP scope，请用 IMAP scope 重新授权"))
        all_ok = False
    except Exception as e:
        print(_fail(f"IMAP token 异常 [{type(e).__name__}]: {e!r}"))
        all_ok = False

    if imap_token is None:
        print(_fail("无 IMAP token，跳过后续 IMAP 测试"))
        return False

    # ── [5] TCP/SSL -> imap-mail.outlook.com:993 ──────────────────────
    print(f"\n[5] TCP/SSL -> {_IMAP_HOST}:{_IMAP_PORT}")
    ms = await _tcp_ssl_check(_IMAP_HOST, _IMAP_PORT)
    if ms is not None:
        print(_ok(f"TCP+SSL 连接成功 ({ms:.0f}ms)"))
    else:
        print(_fail(f"无法连接 {_IMAP_HOST}:{_IMAP_PORT}"))
        return False

    # ── [6] IMAP XOAUTH2 认证 ─────────────────────────────────────────
    print("\n[6] IMAP XOAUTH2 认证")
    xoauth2 = _make_xoauth2(email, imap_token)
    imap = None
    auth_ok = False
    try:
        imap = aioimaplib.IMAP4_SSL(host=_IMAP_HOST, port=_IMAP_PORT, timeout=20)
        await imap.wait_hello_from_server()
        print(_info("  HELLO 已收到"))
        # aioimaplib xoauth2(user, raw_access_token_str) 内部自建 XOAUTH2 SASL
        resp = await imap.xoauth2(email, imap_token)
        ok = resp.result
        lines = resp.lines
        print(_info(f"  XOAUTH2 响应: result={ok!r}  lines={lines!r}"))
        if ok == "OK":
            print(_ok("XOAUTH2 认证成功"))
            auth_ok = True
        else:
            print(_fail(f"XOAUTH2 认证失败: {ok}"))
            for part in lines:
                if isinstance(part, bytes) and len(part) > 4:
                    try:
                        decoded = base64.b64decode(part).decode("utf-8", errors="replace")
                        print(_info(f"  服务端详情: {decoded}"))
                        d = json.loads(decoded)
                        if "400" in str(d.get("status", "")):
                            print(_warn(
                                "  XOAUTH2 被拒 (400):\n"
                                "    1. 该账户未启用 IMAP\n"
                                "       -> outlook.com -> 设置 -> 邮件 -> 同步邮件 -> 启用 IMAP\n"
                                "    2. Azure 应用未获 IMAP.AccessAsUser.All 权限\n"
                                "    3. 企业账户需管理员授权"
                            ))
                    except Exception:
                        pass
            all_ok = False
    except asyncio.TimeoutError:
        print(_fail("IMAP 认证超时 (20s)"))
        all_ok = False
    except Exception as e:
        print(_fail(f"IMAP 连接异常 [{type(e).__name__}]: {e!r}"))
        print(_info(f"  args = {e.args!r}"))
        if not e.args or all(not str(a) for a in e.args):
            print(_warn(
                "  空异常 — 常见原因:\n"
                "    * 账户未启用 IMAP（最常见）\n"
                "      -> outlook.com -> 设置 -> 邮件 -> 同步邮件 -> 启用 IMAP\n"
                "    * XOAUTH2 token 格式错误"
            ))
        all_ok = False
    finally:
        if imap is not None:
            try:
                await imap.logout()
            except Exception:
                pass

    if not auth_ok:
        return False

    # ── [7] SELECT INBOX + SEARCH UNSEEN ─────────────────────────────
    print("\n[7] SELECT INBOX + SEARCH UNSEEN")
    imap2 = None
    try:
        imap2 = aioimaplib.IMAP4_SSL(host=_IMAP_HOST, port=_IMAP_PORT, timeout=20)
        await imap2.wait_hello_from_server()
        await imap2.xoauth2(email, imap_token)

        resp2 = await imap2.select("INBOX")
        ok, data = resp2.result, resp2.lines
        if ok == "OK":
            count = data[0].decode() if data and isinstance(data[0], bytes) else "?"
            print(_ok(f"INBOX 选取成功，共 {count} 封邮件"))
        else:
            print(_fail(f"SELECT INBOX 失败: {data!r}"))
            all_ok = False

        resp3 = await imap2.search("UNSEEN", charset=None)  # Outlook 不支持 utf-8 charset
        ok, data = resp3.result, resp3.lines
        if ok == "OK":
            raw = data[0]
            if isinstance(raw, bytes):
                raw = raw.decode()
            uids = [u for u in raw.split() if u]
            print(_ok(f"SEARCH UNSEEN 成功，未读 {len(uids)} 封"))
            if uids:
                print(_info(f"  最新 UID: {uids[:5]}"))
        else:
            print(_fail(f"SEARCH UNSEEN 失败: {data!r}"))
            all_ok = False

    except Exception as e:
        print(_fail(f"INBOX/SEARCH 异常 [{type(e).__name__}]: {e!r}"))
        all_ok = False
    finally:
        if imap2 is not None:
            try:
                await imap2.logout()
            except Exception:
                pass

    # ── [8] Graph API 读信测试 ────────────────────────────────────────
    if graph_token:
        print("\n[8] Graph API 读信测试")
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False, verify=False) as c:
                r = await c.get(
                    "https://graph.microsoft.com/v1.0/me/messages",
                    headers={"Authorization": f"Bearer {graph_token}"},
                    params={
                        "$select": "id,subject,receivedDateTime,isRead",
                        "$filter": "isRead eq false",
                        "$top": "5",
                        "$orderby": "receivedDateTime desc",
                    },
                )
                r.raise_for_status()
                msgs = r.json().get("value", [])
                print(_ok(f"Graph API 成功，未读邮件 {len(msgs)} 封"))
                for m in msgs[:3]:
                    print(_info(f"  [{m.get('receivedDateTime','')}] {m.get('subject','(无)')}"))
        except httpx.ConnectError:
            print(_fail("Graph API — 无法连接 graph.microsoft.com"))
        except httpx.HTTPStatusError as e:
            print(_fail(f"Graph API [{e.response.status_code}]: {e.response.text[:200]}"))
        except Exception as e:
            print(_fail(f"Graph API 异常: {e!r}"))

    return all_ok


# ── 主入口 ────────────────────────────────────────────────────────────────
async def main() -> None:
    argv = sys.argv[1:]
    test_all = "--all" in argv
    idx_arg: int | None = None
    for i, a in enumerate(argv):
        if a == "--idx" and i + 1 < len(argv):
            try:
                idx_arg = int(argv[i + 1])
            except ValueError:
                pass

    print(_head("Outlook IMAP 连通性诊断"))
    print(f"DB: {DB_PATH}")

    proxy = _detect_proxy()
    if proxy:
        print(_info(f"检测到代理环境变量: {proxy}"))
    else:
        print(_warn("未检测到 HTTPS_PROXY/HTTP_PROXY 环境变量"))
        print(_info("如需代理: set HTTPS_PROXY=http://127.0.0.1:7890"))

    import src.db as db_mod
    await db_mod.init()
    accounts = await _load_outlook_accounts()

    if not accounts:
        print(_fail("未找到任何 Outlook 账户配置"))
        print(_info("请在 WebUI -> Settings -> Outlook 页面添加账户"))
        return

    print(_info(f"共找到 {len(accounts)} 个 Outlook 账户"))

    targets = list(range(len(accounts))) if test_all else [idx_arg if idx_arg is not None else 0]

    results: list[tuple[int, bool]] = []
    for i in targets:
        if i >= len(accounts):
            print(_fail(f"账户索引 {i} 不存在"))
            results.append((i, False))
            continue
        ok = await diagnose_account(accounts[i], i)
        results.append((i, ok))

    print(_head("诊断汇总"))
    for i, ok in results:
        em = accounts[i].get("email", "?") if i < len(accounts) else "?"
        print(f"  [{i}] {em}: {_ok('PASS') if ok else _fail('FAIL')}")

    print()
    if all(ok for _, ok in results):
        print(_ok("所有测试通过！"))
    else:
        print(_warn("存在失败项，请根据上方提示排查。"))
        print(_info(
            "常见修复:\n"
            "  * IMAP 未启用  -> outlook.com 设置 -> 邮件 -> 同步 -> 启用 IMAP\n"
            "  * 缺少权限     -> Azure 添加 IMAP.AccessAsUser.All 并重新授权\n"
            "  * 网络受限     -> set HTTPS_PROXY=http://代理IP:端口\n"
            "  * Token 失效   -> 重新运行设备码授权流程"
        ))


if __name__ == "__main__":
    asyncio.run(main())
