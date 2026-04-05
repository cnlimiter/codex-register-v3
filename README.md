# codex-register-v3
<p align="center">
  <a href="https://linux.do" target="_blank">
    <img src="https://img.shields.io/badge/LINUX-DO-FFB003?style=for-the-badge&logo=linux&logoColor=white" alt="LINUX DO" />
  </a>
</p>
ChatGPT 账号无头浏览器自动批量注册工具。

支持双浏览器引擎（Playwright Chromium / Camoufox Firefox）、多种邮件服务（GPTMail / NPCmail / YYDS Mail / 通用 IMAP / Outlook）、代理池轮换，asyncio 并发执行，SQLite 持久化存储，注册完成后自动完成 Codex OAuth Token 换取。

---

## 功能特性

| 功能 | 说明 |
|------|------|
| 双引擎 | Playwright (Chromium) 或 Camoufox (Firefox)，可通过 WebUI 或 CLI 即时切换 |
| 反检测 | Chromium 注入 13 点 stealth JS；Camoufox 内置指纹混淆 + GeoIP |
| 手机指纹 | 顶层 `mobile: true` 即可全程使用手机端 UA / 视口 / 触控，注册与 OAuth 复用同一 session |
| 多邮件后端 | GPTMail / NPCmail / YYDS Mail / **通用 IMAP** / **Outlook**，统一工厂接口 |
| IMAP 别名 | qq.com / gmail.com 自动启用 `+alias` 子地址，每次注册生成唯一别名，并发互不干扰 |
| 多 IMAP 账户 | 支持配置多个 IMAP 邮箱，随机轮换或按索引固定使用 |
| WebUI 配置 | WebUI/FastAPI 配置存储在 SQLite `settings` 表；支持页面直接修改各配置 section |
| OAuth Token | 注册完成后自动完成 Codex PKCE OAuth2 流程，写入 `access_token` / `refresh_token` / `id_token` |
| OAuth OTP 重试 | OAuth 登录阶段若直接进入邮箱验证码，支持自动检测；验证码错误时自动重发、拉取新码并重试 |
| 代理池 | 从文本文件导入，轮询分配，失败 3 次自动禁用 |
| 并发 | asyncio.Semaphore 控制同时运行的浏览器数量 |
| 持久化 | aiosqlite 驱动的 SQLite，账号去重 upsert |
| CLI / WebUI | typer + rich CLI，以及 FastAPI + React WebUI |
| 日志 | loguru 双路输出：终端彩色 INFO + 文件 DEBUG（register.log，10 MB 自动轮转） |

---

## 注册流程（7 步状态机）

注册流程完全逆向自 `plan/browser/tool.js` (`_0x548_inner`) 并在 Python 中忠实复现：

```
GOTO_SIGNUP → FILL_EMAIL → FILL_PASSWORD → WAIT_CODE
            → FILL_CODE  → FILL_PROFILE  → COMPLETE
```

| 状态 | 动作 |
|------|------|
| `GOTO_SIGNUP` | 打开 `chatgpt.com/auth/login`，等待 Auth0 跳转；若邮箱框已出现直接填写，否则查找并点击"Sign up"按钮 |
| `FILL_EMAIL` | 等待邮箱输入框，用 React 原生 setter 填入生成的邮箱，点击 Continue |
| `FILL_PASSWORD` | 等待密码框，填入自动生成的强密码（大写+小写+数字+特殊字符），点击 Continue |
| `WAIT_CODE` | 轮询邮件服务获取 6 位验证码（最长 60 s，可配置） |
| `FILL_CODE` | 将 6 位验证码逐位填入 `input[maxlength="1"]` 方格或单字段，点击 Continue |
| `FILL_PROFILE` | 填写 firstName / lastName；生日优先走 `[role="spinbutton"]`，不足时回退到 `<select>` 下拉或 `input[type='date']`；点击 Agree |
| `COMPLETE` | 等待跳回 `chatgpt.com`，账号标记为「注册完成」；随即执行 Codex OAuth 换取 Token |

每步失败后最多重试 5 次，指数退避（网络错误最长 60 s，其他错误最长 30 s）；任何步骤检测到错误页面均自动重试。

OAuth 阶段会复用注册完成后的同一个浏览器会话 / Cookie；若 Auth0 要求邮箱 OTP，当前实现支持：

- 邮箱提交后**直接跳到验证码页**（无密码框）
- 验证码输错后自动点击 **Resend**、拉取**新验证码**并重试
- 若回调 URL 已变为 `http://localhost:1455/auth/callback?code=...`，即使路由拦截未触发，也会从当前页面 URL 直接提取 `code` 并进入换 token

---

## 环境要求

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/) 包管理器

---

## 安装

```powershell
# 1. 克隆项目
git clone <repo-url>
cd codex-register-v3-single

# 2. 安装依赖
uv sync

# 3. 安装浏览器
uv run python -m playwright install chromium
uv run python -m camoufox fetch   # 下载 GeoIP 数据库（约 65 MB）

# 4. 初始化数据库
uv run python -m src.main db init

```

---

## 配置

当前项目的**唯一运行时配置源**是 SQLite `accounts.db` 中的 `settings` 表，由 `src/settings_db.py` 管理。

### 推荐用法

- 日常使用 **WebUI** 修改配置：

```powershell
uv run python -m src.main webui
```

- 也可以用 CLI 直接读写 SQLite 配置：

```powershell
uv run python -m src.main config show
uv run python -m src.main config get engine
uv run python -m src.main config set engine camoufox
uv run python -m src.main config set timeouts.otp_code 240
```

### 主要配置 section

| Section | 内容 |
|--------|------|
| `general` | `engine` / `headless` / `slow_mo` / `mobile` / `max_concurrent` / `mail_provider` / `proxy_strategy` / `proxy_static` |
| `mail.gptmail` / `mail.npcmail` / `mail.yydsmail` | API 邮箱配置 |
| `mail.imap` / `mail.outlook` | 自有邮箱账号列表 |
| `registration` | 邮箱前缀 / 域名 |
| `oauth` | 是否启用 OAuth / OAuth 总超时 |
| `mouse` | 人工轨迹点击配置 |
| `timeouts` | 各阶段超时 |
| `timing` | 页面动作节奏参数 |
| `team` / `sync` | 预留的同步相关配置 |

### 配置字段速查

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `engine` | `playwright` | `playwright` \| `camoufox` |
| `headless` | `true` | `false` 可在窗口中实时观察注册过程 |
| `slow_mo` | `0` | 每步额外延迟 ms，`0` = 自动 |
| `mobile` | `false` | `true` = 整个流程（含 OAuth）使用手机指纹 |
| `max_concurrent` | `2` | 最大并发浏览器数量 |
| `mail_provider` | `gptmail` | 见下方[邮件服务说明](#邮件服务说明) |
| `proxy_strategy` | `none` | `pool` \| `static` \| `none` |
| `proxy_static` | `""` | 固定代理 URL |
| `oauth.enabled` | `true` | 注册后自动换取 Codex Token |
| `mouse.human_simulation` | `true` | `false` 时关键按钮退回直接点击，不走人工轨迹 |

如果你是通过 WebUI 使用本项目，可以把 SQLite `settings` 直接视为唯一配置源。

---

## 邮件服务说明

### API 类（临时邮箱）

| 服务 | `mail_provider` 值 | 获取 Key |
|------|--------------------|---------|
| **GPTMail** | `gptmail` | 公共 Key `gpt-test` 可免费使用，有频率限制；大批量请申请付费 Key |
| **NPCmail** | `npcmail` | 注册 [dash.xphdfs.me](https://dash.xphdfs.me) 获取 |
| **YYDS Mail** | `yydsmail` | 注册 [maliapi.215.im](https://maliapi.215.im/v1) 获取 |

### IMAP（自有真实邮箱）

使用自己的 Gmail / QQ 邮箱直接收取验证码，无需第三方 API Key。

**启用方法：**

- 在 WebUI 中将 `general.mail_provider` 设为 `imap` / `imap:0` / `imap:1`
- 在 `mail.imap` section 中配置账号列表，结构示例：

```json
[
  {
    "email": "yourname@gmail.com",
    "password": "abcd efgh ijkl mnop",
    "host": "imap.gmail.com",
    "port": 993,
    "ssl": true,
    "folder": "INBOX"
  },
  {
    "email": "123456@qq.com",
    "password": "xxxxxxxxxxxxxx",
    "host": "imap.qq.com",
    "port": 993,
    "ssl": true,
    "folder": "INBOX"
  }
]
```

**别名模式（`+alias` 子地址）：**

`qq.com` 和 `gmail.com` 域名**自动启用**：每次注册生成独立别名地址（如 `yourname+a3k9xm2b@gmail.com`），验证码仍投递到原收件箱，`poll_code()` 通过 `To:` 头部过滤，多并发任务互不干扰。

其他域名如需别名模式，在账户配置中加 `use_alias: true`。

**前置步骤：**

| 邮箱 | 步骤 |
|------|------|
| Gmail | 账户安全 → 应用专用密码（生成 16 位密码）；Gmail 设置 → IMAP → 启用 |
| QQ 邮箱 | 设置 → 账户 → IMAP/SMTP 服务 → 开启，获取授权码 |

### Outlook / Hotmail（Microsoft OAuth2）

使用 Outlook、Hotmail 或 Live 邮箱通过 Microsoft Graph API 或 IMAP XOAUTH2 收取验证码。需要在 Azure AD 注册一个应用，并预先完成 OAuth2 设备码授权流程获得 `refresh_token`。

#### 前置步骤：在 Azure AD 注册应用

1. 登录 [Azure 门户](https://portal.azure.com) → **Microsoft Entra ID（Azure AD）** → **应用注册** → **新注册**。
2. **受支持的账户类型** 选 _个人 Microsoft 账户（仅 outlook.com / hotmail.com）_，填入任意名称。
3. **重定向 URI** 选 _公共客户端/本机_ → 填入：
   ```
   https://login.microsoftonline.com/common/oauth2/nativeclient
   ```
4. 注册完成后，在 **概览** 页面复制 **应用程序（客户端）ID**（即 `client_id`）。
5. 进入 **API 权限** → **添加权限** → **Microsoft Graph** → **委托权限**，添加：
   - `Mail.Read`（Graph 收信模式，推荐）
   - `offline_access`（允许刷新 Token）
   - 若使用 IMAP 模式，改为添加 `IMAP.AccessAsUser.All` + `offline_access`
6. 进入 **身份验证** → 底部勾选 **允许公共客户端流**（_Enable the following mobile and desktop flows_）。

#### 获取 refresh_token（一次性操作）

**推荐方法：设备码流**

```powershell
# Graph 模式（推荐）
$clientId = "你的 client_id"
$scope    = "https://graph.microsoft.com/Mail.Read offline_access"

# 1. 发起设备码请求
$resp = Invoke-RestMethod -Method Post `
  -Uri "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode" `
  -Body @{ client_id=$clientId; scope=$scope }

# 2. 按提示在浏览器中打开链接并登录
Write-Host $resp.message

# 3. 轮询获取 Token（登录完成后执行）
$token = Invoke-RestMethod -Method Post `
  -Uri "https://login.microsoftonline.com/consumers/oauth2/v2.0/token" `
  -Body @{
    client_id=$clientId; grant_type="urn:ietf:params:oauth:grant-type:device_code"
    device_code=$resp.device_code
  }

Write-Host "refresh_token:" $token.refresh_token
```

> 也可以使用 [msal-python](https://github.com/AzureAD/microsoft-authentication-library-for-python) 等工具完成设备码流程，或在 Web UI 中通过 **Settings → Outlook/Hotmail → 添加账户** 界面完成。

#### 配置 Outlook 账户

- 在 WebUI 中将 `general.mail_provider` 设为 `outlook` / `outlook:0` / `outlook:1`
- 在 `mail.outlook` section 中配置账号列表，结构示例：

```json
[
  {
    "email": "yourname@outlook.com",
    "client_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "tenant_id": "consumers",
    "refresh_token": "0.AXXXXXXXXXXXXX...",
    "fetch_method": "graph"
  }
]
```

> `fetch_method` 说明：
> - `graph`（默认）：通过 Microsoft Graph REST API 收信，**无需开启 IMAP**，推荐使用。
> - `imap`：通过 IMAP XOAUTH2 收信，需在 Azure 权限中添加 `IMAP.AccessAsUser.All`。

#### CLI 启动注册

```powershell
# 切换邮件服务为 outlook 并注册 3 个账号
uv run python -m src.main config set mail_provider outlook
uv run python -m src.main register --count 3

# 或者直接用 --provider 参数（不改当前 DB 配置）
uv run python -m src.main register --count 3 --provider outlook

# 固定使用第 0 个 Outlook 账户注册
uv run python -m src.main register --count 5 --provider outlook:0

# 搭配代理注册
uv run python -m src.main register --count 3 --provider outlook --proxy "http://user:pass@host:port"

# 有头调试模式（可在浏览器窗口观察注册过程）
uv run python -m src.main register --count 1 --provider outlook --headed
```

**注意事项：**
- Outlook 账号直接用于注册（邮箱地址即被注册的 ChatGPT 账号），不支持 `+alias` 别名模式；多账户并发时每个浏览器任务使用不同的 Outlook 账号。
- `access_token` 由程序自动管理（约 1 小时有效期），无需手动填写；`refresh_token` 长期有效，只要不撤销授权即可一直使用。
- 若报错 `No refresh_token configured`，说明该账户的 `refresh_token` 为空，需重新完成设备码授权流程。

---

## CLI 命令

### 注册账号

```powershell
# 注册 5 个账号
uv run python -m src.main register --count 5

# 指定引擎和邮件服务（覆盖当前 DB 配置）
uv run python -m src.main register --count 3 --engine camoufox --provider imap:0

# 覆盖并发数
uv run python -m src.main register --count 10 --concurrency 4

# 临时指定代理（优先级最高）
uv run python -m src.main register --count 3 --proxy "http://user:pass@host:port"

# 有头模式调试（可在浏览器窗口实时观察）
uv run python -m src.main register --count 1 --headed
```

### 查看账号

```powershell
# 列出全部账号
uv run python -m src.main list-accounts

# 按状态筛选
uv run python -m src.main list-accounts --status 注册完成
```

### 导出账号

```powershell
uv run python -m src.main export --format json --output accounts.json
uv run python -m src.main export --format csv  --output accounts.csv
```

### 导入账号

```powershell
uv run python -m src.main import-accounts accounts.json
uv run python -m src.main import-accounts accounts.txt   # email:password 格式
```

### 管理代理

```powershell
# 从文件导入代理（默认 proxies.txt）
uv run python -m src.main import-proxies
uv run python -m src.main import-proxies my_proxies.txt

# 启用代理池
uv run python -m src.main config set proxy_strategy pool

# 固定代理
uv run python -m src.main config set proxy_static "http://user:pass@host:port"
uv run python -m src.main config set proxy_strategy static

# 关闭代理
uv run python -m src.main config set proxy_strategy none
```

> **代理优先级**：`--proxy` CLI 参数 > `proxy_strategy=static` > `proxy_strategy=pool` > `none`

### 配置管理

```powershell
# 查看全部运行时配置（SQLite）
uv run python -m src.main config show

# 读取单项
uv run python -m src.main config get engine

# 修改（自动推断 int / float / bool 类型）
uv run python -m src.main config set engine camoufox
uv run python -m src.main config set max_concurrent 4
uv run python -m src.main config set mail_provider imap
uv run python -m src.main config set headless false
uv run python -m src.main config set mobile true
uv run python -m src.main config set timeouts.otp_code 240
```

### WebUI

```powershell
uv run python -m src.main webui
```

默认地址：`http://localhost:7860`

WebUI 启动后会：

- 初始化数据库
- 通过 FastAPI 提供配置、任务、账号、代理等接口
- 使用 `webui_frontend/` 构建后的前端页面

### 数据库

```powershell
uv run python -m src.main db init
```

---

## 代理文件格式

`proxies.txt` 每行一个代理：

```
http://host:port
http://user:pass@host:port
socks5://user:pass@host:port
```

---

## 项目结构

```
codex-register-v3-single/
├── proxies.txt          # 代理列表
├── accounts.db          # SQLite 数据库
├── register.log         # 调试日志（10 MB 自动轮转，保留 7 天）
├── pyproject.toml
├── plan/                # 只读参考资料，不要修改
│   ├── oauth.har        # 实录 OAuth 流程 HAR 包
│   └── browser/
│       └── tool.js      # 原始 JS 用户脚本（注册状态机逆向来源）
├── src/
│   ├── main.py          # CLI 入口（typer）
│   ├── config.py        # SQLite 兼容层（保留 load/get/set_key 旧接口）
│   ├── settings_db.py   # WebUI/运行时配置的 SQLite 存储层
│   ├── db.py            # SQLite schema 初始化
│   ├── accounts.py      # 账号 CRUD / 导入导出
│   ├── proxy_pool.py    # 代理池（轮询 + 失败计数自动禁用）
│   ├── browser/
│   │   ├── engine.py    # 浏览器工厂（playwright / camoufox，含手机指纹）
│   │   ├── helpers.py   # DOM 工具（React input 填充、人工鼠标移动等）
│   │   ├── register.py  # 7 步注册状态机
│   │   └── oauth.py     # Codex PKCE OAuth2 Token 换取（含 OTP 重试 / callback URL 补抓取）
│   ├── webui/
│   │   └── server.py    # FastAPI WebUI 后端
│   └── mail/
│       ├── base.py      # 抽象基类 MailClient
│       ├── gptmail.py   # GPTMail 客户端
│       ├── npcmail.py   # NPCmail 客户端
│       ├── yydsmail.py  # YYDS Mail 客户端
│       ├── imap.py      # 通用 IMAP 客户端（含多账户/别名）
│       └── outlook.py   # Outlook / Hotmail 客户端
└── webui_frontend/
    ├── package.json     # React + Vite 前端
    └── src/
```

---

## 开发与调试

```powershell
# 测试浏览器引擎（访问 chatgpt.com 并截图）
uv run python -m src.browser.engine playwright
uv run python -m src.browser.engine camoufox --headed

# 注册状态机空跑（不启动浏览器，仅打印 7 步日志）
uv run python -m src.browser.register

# 测试 IMAP 邮箱连通性（等待 30 s 内是否能收到验证码）
uv run python -m src.mail.imap

# 测试其他邮件服务
uv run python -m src.mail.gptmail YOUR_API_KEY
```

---

## 超时配置参考

所有超时单位为**秒**，保存在 SQLite `settings.timeouts` section：

| 键 | 默认值 | 阶段 |
|----|--------|------|
| `page_load` | 30 | `page.goto()` 导航超时 |
| `auth0_redirect` | 8 | 等待跳转到 auth.openai.com |
| `email_input` | 15 | 等待邮箱输入框出现 |
| `password_input` | 60 | 等待密码输入框出现 |
| `otp_input` | 60 | 等待 OTP 输入框出现 |
| `otp_code` | 180 | 轮询邮箱获取验证码的最长时间 |
| `profile_detect` | 15 | 等待姓名输入框出现 |
| `profile_field` | 5 | 等待生日/姓名等单个资料字段 |
| `complete_redirect` | 20 | 等待跳回 chatgpt.com |
| `oauth_navigate` | 20 | OAuth authorize 导航超时 |
| `oauth_flow_element` | 8 | OAuth 中间页按钮等待 |
| `oauth_login_email` | 8 | OAuth 登录页邮箱框等待 |
| `oauth_login_password` | 10 | OAuth 登录页密码框等待 |
| `oauth_token_exchange` | 30 | `/oauth/token` HTTP 请求超时 |
| `oauth_total` | 45 | Codex OAuth 全流程硬超时 |

使用慢速代理时建议将 `page_load` 调大至 60：

```powershell
uv run python -m src.main config set timeouts.page_load 60
uv run python -m src.main config set timeouts.otp_code 240
```

---

## 依赖项

| 包 | 用途 |
|----|------|
| `playwright` | Chromium 浏览器自动化 |
| `camoufox[geoip]` | Firefox 指纹混淆引擎 |
| `httpx` | 异步 HTTP 客户端（邮件 API / OAuth） |
| `aioimaplib` | 异步 IMAP 客户端 |
| `aiosqlite` | 异步 SQLite |
| `loguru` | 结构化日志 |
| `typer` | CLI 框架 |
| `rich` | 终端美化输出 |

---

## 注意事项

- 本工具仅供学习研究使用，请遵守 ChatGPT 服务条款。
- 建议搭配质量稳定的代理使用，避免 IP 被封禁。
- Gmail / QQ 邮箱的 IMAP 密码须使用**应用专用密码**（授权码），而非登录密码。
- 日志文件 `register.log` 超过 10 MB 自动轮转，保留 7 天。
- 注册密码由程序自动生成（16 位，含大写+小写+数字+特殊字符），写入数据库。
- 调试时设置 `headless: false` 可在浏览器窗口中实时观察每一步操作。
