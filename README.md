# aistudio-api

基于 [DeconstructedCube/aistudio-api](https://github.com/DeconstructedCube/aistudio-api)，
本分支维护于 [ecyasd/aistudio-api](https://github.com/ecyasd/aistudio-api)。

本分支包含浏览器启动与首次调用恢复、账号会话隔离、Cookie 刷新保存、API Key
配置持久化，以及前端「实时错误」页面。错误仅保留在内存中，最近 10 分钟最多
100 条，页面可见时每 5 秒刷新，重启后清空。Cookie 被 Google 撤销或登录过期时，
仍需重新导入有效登录状态，现有调用密钥保持不变。公开 HTTPS 运行与自动发布说明见
[deploy/README.md](deploy/README.md)。
协议保真、账号隔离和失败处理的实现边界见 [反代契约](docs/PROXY_CONTRACT.md)。
上游 Docker Hub 镜像不包含本分支修复；使用
`docker build -t aistudio-api:local .` 从本仓库构建，才能使用对应的前后端。

<p align="center">
  <a href="https://github.com/DeconstructedCube/aistudio-api"><img src="https://img.shields.io/badge/API-Gemini%20v1beta-4285F4?style=flat-square&logo=google" alt="Gemini API"></a>
  <a href="https://deepwiki.com/DeconstructedCube/aistudio-api"><img src="https://img.shields.io/badge/DeepWiki-DeconstructedCube%2Faistudio--api-5B68E4?style=flat-square&logo=data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACwAAAAyCAYAAAAnWDnqAAAAAXNSR0IArs4c6QAAA05JREFUaEPtmUtyEzEQhtWTQyQLHNak2AB7ZnyXZMEjXMGeK/AIi+QuHrMnbChYY7MIh8g01fJoopFb0uhhEqqcbWTp06/uv1saEDv4O3n3dV60RfP947Mm9/SQc0ICFQgzfc4CYZoTPAswgSJCCUJUnAAoRHOAUOcATwbmVLWdGoH//PB8mnKqScAhsD0kYP3j/Yt5LPQe2KvcXmGvRHcDnpxfL2zOYJ1mFwrryWTz0advv1Ut4CJgf5uhDuDj5eUcAUoahrdY/56ebRWeraTjMt/00Sh3UDtjgHtQNHwcRGOC98BJEAEymycmYcWwOprTgcB6VZ5JK5TAJ+fXGLBm3FDAmn6oPPjR4rKCAoJCal2eAiQp2x0vxTPB3ALO2CRkwmDy5WohzBDwSEFKRwPbknEggCPB/imwrycgxX2NzoMCHhPkDwqYMr9tRcP5qNrMZHkVnOjRMWwLCcr8ohBVb1OMjxLwGCvjTikrsBOiA6fNyCrm8V1rP93iVPpwaE+gO0SsWmPiXB+jikdf6SizrT5qKasx5j8ABbHpFTx+vFXp9EnYQmLx02h1QTTrl6eDqxLnGjporxl3NL3agEvXdT0WmEost648sQOYAeJS9Q7bfUVoMGnjo4AZdUMQku50McDcMWcBPvr0SzbTAFDfvJqwLzgxwATnCgnp4wDl6Aa+Ax283gghmj+vj7feE2KBBRMW3FzOpLOADl0Isb5587h/U4gGvkt5v60Z1VLG8BhYjbzRwyQZemwAd6cCR5/XFWLYZRIMpX39AR0tjaGGiGzLVyhse5C9RKC6ai42ppWPKiBagOvaYk8lO7DajerabOZP46Lby5wKjw1HCRx7p9sVMOWGzb/vA1hwiWc6jm3MvQDTogQkiqIhJV0nBQBTU+3okKCFDy9WwferkHjtxib7t3xIUQtHxnIwtx4mpg26/HfwVNVDb4oI9RHmx5WGelRVlrtiw43zboCLaxv46AZeB3IlTkwouebTr1y2NjSpHz68WNFjHvupy3q8TFn3Hos2IAk4Ju5dCo8B3wP7VPr/FGaKiG+T+v+TQqIrOqMTL1VdWV1DdmcbO8KXBz6esmYWYKPwDL5b5FA1a0hwapHiom0r/cKaoqr+27/XcrS5UwSMbQAAAABJRU5ErkJggg==" alt="DeepWiki"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/Framework-FastAPI-009688?style=flat-square&logo=fastapi" alt="FastAPI"></a>
  <a href="https://vuejs.org/"><img src="https://img.shields.io/badge/Frontend-Vue%203%20%2B%20Vite-4FC08D?style=flat-square&logo=vuedotjs" alt="Vue 3"></a>
  <a href="https://docs.astral.sh/uv/"><img src="https://img.shields.io/badge/Python-3.11%2B%20%7C%20uv-261230?style=flat-square&logo=python" alt="Python uv"></a>
</p>

<p align="center">
  Google AI Studio 反向代理服务，将 Web 端 AI Studio 转换为标准 Google Gemini API 协议接口。
</p>

<p align="center">
  <a href="./README_EN.md">English Documentation</a>
</p>

---

## 目录

- [核心特性](#核心特性)
- [技术架构与文档](#技术架构与文档)
- [快速开始](#快速开始)
  - [Android (Termux)](#android-termux)
  - [Windows](#windows)
  - [Linux / macOS](#linux--macos)
  - [Docker 部署](#docker-部署)
- [配置与环境变量](#配置与环境变量)
- [API 调用示例](#api-调用示例)
  - [cURL 调用](#curl-调用)
  - [Python SDK (google-genai)](#python-sdk-google-genai)
  - [工具调用 (Function Calling)](#工具调用-function-calling)
- [Web 管理控制台](#web-管理控制台)
- [开发与测试](#开发与测试)

---

## 核心特性

| 功能模块 | 说明 |
|---|---|
| **Gemini 协议兼容** | 支持 `/v1beta/...` 接口规范，包括 Thinking 思维链、多模态、Function Calling 工具调用及图片生成 |
| **动态模型发现** | 自动向上游同步可用模型列表，支持 `gemini-3.7-flash`、`gemini-3.8-flash` 等最新模型 |
| **多账号自动轮换** | 支持多账号管理与故障自动切换，按模型独立记录 429 配额，每日美西午夜自动重置 |
| **运行状态持久化** | 模型调用量、Token 消耗统计 (`stats.json`) 以及账号 429 限额与冷却状态 (`rotator_state.json`) 自动落盘，容器/服务重启不丢失状态 |
| **下游参数容错与丢弃** | 支持配置 `drop_unsupported_params` 自动过滤下游传入的非标准安全分类（如 `HARM_CATEGORY_CIVIC_INTEGRITY`）与不兼容工具，避免 400 阻断 |
| **批量子账号探活** | 导入单个包含多个 Google 身份的 Cookie 后，自动递归探活子账号（`u/0`, `u/1`...）并分别建档 |
| **内置搜索与工具** | 支持 Google Search 联网搜索、Google Maps、代码执行沙箱等官方扩展能力 |
| **轻量 CDP 驱动** | 基于纯 Python 异步 WebSocket 直连 Chrome DevTools Protocol，无需 Node.js、Playwright 或 Selenium |
| **Web 管理面板** | 提供 Web 管理控制台，支持账号管理（文本与 JSON 批量导入）、调用量监控、在线编辑与热重载 `config.yaml` |
| **独立访问凭据** | 网站管理密码与 API 调用密钥分离；修改模型设置、重新导入或激活账号不会重置现有调用密钥 |
| **实时错误** | `/error-records` 展示经过脱敏的最近错误；仅在内存保留 10 分钟、最多 100 条，重启后清空 |
| **Android 伴侣端** | 配套 Android 原生应用（`android/`），采用 Jetpack Compose 构建，支持多 Profile 隔离登录、Bitwarden 自动填充与一键导出凭据包 |
> [!NOTE]
> **内存占用参考**：纯 Python 后端服务常驻内存约 35 - 45 MB；拉起单实例受控 Chromium 后总物理内存 (PSS) 约 350 - 450 MB（宿主 Linux 下约 250 - 350 MB）。Android Termux 环境建议空闲 RAM ≥ 1 GB。

## 技术架构与文档

```
┌────────────────────────────────────────────────────────┐
│               Client (cURL / Python SDK)               │
└───────────────────────────┬────────────────────────────┘
                            │ /v1beta/models/...
┌───────────────────────────▼────────────────────────────┐
│              FastAPI HTTP Gateway (Port 8080)          │
├───────────────────────────┬────────────────────────────┤
│    Account Rotator        │    Chat & Wire Codec       │
│  (Sticky & 429 Isolation) │ (Protobuf-JSON & BotGuard) │
└───────────────────────────┼────────────────────────────┘
                            │ CDP WebSocket
┌───────────────────────────▼────────────────────────────┐
│      Controlled Chromium Instance (CloakBrowser)       │
│       In-Page XHR Replay (withCredentials=true)        │
└───────────────────────────┬────────────────────────────┘
                            │ HTTPS / RPC
┌───────────────────────────▼────────────────────────────┐
│         Google AI Studio (alkalimakersuite-pa)         │
└────────────────────────────────────────────────────────┘
```

详细的设计与协议分析文档：

- [系统架构设计 (ARCHITECTURE.md)](./docs/ARCHITECTURE.md)：分层架构、请求生命周期、Wire Codec 编解码与并发控制模型。
- [Wire 协议逆向技术规范 (WIRE_SPECIFICATION.md)](./docs/WIRE_SPECIFICATION.md)：Protobuf-JSON 数组映射、字段索引、多模态 Part 结构与 Schema 编码。
- [BotGuard 验证链路机制 (BOTGUARD_VERIFICATION_CHAIN.md)](./docs/BOTGUARD_VERIFICATION_CHAIN.md)：WAA 挑战握手、Wasm 动态签名、内容哈希与服务端校验流程。
- [工程协作规则 (AGENTS.md)](./AGENTS.md)：代码结构、凭据与状态保护、协议和验证要求。
- [部署与维护说明](./deploy/README.md)：HTTPS、认证、发布、回滚与排错。

---

## 快速开始

以下命令用于本机开发。首次启动后，在管理界面导入经授权的 Google 登录状态，
再到「系统设置 → API 密钥管理」创建调用密钥。网站登录密码通过
`AISTUDIO_WEB_PASSWORD` 设置，与调用密钥独立。公开服务按[部署说明](deploy/README.md)
使用 HTTPS，并保持后端和浏览器调试端口私有。
仓库只跟踪 `config.example.yaml`；首次使用复制为 `config.yaml`，已有运行配置应保留。

### Android (Termux)

> [!TIP]
> **下载提示**：请务必从 [F-Droid](https://f-droid.org/packages/com.termux/) 或 [GitHub Releases](https://github.com/termux/termux-app/releases) 安装 Termux。**切勿使用 Google Play 商店版本**（Play 商店版本已停止维护，更新环境会报错）。

打开 Termux，依次复制执行以下命令：

```bash
# 步骤 1：更新 Termux 基础环境并安装 git
pkg update -y && pkg install -y git

# 步骤 2：克隆代码仓库并进入目录
git clone https://github.com/ecyasd/aistudio-api.git
cd aistudio-api
cp config.example.yaml config.yaml

# 步骤 3：初始化依赖环境（自动配置手机专属加速源，无需本地编译）
bash scripts/setup-env.sh
uv sync

# 步骤 4：安装并配置受控浏览器（仅首次需要，耗时约 1~2 分钟）
bash scripts/setup-browser.sh

# 步骤 5：启动服务
uv run python main.py server --port 8080
```

启动成功后：
1. 打开手机自带浏览器，访问 **`http://localhost:8080`** 即可进入 Web 管理控制台；
2. 点击 **“账号管理”** 页面，粘贴你的 Google 账号 Cookie 或导入凭据包即可开始调用；
3. **后台保活提示**：下拉手机通知栏，点击 Termux 通知项上的 `Acquire Wakelock`，并在手机系统设置中为 Termux 开启“无限制后台运行/电池优化白名单”，防止锁屏休眠被系统误杀。

---

### Windows

前置要求：电脑已安装 [Python 3.11+](https://www.python.org/downloads/)（安装时勾选 `Add python.exe to PATH`）。

打开 PowerShell 或终端，依次执行：

```bat
# 步骤 1：安装 uv 包管理工具
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 步骤 2：克隆代码仓库并进入目录
git clone https://github.com/ecyasd/aistudio-api.git
cd aistudio-api
Copy-Item config.example.yaml config.yaml

# 步骤 3：同步安装项目依赖
uv sync

# 步骤 4：安装 CloakBrowser 浏览器（仅首次需要）
scripts\install-browser.bat

# 步骤 5：启动服务
uv run python main.py server --port 8080
```

服务启动后，浏览器访问 **`http://localhost:8080`** 进入管理控制台。

---

### Linux / macOS

打开终端，依次执行：

```bash
# 步骤 1：安装 uv 包管理工具
curl -LsSf https://astral.sh/uv/install.sh | sh

# 步骤 2：克隆代码仓库并进入目录
git clone https://github.com/ecyasd/aistudio-api.git
cd aistudio-api
cp config.example.yaml config.yaml

# 步骤 3：同步安装项目依赖
uv sync

# 步骤 4：安装 CloakBrowser 浏览器（仅首次需要）
bash scripts/setup-browser.sh

# 步骤 5：启动服务
uv run python3 main.py server --port 8080
```

服务启动后，浏览器访问 **`http://localhost:8080`** 进入管理控制台。

---

### Docker 部署

从本分支构建包含对应后端、前端与 CloakBrowser 的镜像：

```bash
test -f .env || cp .env.example .env
# Set AISTUDIO_WEB_PASSWORD in .env before starting management access.
docker build -t aistudio-api:local .
docker run -d \
  --name aistudio-api \
  --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v aistudio-api-data:/app/data \
  --mount type=bind,source="$(pwd)/.env",target=/app/.env,readonly \
  aistudio-api:local
```

使用 Docker Compose 启动：

```bash
python3 scripts/prepare_compose_config.py
test -f .env || cp .env.example .env
# Set AISTUDIO_WEB_PASSWORD in .env before starting management access.
docker compose up -d --build
```

首次启动或从旧版 Compose 迁移时，上述工具将原 `config.yaml` 完整复制到
`runtime-config/config.yaml`，保留原文件；没有旧配置时才使用示例。若两份配置已存在且
内容不同，工具拒绝覆盖，需先明确保留哪一份。后续管理界面保存只更新
`runtime-config/config.yaml`，常规重启直接运行 `docker compose up -d`。
Compose 挂载配置父目录，允许原子保存调用密钥和模型设置；不要改回单文件挂载。
`runtime-config/` 与账号数据一样必须保留在版本控制和构建上下文之外。

启动后访问 **`http://localhost:8080`** 进入管理控制台。上游镜像不会自动包含本分支修改；
服务器自动发布使用仓库 CI 生成的代码工件，详见[部署说明](deploy/README.md)。

---

## 配置与环境变量

服务启动后可访问 `http://localhost:8080` 进入 Web 控制台完成可视化管理。

### 环境变量支持

| 变量名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `AISTUDIO_PORT` | int | `8080` | API 服务监听端口 |
| `AISTUDIO_WEB_PASSWORD` | string | `""` | 独立的网站管理密码（亦可设置 `AISTUDIO_ADMIN_PASSWORD`），不授予 API 调用权限 |
| `AISTUDIO_PROXY` | string | `""` | 出口代理地址，支持 `http://`、`https://` 或 `socks5://` |
| `AISTUDIO_DATA_DIR` | string | `data/` | 主数据持久化根目录（Docker 下为 `/app/data`） |
| `AISTUDIO_ACCOUNTS_DIR` | string | `data/accounts` | 账号持久化凭据目录 |
| `AISTUDIO_STATS_FILE` | string | `data/stats.json` | 模型调用与 Token 使用量统计持久化文件 |
| `AISTUDIO_ROTATOR_STATE_FILE` | string | `data/rotator_state.json` | 账号调度与 429 冷却状态持久化文件 |
| `AISTUDIO_CONFIG_FILE` | string | `config.yaml` | 模型规则与默认工具配置文件路径 |
| `AISTUDIO_PERSIST_STATS` | bool | `true` | 是否持久化模型调用与 Token 统计（1=开启，0=关闭） |
| `AISTUDIO_PERSIST_ROTATOR` | bool | `true` | 是否持久化账号 429 冷却与调度状态（1=开启，0=关闭） |
| `AISTUDIO_BROWSER_EXECUTABLE` | string | 自动探测 | 自定义 CloakBrowser 可执行文件路径（Docker 内为 `/opt/cloakbrowser/chrome`） |
| `AISTUDIO_BROWSER_PORT` | int | `9222` | Chromium 远程调试端口 (CDP) |
| `AISTUDIO_BROWSER_HEADLESS` | bool | `true` | 是否以无头模式运行 Chromium |
| `AISTUDIO_BROWSER_IDLE_TIMEOUT` | int | `900` / `1800` | 浏览器空闲自动回收超时秒数（Termux 下默认 900s，桌面端 1800s，0 为禁用） |
| `AISTUDIO_PROOT_NAME` | string | `aistudio-api` | Termux 环境下专用的 proot-distro 容器名称 |
| `AISTUDIO_DEFAULT_TEXT_MODEL` | string | `gemini-3.7-flash` | 默认文本模型 |
| `AISTUDIO_DEFAULT_IMAGE_MODEL` | string | `gemini-3.1-flash-image-preview` | 默认图像生成模型 |
| `AISTUDIO_TIMEZONE` | string | 自动探测 | 受控浏览器指纹与 Wire 协议交互时区（默认 `America/Los_Angeles`） |
| `AISTUDIO_LOCALE` | string | `en-US` | 受控浏览器区域语言 |
| `AISTUDIO_DUMP_REQUESTS` | bool | `false` | 是否在终端转储每次 API 交互报文详情（支持 `DEBUG=1` 触发） |
| `AISTUDIO_DUMP_FILE` | bool | `false` | 是否将请求与响应报文详情落盘保存 |
| `AISTUDIO_DUMP_DIR` | string | `dumps/` | 报文转储保存目录 |
---

## API 调用示例

在管理界面生成调用密钥后，支持以下 API 鉴权方式：
- Query 参数：`?key=YOUR_API_KEY`
- 请求头：`x-goog-api-key: YOUR_API_KEY`、`x-api-key: YOUR_API_KEY` 或 `Authorization: Bearer YOUR_API_KEY`

未配置调用密钥时 API 拒绝访问。管理接口仅接受管理密码的 Bearer 认证；
调用密钥不能读取管理配置、账号或错误记录。Query 密钥用于 Gemini 客户端兼容，
反向代理日志应省略查询字符串。

### cURL 调用

<details>
<summary><b>1. 查询模型列表</b></summary>

```bash
curl http://localhost:8080/v1beta/models \
  -H "x-goog-api-key: your-api-key"
```
</details>

<details>
<summary><b>2. 文本生成 (Non-streaming)</b></summary>

```bash
curl http://localhost:8080/v1beta/models/gemini-3.7-flash:generateContent \
  -H "x-goog-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "用简短的一句话介绍量子计算。"}]
      }
    ]
  }'
```
</details>

<details>
<summary><b>3. 流式生成 (Server-Sent Events)</b></summary>

```bash
curl http://localhost:8080/v1beta/models/gemini-3.7-flash:streamGenerateContent?alt=sse \
  -H "x-goog-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "介绍一下 Python 协程的工作原理。"}]
      }
    ]
  }'
```
</details>

---

### 截断恢复与可选自动续写

上游响应在首段内容发出前意外结束时，会在不主动切号的情况下重试一次；已经发出文字、
思考、签名、工具或图片后不重放。确定的 JSON 语法错误、资源超限和安全阻止不会因此重试。
完整传输及真实完成标志仍是成功条件，部分正文不会被标成正常结束。

模型目录会为支持的文本模型同时列出 `抗截断/原模型名`，例如
`抗截断/gemini-3.1-pro-preview`。刷新客户端模型列表后，选择这个名称即可启用续写；
它共用原模型的账号、额度和统计，并非另一个上游模型。这个前缀会启用续写，即使客户端
同时发送了 `"autoContinue": false`。普通模型名默认不启用额外生成。

两个生成端点也接受请求体顶层扩展 `"autoContinue": true`（或 `auto_continue`），
可在使用普通模型名时单独启用。仅在文本响应真实以 `MAX_TOKENS` 结束时，最多追加两轮续写。
`maxOutputTokens` 是**每轮**上限，合并回答可能超过它；额外轮次会消耗额外请求额度和
Token，返回的用量累计包含各轮输入和输出。普通调用不启用额外生成。
续写轮次合并登记一次成功，错误重试仍登记相应失败和成功事件；面板和账号统计中的
请求数既不等于客户端请求数，也不等于上游实际调用次数。失败或断连时不保证返回
此前各轮的部分用量。

```json
{
  "contents": [{"role": "user", "parts": [{"text": "请写一篇较长的说明。"}]}],
  "tools": [],
  "autoContinue": true,
  "generationConfig": {"maxOutputTokens": 2048}
}
```

续写保持同一账号租约和并发名额，所有轮次共享 `AISTUDIO_TIMEOUT_STREAM` 时间预算；
用于续写的累计片段（含思考、签名及片段开销）最多保留 512 KiB，输入历史估算预算为
8 MiB，达到边界便停止追加请求并保留真实结束原因。文本结果可包含思考与签名；内部追加
请求保留每轮的片段顺序、签名所属片段及对话轮次。工具声明和调用策略保持原样，实际产生
工具调用、图片、代码执行、引用等无法完整回放的内容时停止追加请求；包含工具调用、
代码或多模态片段的输入历史，以及 JSON/结构化输出也不自动续写。不会强制工具调用、
绕过安全阻止，或把断流伪装成正常完成。
模型可能重复部分文字，服务不会猜测删改正文。断连或失败会清理资源并报告错误，不能保证
任意回答完整。因输入预算或不支持回放的内容而停用自动续写时，响应探测会分别记录
`continuation_skipped_budget` 或 `continuation_skipped_unsupported`。

返回给客户端的是合并后的单条回答，标准 Gemini 响应无法表达服务内部插入的续写对话轮次。
因此，跨请求重放完整思考签名历史的聊天客户端应使用普通模型名并自行管理续写；不能保证
将合并回答作为单个带签名的模型轮次再次提交后，上游仍接受这些签名。

官方 Python SDK 可通过
`config={"http_options": {"extra_body": {"autoContinue": True}}, "tools": []}`
将这一扩展传入请求；也可直接设置 `model="抗截断/gemini-3.1-pro-preview"`。
流式和非流式均可使用，HTTP 路径中的模型名为 `models/抗截断/原模型名`。

### Python SDK (google-genai)

使用 Google 官方 `google-genai` SDK 直连本代理服务：

```python
from google import genai

client = genai.Client(
    api_key="your-api-key",
    http_options={
        "api_version": "v1beta",
        "base_url": "http://localhost:8080",
    },
)

# 1. 流式响应
response = client.models.generate_content_stream(
    model="gemini-3.7-flash",
    contents="解释一下什么是反应式编程？",
)
for chunk in response:
    print(chunk.text, end="", flush=True)

# 2. 非流式响应
result = client.models.generate_content(
    model="gemini-3.7-flash",
    contents="用 Python 写一个快速排序算法。",
)
print(result.text)
```

---

### 工具调用 (Function Calling)

```python
from google import genai
from google.genai import types

client = genai.Client(
    api_key="your-api-key",
    http_options={
        "api_version": "v1beta",
        "base_url": "http://localhost:8080",
    },
)

# 定义工具声明
weather_tool = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="get_current_weather",
            description="获取指定地点的实时天气状况",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "location": types.Schema(
                        type="STRING",
                        description="城市或地区名称，例如 'Beijing' 或 'Tokyo'",
                    ),
                    "unit": types.Schema(
                        type="STRING",
                        enum=["celsius", "fahrenheit"],
                        description="温度单位",
                    ),
                },
                required=["location"],
            ),
        )
    ]
)

response = client.models.generate_content(
    model="gemini-3.7-flash",
    contents="今天杭州天气怎么样？",
    config=types.GenerateContentConfig(tools=[weather_tool]),
)

if response.function_calls:
    for call in response.function_calls:
        print(f"触发工具调用: {call.name}")
        print(f"调用参数: {call.args}")
```

---

## Web 管理控制台

系统内置静态打包的 Web 前端管理控制台（访问 `http://localhost:8080`）：

- **仪表盘 (Dashboard)**：实时展示服务健康状况、各模型请求量、成功率及 429 冷却指标。
- **账号管理 (Accounts)**：支持 Cookie 单条与凭据包批量导入、自动探测多账号、手动切换当前活跃账号或重置冷却状态。
- **系统设置 (Settings)**：编辑模型规则并管理 API 密钥。普通模型设置保存保留当前密钥，只有专用密钥管理操作会修改密钥。
- **实时错误 (Real-time Errors)**：页面可见时每 5 秒刷新，显示脱敏错误摘要、分类和请求 ID；数据不写磁盘，重启后清空。
- **访问鉴权**：网站使用独立管理密码；API 使用已配置的调用密钥。Google 登录失效返回 `503 / upstream_auth_expired`，应重新导入 Google 登录状态；调用密钥错误返回 `401 / authentication_error`。

---

## 开发与测试

前端源码位于 `web/`，采用 Vue 3 + TypeScript + Vite + Tailwind CSS。
使用与 CI 一致的 Node.js 24.22.0 和已提交的 npm lock：

```bash
# 启动前端开发调试服务
cd web
npm ci
npm run dev            # 启动开发服务（localhost:3000，代理至 8080）
npm run type-check     # 执行 TypeScript 类型校验
npm run lint           # 执行 ESLint 代码规范检查
npm run test:frontend  # 离线浏览器与页面行为回归
npm run build          # 生产构建，产物输出至 src/aistudio_api/static
```

Python 端质量检查与测试：

```bash
# 执行代码风格与 Lint 检查
uv run ruff check .
# 执行静态类型检查
bun x pyright

# 执行单元测试套件
uv run pytest
```

Android 伴侣端代码质量检查：

```bash
# 执行 Kotlin 代码规范检查 (已配置于 ~/.zshrc)
ktlint "android/app/src/main/java/**/*.kt"
```
