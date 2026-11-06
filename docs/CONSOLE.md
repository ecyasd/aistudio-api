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

- `/requests`：状态、协议、模型、总耗时、首正文、上游调用次数、上游报告的 Token、结束原因和阶段时间线。可停止进行中的请求，也可打开关联日志。
- `/logs`：按请求分组的固定事件，支持级别、来源、模型/请求 ID 搜索、展开阶段和跟随最新。筛选状态保存于 URL，便于直接打开。
- 两页每两秒读取快照，支持暂停和手动刷新；页面隐藏时暂停轮询，离开页面取消读取。读取失败保留上次数据并显示失败状态。
- HTTP 响应携带 `X-Request-ID`；流式 HTTP 200 后发生的错误单独记录错误状态，不能据 HTTP 200 判定成功。

`/api/console/requests`、`/api/console/logs` 及 `/api/console/requests/{id}/cancel` 均要求管理认证并禁止缓存。取消等待请求自己的生成任务清理；不关闭共享网关或其他请求。

结束请求只在内存保留 10 分钟、最多 100 条，活动请求最多 64 条；日志最多 1,000 条，每个请求最多 64 个阶段。只记录白名单元数据和固定事件，不存对话、正文、思考、签名、Cookie、凭据或原始异常。完整报文转储及其设置入口已移除。

首正文时间表示服务端首次收到正文，不能直接等同于用户设备上的显示时间。流式用量取上游实际报告值；未报告显示“未报告”，不估算为零。非流式首正文与完整结果一起到达，不能作为真实首字测量。

## 验证

后端运行 `uv run --no-sync python scripts/run_tests.py`、`uv run --no-sync ruff check .`、`uv run --no-sync pyright`。测试入口在导入应用前隔离配置与数据，只使用合成响应及网关替身。前端运行 `npm run type-check`、`npm run lint`、`npm run build`。

仓库的 CI 在 Linux 上检查后端和前端；Docker 构建为手动 Linux amd64，避免继承上游定时多平台发布。维护分支的推送不触发旧版生产交付。
