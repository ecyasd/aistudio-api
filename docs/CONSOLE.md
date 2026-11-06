# Browserless API 与酒馆控制台

后端基于 [DeconstructedCube/aistudio-api 的 feat/browserless-waa-engine](https://github.com/DeconstructedCube/aistudio-api/tree/feat/browserless-waa-engine)，保留 WAA 签名、HTTP/2 网关、wire 编解码和模型调度。管理交互参考 [Mag1cFall/AIStudio2API](https://github.com/Mag1cFall/AIStudio2API) 的独立请求页、日志分组和筛选方式；不引入 AIS2 的 Go 后端、桌面运行时或网页试聊。

## 启动与鉴权

使用 Python 3.11+；开发安装 `uv venv`、`uv pip install -e ".[dev]"`。前端使用 Node 24，执行 `cd web`、`npm ci`、`npm run build`。运行入口保持 `python main.py server --port 8080`，WAA 签名服务配置沿用上游说明。

通过环境配置独立的 `AISTUDIO_WEB_PASSWORD`，在管理界面的专用密钥管理中配置调用密钥，或在配置文件的 `api_keys` 中设置。未配置管理密码时管理接口拒绝访问；未配置调用密钥时模型接口拒绝调用。管理密码不能调用模型，调用密钥不能访问管理接口。管理密码只接受 Bearer 头；Gemini 调用继续支持 `?key=`、`x-goog-api-key` 和 Bearer。

启动仅加载本地元数据和签名服务，不主动探测 Google 账号等级、邮箱或执行模型预热。已有账号选择在实际生成请求时恢复。更新和回滚只替换代码，运行配置、账号文件、统计和凭据放在外部数据目录中。

## 酒馆接入

| 方式 | 配置 |
| --- | --- |
| OpenAI 兼容 | 自定义兼容端点，基础 URL 为 `https://<服务地址>/v1`；使用独立调用密钥 |
| 模型列表 | `GET /v1/models`，返回 `object: list` 和 `data` |
| 聊天生成 | `POST /v1/chat/completions`，支持流式和非流式 |
| Gemini 原生 | 保持 `/v1beta/models` 及 `:generateContent` / `:streamGenerateContent` |

OpenAI 适配支持 system/developer/user/assistant/tool 历史、assistant 预填、temperature、top_p、top_k、max_tokens/max_completion_tokens、stop、seed、频率/存在惩罚、reasoning_effort、JSON 输出及函数工具。图片输入支持 base64 data URL；远程图片 URL、多候选 `n>1`、音视频输入和图片输出请使用 Gemini 原生能力。兼容范围为 Chat Completions，不实现 Responses API。

流式思考使用 `reasoning_content`，正文使用 `content`。正常结束发送结束原因和 `[DONE]`；`stream_options.include_usage` 启用时，在 `[DONE]` 前发送 `choices: []` 的用量块。流内错误以 error 事件结束，不补发成功结束标记。函数调用使用 `tool_calls`；Gemini 特有的有签名多轮工具历史应使用原生接口。

接口形状参照 [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) 与 [流式事件文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)。

## 请求与日志

- `/usage`：参考 AIS2 用量页，提供时间范围、模型、协议、通道和结果筛选，六项指标、四张交互图、模型排行与分页记录。统计覆盖当前留存的结束记录，不仅是请求页最近 500 条；未报告用量和无样本耗时显示缺失，不推算价格。首正文分位数只统计流式请求。
- `/requests`：状态、协议、模型、总耗时、首正文、上游调用次数、上游报告的 Token、结束原因和阶段时间线。可停止进行中的请求，也可打开关联日志。
- 请求管理上方同时展示当前请求与账号/模型冷却，提供停止、解除冷却和详情；从用量页可读取仍在留存期内的较早记录。
- `/logs`：按请求分组的固定事件，支持级别、来源、模型/请求 ID 搜索、展开阶段和跟随最新。筛选状态保存于 URL，便于直接打开。
- 请求与冷却每 10 秒读取快照，日志页每 2 秒读取；支持暂停和手动刷新。页面隐藏时暂停轮询，离开页面取消读取。读取失败保留上次数据并显示失败状态，停止请求与手动刷新立即执行。
- HTTP 响应携带 `X-Request-ID`；流式 HTTP 200 后发生的错误单独记录错误状态，不能据 HTTP 200 判定成功。

`/api/console/requests`、`/api/console/logs` 及 `/api/console/requests/{id}/cancel` 均要求管理认证并禁止缓存。取消等待请求自己的生成任务清理；不关闭共享网关或其他请求。

请求与事件默认保存到 SQLite，保留 7 天，请求最多 5,000 条、事件最多 10,000 条；页面展示最近 500 条结束请求及 1,000 个事件，活动请求最多 64 条，每个请求最多 64 个阶段。只记录白名单元数据和固定事件，不存对话、正文、思考、签名、Cookie、凭据或原始异常。完整报文转储及其设置入口已移除。

首正文时间表示服务端首次收到正文，不能直接等同于用户设备上的显示时间。流式用量取上游实际报告值；未报告显示“未报告”，不估算为零。非流式首正文与完整结果一起到达，不能作为真实首字测量。

## PG、Build 代码助手与运行引擎

管理界面统一使用 `web/src/assets/theme.css` 的 AIS2 风格深色主题。引擎选择和上游已有的 WAA 签名服务管理合并到同一个配置区；模型规则默认折叠，按需展开。

账号导入界面自动区分 Cookie 请求头、JSON Cookie 数组、Playwright storage state、Netscape cookies.txt 和账号包。文件可多选；预览只识别本地格式，不把 Cookie 条数当作账号数。导入保存显式传入 `probe_identity=false`，默认不探测 Google；只有勾选识别多登录身份才调用现有探测导入接口。部分文件失败时保留结果，只重试失败来源。导入 API 未传此字段时保留原有探测行为，兼容既有客户端。

普通模型调用 Playground（PG）；具有官网能力码 48 的模型在目录追加 `-build` 条目，调用 `CodeAssistantOffline` 与 `StreamCodeAssistantOfflineGeneration`，这是 Build 写代码助手。两类模型同时提供，可在同一个服务中并发调用，共用已有账号和 WAA 基础设施，按完整模型 ID 分开记录冷却与用量；无需启动第二套 API 服务。协议依据 [AIS2 的代码助手说明](https://github.com/Mag1cFall/AIStudio2API/blob/main/docs/build.md#8-写代码助手通道)。

代码助手支持文本、多轮历史、思考、客户端函数转录、任务快照增量、问题工具、停止序列、流式/非流式、取消与用量。每次请求使用独立空应用，完整转录客户端历史，请求结束删除应用，不保留会话复用缓存。图片/文件附件请使用 PG。助手的 temperature、结构化输出、安全设置和输出上限由上游助手决定；公开接口的参数不会改写它的服务端配置。

默认 `browserless` 复用上游 SpiderMonkey WAA。设置页可保存 `browser`，重启后使用原上游 WAA 页面钩子与 CloakBrowser 原生 fetch。浏览器进程和页面归请求所有，最多两个同时运行，切号与取消释放旧资源；仅在真实生成请求中启动。镜像内已包含固定版本浏览器，验证只打开离线测试页，不登录 Google、不执行推理。

设置页提供受管理认证保护的“重启服务”；生成进行中返回冲突提示。接口只结束当前 API 进程，由 Docker `unless-stopped` 策略恢复，不能执行任意命令。界面等待进程标识变化后报告恢复。`AISTUDIO_ALLOW_RESTART=1` 在 Docker 中默认启用，本地裸进程默认关闭。所有新版本的配置、账号、调用密钥、统计和日志均保留。

## 验证

后端运行 `uv run --no-sync python scripts/run_tests.py`、`uv run --no-sync ruff check .`、`uv run --no-sync pyright`。测试入口在导入应用前隔离配置与数据，只使用合成响应及网关替身。前端运行 `npm run type-check`、`npm run lint`、`npm run build`。

仓库的 CI 在 Linux 上检查后端、前端和 amd64 容器；master 通过后由 GitHub Release、OIDC 与 SSM 自动部署。服务器环境变量、持久目录及日志轮转见 [部署说明](../deploy/README.md)。
