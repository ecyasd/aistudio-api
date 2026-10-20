# aistudio-api 系统架构设计

本文档说明 `aistudio-api` 的模块职责、请求生命周期和资源约束。实现与离线回归测试是行为依据；运行配置、账号数据和发布记录不属于源代码。

---

## 目录

- [1. 整体架构与分层设计](#1-整体架构与分层设计)
- [2. 核心模块与职责划分](#2-核心模块与职责划分)
- [3. 请求生命周期与执行时序](#3-请求生命周期与执行时序)
- [4. 并发控制与高可用设计](#4-并发控制与高可用设计)
- [5. 跨平台支持与依赖隔离](#5-跨平台支持与依赖隔离)
- [6. 修改入口与验证](#6-修改入口与验证)

---

## 1. 整体架构与分层设计

系统采用分层设计（Layered Architecture），划分为 **API 接入层**、**Application 应用服务层**、**Domain 领域层** 与 **Infrastructure 基础设施层**：

```mermaid
flowchart TD
    Client["客户端调用方<br/>(cURL / Python SDK / Web 控制台)"]

    subgraph APILayer ["1. API 接入与路由层 (FastAPI)"]
        RoutesGemini["routes_gemini.py<br/>Gemini v1beta 兼容接口"]
        RoutesModels["routes_models.py<br/>模型发现与能力查询"]
        RoutesAccounts["routes_accounts.py<br/>账号与 Cookie 管理"]
        RoutesSystem["routes_system.py<br/>系统监控与配置热重载"]
    end

    subgraph AppLayer ["2. Application 应用服务层"]
        APISvc["api_service_gemini.py<br/>请求生命周期与流式处理"]
        Orchestrator["account_orchestrator.py<br/>_switch_lock 防雪崩切号编排"]
        ChatSvc["chat_service.py<br/>多模态请求规范化"]
        AccountRotator["account_rotator.py<br/>模型级 Sticky 调度与 429 隔离"]
        AccountSvc["account_service.py<br/>账号激活与存储用例封装"]
    end

    subgraph DomainLayer ["3. Domain 领域层"]
        Models["models.py<br/>纯净领域模型 (Candidate, ModelOutput)"]
        Errors["errors.py<br/>统一异常体系 (AuthError, UsageLimitExceeded)"]
    end

    subgraph InfraLayer ["4. Infrastructure 基础设施层"]
        subgraph GatewaySub ["协议网关与编解码 (gateway/)"]
            GatewayClient["client.py<br/>AIStudioClient 统一门面"]
            Session["session.py<br/>BrowserSession 协调与蓝绿管理"]
            SessionState["session_state.py<br/>SessionState 状态机与就绪门"]
            SessionAuth["session_auth.py<br/>Cookie 清洗与账号身份校验"]
            BotGuardBootstrap["botguard_bootstrap.py<br/>WAA 探针握手与基线模板捕获"]
            CaptureSvc["capture.py<br/>请求模板内存改写与单例"]
            WireCodec["wire_codec.py<br/>Protobuf-JSON 请求构造与重写"]
            WireParser["wire_parser.py<br/>Protobuf-JSON 响应解析器"]
            StreamParser["stream_parser.py<br/>增量流式 JSON 状态机解析器"]
            StreamingGateway["streaming.py<br/>增量 SSE 流式管道调度"]
            Transport["transport.py<br/>CDP Native Binding 推送传输与反压"]
            ReplaySvc["replay.py<br/>页面内 XHR 重放服务"]
            ModelDisc["model_discovery.py<br/>动态模型发现与元数据获取"]
            ModelDefaults["model_defaults.py<br/>配置解析与 mtime 内存缓存"]
            WireTypes["wire_types.py<br/>对话内容与 Part 实体契约"]
        end
        subgraph BrowserSub ["浏览器与 CDP 通信 (browser/)"]
            CDPClient["cdp_client.py<br/>纯 Python 异步 WebSocket CDP 客户端"]
            BrowserEngine["browser_engine.py<br/>CloakBrowser 探测、Windows Job Object 与进程管理"]
            Scripts["scripts.py<br/>Fetch + ReadableStream 与 DOM GC 脚本加载"]
        end
        subgraph AccountSub ["账号凭据与持久化 (account/)"]
            AccountStore["account_store.py<br/>原子紧凑 JSON 文件凭据库"]
            CookieParser["cookie_parser.py<br/>Cookie 解析、Hash 计算与探活"]
            CookieRefresher["cookie_refresher.py<br/>Cookie 格式整理与会话刷新"]
        end
    end

    subgraph Upstream ["5. Google 上游服务"]
        AIStudio["Google AI Studio<br/>alkalimakersuite-pa"]
        Waa["Google WAA 反作弊网关<br/>waa-pa"]
    end

    Client --> APILayer
    APILayer --> AppLayer
    AppLayer --> DomainLayer
    AppLayer --> InfraLayer
    InfraLayer --> DomainLayer
    BrowserSub --> Upstream
    GatewaySub --> Upstream
```
---

## 2. 核心模块与职责划分

### 2.1 API 接入层 (`src/aistudio_api/api/`)

| 文件 | 核心职责 |
|---|---|
| `routes_gemini.py` | 对外暴露 `/v1beta/models/{model}:generateContent` 和 `:streamGenerateContent` 端点 |
| `routes_models.py` | 动态向上游拉取并缓存可用模型列表（如 `gemini-3.7-flash`、`gemini-3.8-flash` 等） |
| `routes_accounts.py` | 提供 Cookie 单条/Bundle文件导入、多账号递归探活（`u/0`, `u/1`...）、手动激活与删除接口 |
| `routes_system.py` | 公开最小健康状态、受管理认证保护的监控指标与配置接口 |
| `dependencies.py` | 分离管理密码和模型调用密钥；模型支持 Query 参数 `?key=`、Header `x-goog-api-key`、`x-api-key` 或 `Bearer`，空调用密钥配置拒绝请求 |

### 2.2 应用服务层 (`src/aistudio_api/application/`)

| 文件 | 核心职责 |
|---|---|
| `chat_service.py` | 将客户端提交的 Gemini 请求转换为内部结构，处理 Base64 媒体、系统提示与工具参数声明 |
| `account_rotator.py` | 负责多账号的 Sticky 黏性调度，按模型维护 429 限流与 403 鉴权异常隔离状态，每日美西午夜重置 |
| `account_orchestrator.py` | 提供全局防雪崩互斥锁（`_switch_lock`），在并发 429 与 403 异常时实现安全有序故障转移切号与单账号在位自愈 |
| `account_service.py` | 账号领域用例（Cookie 保存、批量探活导入、凭据激活）封装，杜绝路由层穿透访问存储私有属性 |
| `api_service_gemini.py` | 编排请求全生命周期，协调捕获模板、签名、传输重放及 SSE 流式响应 |
| `model_selection.py` | 将 `抗截断/<base>` 选择器映射为原模型，保持账号准入、冷却、模板与统计共用原模型身份 |
| `continuation.py` | 仅在真实 `MAX_TOKENS` 完成后执行有界续写；保留每轮 Part、签名和对话边界，累计用量 |
| `inference_limits.py` | 限制推理并发、等待队列和排队时长；取消及退出时释放名额 |

### 2.3 领域模型层 (`src/aistudio_api/domain/`)

| 文件 | 核心职责 |
|---|---|
| `models.py` | 纯净领域数据结构定义（`Candidate`, `ModelOutput`, `GeneratedImage`），杜绝外部协议与 Protobuf 数组下标耦合 |
| `errors.py` | 统一异常体系定义（`SessionExpiredError`, `AuthError`, `UsageLimitExceeded`, `RequestError` 等） |

### 2.4 基础设施层 (`src/aistudio_api/infrastructure/`)

- **浏览器与 CDP 子系统 (`browser/`)**：
  - `cdp_client.py`：Chrome DevTools Protocol 异步客户端，负责 WebSocket 通信、网络过滤与目标管理。命令发送和回包共享期限；连接关闭时解除等待并回收异步事件任务。
  - `browser_engine.py`：Chromium 路径探测、参数组装与进程/端口生命周期管理（支持 Win32 API 与 Linux 容器环境）。
  - `scripts.py`：浏览器端注入脚本加载器。
  - `js/`：浏览器端独立脚本，包含 Hook 注入、流式通信、快照签名与内存清理。
- **网关与编解码子系统 (`gateway/`)**：
  - `client.py`：`AIStudioClient` 统一门面，组装请求重放、流式生成与会话生命周期。
  - `session.py`：`BrowserSession` 协调核心，管理会话生命周期、蓝绿标签页平滑热切及快照签名生成。
  - `session_state.py`：会话状态机（`SessionState`）、就绪门（`SessionReadinessGate`）及上下文描述符（`SessionContext`），支持并发请求阻塞等待与瞬时唤醒。
  - `session_auth.py`：Cookie 过滤清洗（剔除 `OSID`、`SIDCC` 冲突项）、登录页重定向感知、邮箱身份探测与凭据原子落盘。
  - `botguard_bootstrap.py`：BotGuard WAA 握手、基础请求模板拦截与临时会话管理。
  - `wire_spec.py`：Protobuf-over-JSON (JSPB) 字段索引枚举与 `JspbArray` 稀疏容器定义。
  - `wire_codec.py`：JSPB 请求结构组包、参数动态改写与内置工具注入。
  - `wire_parser.py`：JSPB 响应解析器，处理 Struct 结构体、ListValue 数组与状态码映射；合法元数据块中的默认状态不抹除已取得的完成原因。
  - `wire_types.py`：领域数据结构契约（`AistudioContent`、`AistudioPart`、`AistudioGenerationConfig`）。
  - `stream_parser.py`：流式 JSON 增量状态机解析器，去除 XSSI 前缀并按括号深度切片分块。
  - `transport.py`：基于 CDP Binding 的事件推送管道，支持队列反压控制与时间戳签名注入。
  - `capture.py`：请求模板缓存与纯内存模型适配。
  - `replay.py`：页面上下文 XHR 请求重放与异常降级。
  - `streaming.py`：流式响应分发与异常映射。
  - `model_discovery.py`：可用模型列表动态探测与本地降级。
  - `model_defaults.py`：模型默认规则解析与 mtime 驱动的内存缓存。
- **账号与持久化子系统 (`account/`)**：
  - `account_store.py`：串行读改写注册表，仅在原子落盘成功后发布内存快照；注册表不可读或格式错误时拒绝写入，避免当作空账号覆盖。账号写入失败时恢复本次涉及的凭据与元数据文件，恢复过程与会话检查点共用文件锁；不保证断电或持续存储故障下的跨文件事务。
  - `cookie_parser.py`：Cookie 格式解析、SAPISIDHASH 计算与多子账号探测。
  - `cookie_refresher.py`：Cookie 凭据规整与会话保活刷新。

## 3. 请求生命周期与执行时序

从客户端请求到获取流式响应的端到端调用时序：

```mermaid
sequenceDiagram
    autonumber
    actor Client as 客户端调用方
    participant API as FastAPI 路由层
    participant Rotator as AccountRotator
    participant APISvc as api_service_gemini
    participant Facade as AIStudioClient
    participant Capture as RequestCaptureService
    participant Session as BrowserSession
    participant Codec as WireCodec
    participant Transport as XHRStreamTransport
    participant Page as Chromium (CDP)
    participant Google as Google AI Studio (alkali)

    Client->>API: POST /v1beta/models/gemini-3.7-flash:streamGenerateContent
    API->>APISvc: handle_gemini_generate_content(stream=True)
    APISvc->>APISvc: 解析选择器为原模型，检查请求并取得并发名额
    APISvc->>Rotator: 获取原模型可用账号 (Sticky 检查)
    Rotator-->>APISvc: 返回可用账号，取得其上下文租约
    APISvc->>Facade: stream_generate_content(...)
    
    Facade->>Capture: capture_request(prompt, model, images...)
    opt 会话未就绪或模板缺失
        Capture->>Session: ensure_ready() / capture_template(model)
        Session->>Page: 握手并截获通用基础请求模板
        Page-->>Session: 截断生成并返回基线模板 (URL, Headers, Wire结构)
        Session-->>Capture: 纯内存根据目标 model 动态改写模板
    end

    Capture->>Session: generate_snapshot(contents)
    Session->>Page: default_MakerSuite[snapKey](service, contentHash)
    Page-->>Session: 返回本轮内容对应的签名
    Capture->>Codec: modify_body(template, prompt, snapshot...)
    Codec-->>Capture: 返回最终组装的 Wire JSON Payload
    Capture-->>Facade: 返回 CapturedRequest

    Facade->>Transport: send_streaming_request(page, url, headers, body)
    Transport->>Page: evaluate(STREAMING_INIT_JS) -> window.fetch(credentials='include')
    Page->>Google: 发送携带 Cookie 与 X-Goog-AuthUser 的 POST 请求
    Google-->>Page: 分块推送数据流 (ReadableStream getReader)
    Page-->>Transport: CDP Native Binding (__aistudio_stream_push__) 实时推入 Queue
    Transport-->>Facade: 异步迭代 yield ('chunk', bytes)
    Facade-->>APISvc: 增量解析 candidate (wire_parser)
    APISvc-->>Client: SSE 流式分发 (data: {"candidates": [...]})
    Note over APISvc,Facade: 抗截断模式仅在真实 MAX_TOKENS 后最多追加两轮；共用原租约和总期限
    
    Note over Session,Page: 【流结束/异常后置清理】
    Session->>Page: 执行 DOM_GC_CLEANUP_JS 与 collect_garbage() (V8 GC)
```
---

## 4. 并发控制与高可用设计

### 4.1 单进程受控 Chromium 架构

- 全局维持 **单个受控 Chromium 进程**，避免多实例多进程导致的内存膨胀。
- 启动限制参数：`--renderer-process-limit=1`、`--js-flags=--max-old-space-size=128 --expose-gc`、`--disable-gpu`。
- 并发请求通过 CDP 客户端在同一个浏览器页面上下文内以轻量 `Fetch + ReadableStream` 执行，并由 `DOM_GC_CLEANUP_JS` 与 V8 原生垃圾回收控制内存占用。

### 4.2 模型独立限流、403 鉴权隔离与 Sticky 调度

- **模型级配额隔离**：各账号针对不同模型（如 `gemini-3.7-flash`、`gemini-3.8-flash`）的 429 状态独立记录，单模型额度耗尽不影响其他模型的正常调用。
- **403 鉴权异常快速隔离**：当账号遭遇 `The caller does not have permission` (403) 异常时，调度器立即对该账号设置短时隔离（`auth_cooldown`），优先调度至健康备用账号。
- **Sticky 黏性调度**：默认保持当前活跃账号，直到该账号针对当前模型遭遇 429 限流或 403 异常时，才自动顺延切换至下一个健康账号。
- **并发结果归属**：成功和失败归属请求实际使用的账号；旧请求晚到的成功结果不得清除其他在途请求刚设置的有效冷却。
- **自动配额重置**：按 `America/Los_Angeles` 当地午夜重置 RPD 计数，自动处理夏令时；对应北京时间会随季节变化。成功、限流及其他失败均记入同一当地日期的请求计数，账号统计页面也刷新未再次调用模型的旧日计数；累计总数与登录态隔离保持独立。

### 4.3 防切号雪崩互斥锁 (`_switch_lock`) 与自愈

当多个并发协程同时遭遇 429 限流或 403 权限异常时：
1. 率先获得 `_switch_lock` 的协程执行实质性切号与页面上下文刷新；
2. 后续排队获得锁的协程通过双重检查（Double-Checked Locking），识别到账号已被切换至健康账号且对当前模型可用，直接复用重试，避免并发异常导致多次无谓切号；
3. 需要会话恢复时，在满足错误类型和在途租约约束后刷新模板或上下文。Google 已撤销或过期的凭据需要重新提供，代码刷新无法恢复其有效性。
### 4.4 蓝绿会话平滑热切与就绪门机制 (Blue-Green Session Transition)

针对多账号并发场景，系统实现了无锁化原子就绪门（`SessionReadinessGate`）与蓝绿标签页热切：
1. **独立新标签页隔离初始化**：当触发账号轮换时，网关在后台并发创建新 CDP 标签页，注入新账号 Cookie，导航至目标子路由 `/u/{auth_user}/` 并完成 WAA 挑战握手；
2. **原子门禁切换**：握手就绪后，`SessionReadinessGate` 原子切换至新 `SessionContext`，状态置为 `READY`，瞬间放行排队中的新请求；
3. **旧会话平滑排干 (Drain)**：旧账号上下文移入退休队列，在途请求继续使用原有标签页；当计数归零（`in_flight == 0`）触发 `drain_event` 后再销毁。崩溃、取消和上游断连仍按错误返回，不能把部分正文标记成成功。

### 4.5 流式响应与自动续写的边界

- 流式和非流式共享生成块结构检查。HTTP 200、合法 JSON 外层或早先的完成标记，均不能掩盖后续非法生成块。
- 只有完整传输及真实完成原因才能结束响应。首个内容片段发出后不重放原请求；安全阻止、网络错误和未知协议内容不会触发自动续写。
- 抗截断模式保留最多 512 KiB 的续写片段，输入历史估算预算为 1 MiB；最多两轮额外请求共享一个总期限及账号租约。工具声明和策略不变，实际工具、代码、多模态或其他无法回放的载荷停止追加轮次。
- 内部续写保留每轮独立的模型/用户历史及每个 Part 的签名。客户端收到的是合并回答；需要跨请求完整重放带签名历史的客户端，应使用普通模型并自行管理续写。
- 续写轮次合并登记一次成功，成功返回的 Token 用量累计所有上游轮次；错误重试仍登记相应失败和成功事件。因此统计中的请求数既不等于客户端请求数，也不等于上游实际调用次数。


## 5. 跨平台支持与依赖隔离

| 平台 | 运行模式 | 浏览器后端 | 内存基准 |
|---|---|---|---|
| **Android (Termux)** | `proot-distro` Linux 容器隔离运行 | CloakBrowser (aarch64) | 约 350 - 450 MB PSS（建议空闲 RAM ≥ 1 GB） |
| **Linux (x86_64 / arm64)** | 原生宿主运行 | CloakBrowser (x86_64 / arm64) | 约 250 - 350 MB PSS |
| **macOS (Apple Silicon / Intel)** | 原生宿主运行 | CloakBrowser | 约 250 - 350 MB |
| **Windows (x64)** | 原生宿主运行 (Job Object 进程管理) | CloakBrowser (x64) | 约 280 - 380 MB |
| **Docker 容器** | Debian 基础镜像 (amd64 / arm64) | 容器内置 CloakBrowser (`/opt/cloakbrowser/chrome`) | 约 250 - 350 MB |
> 依赖管理使用 `uv`。由于 PyPI 官方未提供 Android 预编译 wheel，而 Termux 的 TUR 源缺少桌面端 wheel，因此 `uv.lock` 不纳入版本控制：桌面端直接从 PyPI 安装对应 wheel；Termux 环境通过本地 `uv.toml` 使用 TUR 源，避免不同平台的 wheel 源锁定冲突。

## 6. 修改入口与验证

| 变更范围 | 优先检查的位置 | 必须保留的行为 |
|---|---|---|
| 认证与配置 | `api/dependencies.py`、`api/routes_system.py`、`infrastructure/utils/config_store.py` | 管理/调用权限分离，原子保存且保留未编辑字段；日志级别与布尔值写前验证，持久化成功后才应用运行设置 |
| 账号与配额 | `application/account_rotator.py`、`account_orchestrator.py`、`gateway/session.py` | 统计归属原账号、模型冷却独立、退休上下文等待租约释放 |
| 生成与续写 | `application/chat_service.py`、`continuation.py`、`gateway/wire_parser.py`、`streaming.py` | Schema 的零值约束保留、Part 顺序与签名保真、真实完成、预算有界、不重放已发出的内容 |
| 浏览器通信 | `browser/cdp_client.py`、`gateway/transport.py`、`browser/js/` | 同一期限覆盖发送与等待，断连/取消清理队列、任务和请求 |
| HTTP 诊断 | `api/app.py`、`api/error_middleware.py` | 摘要只记录固定操作名与异常类型，未处理异常返回通用 500，并保留安全的内存诊断；取消继续向外传递 |
| 管理界面 | `web/src/`、`tests/frontend/` | 旧响应不覆盖新快照；文件读取与保存只更新当前弹窗和草稿；模型草稿接收未编辑的日志设置刷新，保留明确编辑的 YAML；卸载取消读取，浏览器状态串行且仅在可见时轮询 |

使用已配置的 Python 环境运行 `python scripts/run_tests.py`。该入口在导入前隔离配置、账号、统计与临时目录并禁用 dotenv；测试中的启动和浏览器路径使用合成响应及替身。Windows 自动排除两项 Linux 专用测试，Linux CI 运行完整检查。

Python 代码运行 `python -m ruff check .` 和 `python -m pyright`。前端在 `web/` 按 `package.json` 运行 `npm run type-check`、`npm run lint`、`npm run test:frontend` 和 `npm run build`；不手工编辑静态构建产物。

发布通过受保护分支上的 CI、固定提交制品和既定部署工作流完成，保留上一代码版本并验证健康及认证。模型目录发现也可能访问 Google，因此部署验证不查询生产模型目录、不激活账号或调用真实模型。配置、浏览器数据、密钥及统计留在发布目录之外；回滚只切换代码。
