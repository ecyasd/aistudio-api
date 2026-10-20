# aistudio-api

Based on [DeconstructedCube/aistudio-api](https://github.com/DeconstructedCube/aistudio-api).
This fork is maintained at [ecyasd/aistudio-api](https://github.com/ecyasd/aistudio-api).

This fork includes browser startup and first-request recovery, isolated account
sessions, refreshed Cookie checkpointing, persistent API key configuration, and a
real-time error page. Error records stay in memory for ten minutes (up to 100
records), refresh every five seconds while the page is visible, and clear on
restart. A revoked or expired Google login still requires valid credentials to be
reimported; existing API keys remain unchanged. See [deploy/README.md](deploy/README.md)
for HTTPS operations and automated releases. The upstream Docker Hub image does not include this fork's
changes; build this checkout with `docker build -t aistudio-api:local .` to use
the included frontend and backend.

<p align="center">
  <a href="https://github.com/DeconstructedCube/aistudio-api"><img src="https://img.shields.io/badge/API-Gemini%20v1beta-4285F4?style=flat-square&logo=google" alt="Gemini API"></a>
  <a href="https://deepwiki.com/DeconstructedCube/aistudio-api"><img src="https://img.shields.io/badge/DeepWiki-DeconstructedCube%2Faistudio--api-5B68E4?style=flat-square&logo=data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACwAAAAyCAYAAAAnWDnqAAAAAXNSR0IArs4c6QAAA05JREFUaEPtmUtyEzEQhtWTQyQLHNak2AB7ZnyXZMEjXMGeK/AIi+QuHrMnbChYY7MIh8g01fJoopFb0uhhEqqcbWTp06/uv1saEDv4O3n3dV60RfP947Mm9/SQc0ICFQgzfc4CYZoTPAswgSJCCUJUnAAoRHOAUOcATwbmVLWdGoH//PB8mnKqScAhsD0kYP3j/Yt5LPQe2KvcXmGvRHcDnpxfL2zOYJ1mFwrryWTz0advv1Ut4CJgf5uhDuDj5eUcAUoahrdY/56ebRWeraTjMt/00Sh3UDtjgHtQNHwcRGOC98BJEAEymycmYcWwOprTgcB6VZ5JK5TAJ+fXGLBm3FDAmn6oPPjR4rKCAoJCal2eAiQp2x0vxTPB3ALO2CRkwmDy5WohzBDwSEFKRwPbknEggCPB/imwrycgxX2NzoMCHhPkDwqYMr9tRcP5qNrMZHkVnOjRMWwLCcr8ohBVb1OMjxLwGCvjTikrsBOiA6fNyCrm8V1rP93iVPpwaE+gO0SsWmPiXB+jikdf6SizrT5qKasx5j8ABbHpFTx+vFXp9EnYQmLx02h1QTTrl6eDqxLnGjporxl3NL3agEvXdT0WmEost648sQOYAeJS9Q7bfUVoMGnjo4AZdUMQku50McDcMWcBPvr0SzbTAFDfvJqwLzgxwATnCgnp4wDl6Aa+Ax283gghmj+vj7feE2KBBRMW3FzOpLOADl0Isb5587h/U4gGvkt5v60Z1VLG8BhYjbzRwyQZemwAd6cCR5/XFWLYZRIMpX39AR0tjaGGiGzLVyhse5C9RKC6ai42ppWPKiBagOvaYk8lO7DajerabOZP46Lby5wKjw1HCRx7p9sVMOWGzb/vA1hwiWc6jm3MvQDTogQkiqIhJV0nBQBTU+3okKCFDy9WwferkHjtxib7t3xIUQtHxnIwtx4mpg26/HfwVNVDb4oI9RHmx5WGelRVlrtiw43zboCLaxv46AZeB3IlTkwouebTr1y2NjSpHz68WNFjHvupy3q8TFn3Hos2IAk4Ju5dCo8B3wP7VPr/FGaKiG+T+v+TQqIrOqMTL1VdWV1DdmcbO8KXBz6esmYWYKPwDL5b5FA1a0hwapHiom0r/cKaoqr+27/XcrS5UwSMbQAAAABJRU5ErkJggg==" alt="DeepWiki"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/Framework-FastAPI-009688?style=flat-square&logo=fastapi" alt="FastAPI"></a>
  <a href="https://vuejs.org/"><img src="https://img.shields.io/badge/Frontend-Vue%203%20%2B%20Vite-4FC08D?style=flat-square&logo=vuedotjs" alt="Vue 3"></a>
  <a href="https://docs.astral.sh/uv/"><img src="https://img.shields.io/badge/Python-3.11%2B%20%7C%20uv-261230?style=flat-square&logo=python" alt="Python uv"></a>
</p>

<p align="center">
  Google AI Studio reverse proxy gateway that exposes native Google Gemini API interfaces.
</p>

<p align="center">
  <a href="./README.md">中文文档</a>
</p>

---

## Table of Contents

- [Features](#features)
- [Architecture & Documentation](#architecture--documentation)
- [Quick Start](#quick-start)
  - [Android (Termux)](#android-termux)
  - [Windows](#windows)
  - [Linux / macOS](#linux--macos)
  - [Docker Deployment](#docker-deployment)
- [Configuration & Environment Variables](#configuration--environment-variables)
- [API Usage Examples](#api-usage-examples)
  - [cURL](#curl)
  - [Python SDK (google-genai)](#python-sdk-google-genai)
  - [Function Calling](#function-calling)
- [Web Management Console](#web-management-console)
- [Development & Testing](#development--testing)

---

## Features

| Feature | Description |
|---|---|
| **Gemini API Compatibility** | Supports `/v1beta/...` endpoints including Thinking, multimodal inputs, function calling, and image generation |
| **Dynamic Model Discovery** | Automatically synchronizes and discovers available models from upstream Google AI Studio |
| **Multi-Account Rotation** | Manages multiple accounts with automatic failover, tracks 429 rate limits per model independently, and resets quotas at midnight Pacific Time |
| **Runtime State Persistence** | Model usage statistics (`stats.json`) and account 429 cooldown & quota status (`rotator_state.json`) are automatically persisted to disk across restarts |
| **Downstream Parameter Fault Tolerance** | `drop_unsupported_params` automatically drops unsupported safety categories (e.g. `HARM_CATEGORY_CIVIC_INTEGRITY`) and tools to prevent 400 errors |
| **Multi-Account Probing** | Extracts and verifies sub-accounts (`u/0`, `u/1`...) automatically when importing a multi-session Cookie |
| **Built-in Tools** | Supports official tools such as Google Search, Google Maps, and Code Execution sandbox |
| **Pure Python CDP** | Direct Chrome DevTools Protocol communication over asynchronous WebSockets without Node.js, Playwright, or Selenium |
| **Web Management Console** | Built-in web dashboard for account management (supporting text and JSON bundle file imports), live request metrics, and runtime `config.yaml` hot reloading |
| **Separate Credentials** | Management passwords and API calling keys are independent; model-setting saves and account reimports or activation preserve existing API keys |
| **Real-time Errors** | `/error-records` shows sanitized errors held only in memory for ten minutes, up to 100 entries; restarting clears them |
| **Android Companion App** | Native Android app (`android/`) built with Jetpack Compose, featuring isolated multi-profile management, on-demand WebView, Bitwarden autofill, and one-click bundle export |
> [!NOTE]
> **Memory Footprint**: The standalone Python backend consumes ~35 - 45 MB RAM. With a single controlled Chromium instance, total actual system memory (PSS) is ~350 - 450 MB on Termux proot (~250 - 350 MB on native Linux). On Android Termux, ≥ 1 GB free RAM is recommended.

## Architecture & Documentation

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

Technical reference documentation:

- [System Architecture Specification (ARCHITECTURE.md)](./docs/ARCHITECTURE.md): Layered architecture, request lifecycle, wire codec, and concurrency model.
- [Wire Protocol Reverse Engineering Specification (WIRE_SPECIFICATION.md)](./docs/WIRE_SPECIFICATION.md): Protobuf-JSON array mappings, field indices, multimodal Part structures, and Schema encoding.
- [BotGuard Verification Chain (BOTGUARD_VERIFICATION_CHAIN.md)](./docs/BOTGUARD_VERIFICATION_CHAIN.md): WAA challenge handshake, dynamic Wasm signing, content hashing, and upstream validation.
- [Engineering Rules (AGENTS.md)](./AGENTS.md): Code layout, state protection, protocol requirements, and verification.
- [Deployment Runbook](./deploy/README.md): HTTPS, authentication, releases, rollback, and troubleshooting.

---

## Quick Start

These examples are for local development. Import an authorized Google session in
the management console, then create a calling key in **Settings → API keys**.
Set the independent management password through `AISTUDIO_WEB_PASSWORD`.
For public access, follow the [deployment runbook](deploy/README.md), use HTTPS,
and keep backend and browser debugging ports private.
Only `config.example.yaml` is tracked. Copy it to `config.yaml` for a new checkout;
preserve an existing runtime configuration.

### Android (Termux)

> [!TIP]
> **Download Notice**: Always install Termux from [F-Droid](https://f-droid.org/packages/com.termux/) or [GitHub Releases](https://github.com/termux/termux-app/releases). **Do NOT install from the Google Play Store** (the Play Store build is obsolete and will produce repository update errors).

Open Termux and run the following commands step by step:

```bash
# Step 1: Update Termux environment and install git
pkg update -y && pkg install -y git

# Step 2: Clone repository and enter directory
git clone https://github.com/ecyasd/aistudio-api.git
cd aistudio-api
cp config.example.yaml config.yaml

# Step 3: Initialize Python environment & sync dependencies (auto-resolves prebuilt Android wheels)
bash scripts/setup-env.sh
uv sync

# Step 4: Setup managed CloakBrowser runtime (first run only, takes ~1-2 min)
bash scripts/setup-browser.sh

# Step 5: Start server
uv run python main.py server --port 8080
```

Once started:
1. Open your phone's browser and go to **`http://localhost:8080`** to access the Web Console;
2. Navigate to **Accounts**, paste your Google account Cookies or import a bundle to start using the API;
3. **Background Persistence Tip**: Pull down Android's notification bar, tap `Acquire Wakelock` on Termux's notification, and disable battery optimization for Termux in phone system settings to prevent sleep kills.

---

### Windows

Prerequisite: [Python 3.11+](https://www.python.org/downloads/) installed (check `Add python.exe to PATH` during setup).

Open PowerShell or Windows Terminal:

```bat
# Step 1: Install uv package manager
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Step 2: Clone repository and enter directory
git clone https://github.com/ecyasd/aistudio-api.git
cd aistudio-api
Copy-Item config.example.yaml config.yaml

# Step 3: Sync dependencies
uv sync

# Step 4: Install CloakBrowser (first run only)
scripts\install-browser.bat

# Step 5: Start server
uv run python main.py server --port 8080
```

Open **`http://localhost:8080`** in your browser to access the Web Console.

---

### Linux / macOS

Open a terminal and run:

```bash
# Step 1: Install uv package manager
curl -LsSf https://astral.sh/uv/install.sh | sh

# Step 2: Clone repository and enter directory
git clone https://github.com/ecyasd/aistudio-api.git
cd aistudio-api
cp config.example.yaml config.yaml

# Step 3: Sync dependencies
uv sync

# Step 4: Install CloakBrowser (first run only)
bash scripts/setup-browser.sh

# Step 5: Start server
uv run python3 main.py server --port 8080
```

Open **`http://localhost:8080`** in your browser to access the Web Console.

---

### Docker Deployment

Build this fork's backend, frontend, and CloakBrowser into a local image:

```bash
docker build -t aistudio-api:local .
docker run -d \
  --name aistudio-api \
  --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v aistudio-api-data:/app/data \
  aistudio-api:local
```

Or run with Docker Compose:

```bash
test -f config.yaml || cp config.example.yaml config.yaml
test -f .env || cp .env.example .env
docker compose up -d --build
```

Open **`http://localhost:8080`** in your browser to access the Web Console.

Upstream images do not automatically include this fork's changes. Automated
server releases use this repository's CI artifacts; see the
[deployment runbook](deploy/README.md).

---

## Configuration & Environment Variables

Open `http://localhost:8080` in your browser to access the Web Console.

### Environment Variables
| Variable | Type | Default | Description |
|---|---|---|---|
| `AISTUDIO_PORT` | int | `8080` | HTTP service listening port |
| `AISTUDIO_WEB_PASSWORD` | string | `""` | Independent management password (also checks `AISTUDIO_ADMIN_PASSWORD`); does not authorize API calls |
| `AISTUDIO_PROXY` | string | `""` | Outbound proxy URL (`http://`, `https://`, or `socks5://`) |
| `AISTUDIO_DATA_DIR` | string | `data/` | Main data directory for persistent files (`/app/data` in Docker) |
| `AISTUDIO_ACCOUNTS_DIR` | string | `data/accounts` | Directory for storing account credentials |
| `AISTUDIO_STATS_FILE` | string | `data/stats.json` | Persistent storage file for model usage metrics |
| `AISTUDIO_ROTATOR_STATE_FILE` | string | `data/rotator_state.json` | Persistent storage file for account 429 cooldowns and dispatch states |
| `AISTUDIO_CONFIG_FILE` | string | `config.yaml` | Path to model rules and default tools configuration YAML |
| `AISTUDIO_PERSIST_STATS` | bool | `true` | Persist request and token usage metrics across service restarts |
| `AISTUDIO_PERSIST_ROTATOR` | bool | `true` | Persist account 429 rate limit dates & cooldowns across restarts |
| `AISTUDIO_BROWSER_EXECUTABLE` | string | Auto | Path to CloakBrowser executable (`/opt/cloakbrowser/chrome` in Docker) |
| `AISTUDIO_BROWSER_PORT` | int | `9222` | Chromium remote debugging port (CDP) |
| `AISTUDIO_BROWSER_HEADLESS` | bool | `true` | Run Chromium in headless mode |
| `AISTUDIO_BROWSER_IDLE_TIMEOUT` | int | `900` / `1800` | Browser idle reclamation timeout in seconds (900s on Termux, 1800s on Desktop; 0 disables) |
| `AISTUDIO_PROOT_NAME` | string | `aistudio-api` | Dedicated proot-distro container name for Termux |
| `AISTUDIO_DEFAULT_TEXT_MODEL` | string | `gemini-3.7-flash` | Default text model |
| `AISTUDIO_DEFAULT_IMAGE_MODEL` | string | `gemini-3.1-flash-image-preview` | Default image generation model |
| `AISTUDIO_TIMEZONE` | string | Auto | Browser fingerprint & Wire protocol timezone (`America/Los_Angeles` by default) |
| `AISTUDIO_LOCALE` | string | `en-US` | Browser fingerprint locale |
| `AISTUDIO_DUMP_REQUESTS` | bool | `false` | Dump API request/response exchanges to console logs (enabled via `DEBUG=1`) |
| `AISTUDIO_DUMP_FILE` | bool | `false` | Dump request/response exchanges to individual disk files |
| `AISTUDIO_DUMP_DIR` | string | `dumps/` | Target directory for dumped exchange files |

---

## API Usage Examples

After creating a calling key in the management console, supported API authentication methods are:
- Query param: `?key=YOUR_API_KEY`
- Header: `x-goog-api-key: YOUR_API_KEY`, `x-api-key: YOUR_API_KEY`, or `Authorization: Bearer YOUR_API_KEY`

API access fails closed when no calling keys are configured. Management endpoints
accept the management password through Bearer authentication only; calling keys
cannot read management configuration, accounts, or error records. Query keys
remain supported for Gemini clients; omit query strings from reverse proxy logs.

### cURL

<details>
<summary><b>1. List Models</b></summary>

```bash
curl http://localhost:8080/v1beta/models \
  -H "x-goog-api-key: your-api-key"
```
</details>

<details>
<summary><b>2. Generate Content (Non-streaming)</b></summary>

```bash
curl http://localhost:8080/v1beta/models/gemini-3.7-flash:generateContent \
  -H "x-goog-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "Explain quantum computing in one sentence."}]
      }
    ]
  }'
```
</details>

<details>
<summary><b>3. Stream Generate Content (SSE)</b></summary>

```bash
curl http://localhost:8080/v1beta/models/gemini-3.7-flash:streamGenerateContent?alt=sse \
  -H "x-goog-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "Explain how Python asyncio works."}]
      }
    ]
  }'
```
</details>

---

### Incomplete responses and optional continuation

An upstream response that ends before any response Part is sent receives one retry without
forced account rotation. Once text, thinking, a signature, a tool call or an image has been sent, the
request is never replayed. Definite JSON syntax errors, resource limits and safety stops do not trigger
this recovery. Success still requires complete transport and a real completion marker.

The model catalog lists `抗截断/base-model` variants for supported text models, such as
`抗截断/gemini-3.1-pro-preview`. Refresh the client model list and select a variant to enable
continuation. It shares the original model's account, quota and statistics. Selecting the
variant enables continuation even if the client also sends `"autoContinue": false`.
Ordinary model names default to no extra generation.

Both generation endpoints also accept the top-level JSON extension `"autoContinue": true`
(or `auto_continue`) to opt in while using an ordinary model name. A text response ending
with the actual `MAX_TOKENS` reason can receive at most two extra generation rounds.
`maxOutputTokens` applies **per round**; the combined answer can exceed it. Extra rounds
consume additional request quota and tokens. Returned usage sums all rounds, including
their repeated input. Ordinary requests do not enable extra generation.
Dashboard and account counters record one successfully completed client request, which is
not the number of upstream calls. Failures or disconnects may omit earlier rounds' partial usage.

```json
{
  "contents": [{"role": "user", "parts": [{"text": "Write a long explanation."}]}],
  "tools": [],
  "autoContinue": true,
  "generationConfig": {"maxOutputTokens": 2048}
}
```

All rounds share one account lease, inference slot and `AISTUDIO_TIMEOUT_STREAM` time
budget. Continuation retains at most 512 KiB of Parts, including thinking, signatures and
per-Part overhead, and uses an estimated 1 MiB input
history budget; reaching a bound stops extra requests and retains the real finish reason.
Text results may include thinking and signatures. Internal continuation requests preserve
Part order, each signature's owning Part and each actual response turn. Tool declarations
and policies remain unchanged. Actual tool calls, images, code, citations or other content
that cannot be fully replayed stop extra rounds. Input history containing tool calls, code
or multimodal Parts, and JSON/structured output, are also excluded. Continuation never
forces tool use, bypasses safety stops or disguises broken transport as normal completion.
The model may repeat text; the service does not guess which text to remove. Disconnects and failures
clean up resources and report errors. Complete answers cannot be guaranteed.

The client receives one combined answer. A standard Gemini response cannot represent the
internal continuation conversation turns. Chat clients that replay complete signed thinking
history across requests should use ordinary model names and manage continuation themselves.
Replaying the combined answer as one signed model turn is not guaranteed to pass upstream
signature validation.

With the official Python SDK, pass
`config={"http_options": {"extra_body": {"autoContinue": True}}, "tools": []}`
to opt in, or set `model="抗截断/gemini-3.1-pro-preview"` directly. Both streaming and
non-streaming generation work; the HTTP model path is `models/抗截断/base-model`.

### Python SDK (google-genai)

Using the official `google-genai` Python SDK:

```python
from google import genai

client = genai.Client(
    api_key="your-api-key",
    http_options={
        "api_version": "v1beta",
        "base_url": "http://localhost:8080",
    },
)

# 1. Streaming response
response = client.models.generate_content_stream(
    model="gemini-3.7-flash",
    contents="What is reactive programming?",
)
for chunk in response:
    print(chunk.text, end="", flush=True)

# 2. Non-streaming response
result = client.models.generate_content(
    model="gemini-3.7-flash",
    contents="Write a quicksort implementation in Python.",
)
print(result.text)
```

---

### Function Calling

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

# Tool declaration
weather_tool = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="get_current_weather",
            description="Get real-time weather conditions for a given location",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "location": types.Schema(
                        type="STRING",
                        description="City name, e.g. 'San Francisco' or 'Tokyo'",
                    ),
                    "unit": types.Schema(
                        type="STRING",
                        enum=["celsius", "fahrenheit"],
                        description="Temperature unit",
                    ),
                },
                required=["location"],
            ),
        )
    ]
)

response = client.models.generate_content(
    model="gemini-3.7-flash",
    contents="What is the weather in Tokyo right now?",
    config=types.GenerateContentConfig(tools=[weather_tool]),
)

if response.function_calls:
    for call in response.function_calls:
        print(f"Tool called: {call.name}")
        print(f"Arguments: {call.args}")
```

---

## Web Management Console

The service includes a built-in static Web Console served at `http://localhost:8080`:

- **Dashboard**: Real-time view of service health, per-model request stats, success rates, and rate-limit cooldown status.
- **Accounts**: Import Cookie credentials (single text or bundle files), auto-probe multiple accounts, switch the active account, or reset rate limits.
- **Settings**: Edit model rules and manage API keys. Ordinary model-setting saves preserve current keys; only dedicated key management changes them.
- **Real-time Errors**: Refreshes every five seconds while visible, showing sanitized summaries, categories, and request IDs. Records are never written to disk and clear on restart.
- **Access Control**: Management passwords and API calling keys are independent. Expired Google sessions return `503 / upstream_auth_expired` and require a fresh Google-session import; invalid calling keys return `401 / authentication_error`.

---

## Development & Testing

Frontend code is located in `web/` (Vue 3 + TypeScript + Vite + Tailwind CSS).
Use Node.js 24.22.0 and the committed npm lock, matching CI:

```bash
cd web
npm ci
npm run dev            # Start local Vite dev server (localhost:3000 -> 8080)
npm run type-check     # Run TypeScript type check
npm run lint           # Run ESLint check
npm run test:frontend  # Offline browser and page behavior regressions
npm run build          # Build production assets into src/aistudio_api/static
```

Python quality checks and tests:

```bash
# Run code style & lint check
uv run ruff check .
# Run static type checking
bun x pyright

# Run test suite
uv run pytest
```

Android companion app code style checks:

```bash
# Run Kotlin code style check (configured via ~/.zshrc)
ktlint "android/app/src/main/java/**/*.kt"
```
