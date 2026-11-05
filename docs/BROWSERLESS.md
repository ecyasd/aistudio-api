# 本地 WAA 与 Build 写代码助手

默认仍使用原有受控浏览器。设置 `AISTUDIO_WAA_BACKEND=browserless` 后，普通
Playground 和 `-build` 模型共用本机 SpiderMonkey 152 WAA 运行时与 Firefox 网络栈；
不启动完整浏览器，不使用外部签名中继。本地签名失败会明确结束请求。
账号导入和管理仍沿用原有入口、凭据文件及凭据代际。

## 运行与构建

需要 Go 1.27、C++ 编译器及作者提供的 SpiderMonkey SDK。Linux amd64 的原生 GNU
构建选 `spidermonkey-v152.0.4-aistudio2api.1-linux-amd64-gnu.tar.gz`；Zig musl
交叉构建选 `spidermonkey-v152.0.4-aistudio2api.1-linux-amd64.tar.gz`。
资产在 [作者 Release](https://github.com/Mag1cFall/firefox/releases/tag/v152.0.4-aistudio2api.1)，
`linux-x64` 不是该 Release 的资产名。解压 SDK 后检查来源及摘要，缓存与构建放源码库之外。

```sh
python scripts/build_waa.py --deps /path/to/spidermonkey \
  --cache /path/to/build-cache --output /path/to/waa-signer

/path/to/waa-signer -listen 127.0.0.1:8081 -state-dir /path/to/private/waa-state
```

交叉构建可额外传 `--go /path/to/go`、`--zig /path/to/zig`。
只构建 Linux amd64 签名器。参考 systemd 单元为 `deploy/aistudio-waa.service`；
它不在启动时载入账号或调用 Google。只有真实请求的账号准备才创建 Worker。

API 进程使用以下环境设置：

```text
AISTUDIO_WAA_BACKEND=browserless
AISTUDIO_WAA_SIGNER_URL=http://127.0.0.1:8081
```

签名器仅允许明确的 loopback IP 和端口，客户端关闭环境代理及重定向。
公网网关不能转发其端口或 `/v1/session/*`、`/v1/proof/mint`、`/v1/rpc/stream`。
需要上游网络代理时，在签名器启动参数中使用原有 `-proxy`，让 WAA 与业务 RPC
使用同一出口。不会自动发现其他代理或切换到公网 relay。

Linux CI 编译并自检原生二进制，再将它与 Python 和前端打入同一个固定提交工件。
服务器固定安装器需先完成一次性 native 单元接入；以后代码链接同时决定 Python 和
签名器版本，发布与回滚一起停启两者。原生进程启动前先完成既有崩溃恢复。
现有服务环境不设置上述变量时保留浏览器后端。切换后端需重启 API 进程，账号、
配置、密钥和原有统计均不迁移、不覆盖。

## 请求与账号边界

请求仍经过原有 GenerationRun 准入、账号调度及同任务租约。
每个上下文创建独立随机 native session，重新导入凭据后不会复用旧 Worker。
签名依据编码后实际发送的 contents；签名、Cookie、账号槽与 HTTP 请求共用同一
上下文。最多同时保留两个 Worker，旧上下文等已有租约结束再退休。
取消已开始的签名会在原期限内等待其结束，再释放租约；流断连会关闭原 HTTP 请求。
中途读取错误和缺失完成帧不能被当作正常 EOF，不会重放已经输出的生成。

Cookie checkpoint 核对原凭据代际、文件版本及完整认证 Cookie 家族；新导入不能
被旧请求覆盖。Cookie 轮换并不保证会话永久有效，Google 已撤销或过期的会话仍需
所有者重新提供凭据。没有增加无请求时的 Cookie 探活或“永久续期”承诺。

## Build 助手

从实时 ListModels 主能力字段（index 64）含 `48` 的条目派生 `<模型>-build`。
未取得能力目录时不会根据模型名称伪造 Build 可用性。可手动填写已知的 Build 模型名，
由上游返回真实可用结果。

这是 Build 的写代码助手：`CreateApplet` → `CodeAssistantOffline` →
`StreamCodeAssistantOfflineGeneration`，并非应用预览的 ProxyUnaryCall 通道。
PG 和 Build 使用同一套无浏览器 WAA 签名原理，业务 RPC 及 proof 字段不同。
Google 自带的系统提示词会影响回复风格、身份与格式；服务没有移除它。

此接入面向酒馆文本对话和思考档位。model 历史去除思考后以 `<assistant_turn>`
转录为 user 历史，签名绑定转录后的实际内容。Google 的 Build 服务端生成/安全
设置继续生效，Playground 的温度、top-p、输出上限等不保证在该通道生效。
工具与多模态输入明确拒绝，不会静默丢弃；普通 Playground 的原有能力继续保留。

每个请求只创建自己的临时应用，在结束、失败或取消时取消该任务并清理该应用；
不会删除其他应用或跨请求保存对话。只在同一生成任务的消息列表正常结束且未完成时
重开一次读取流，不重新生成。回复只有思考或内容不完整时明确报错，不自动追加模型调用。
`-build` 不派生抗截断模型，以免 Build 助手和 Playground 的续写策略混用。

## 用量与请求

管理页 `/usage` 和管理 Bearer 保护的 `GET /requests` 提供请求表与汇总。
`request_usage.sqlite3` 位于既有 `AISTUDIO_DATA_DIR`，不进入代码工件。
保留最近 30 天、最多 1000 条已结束客户端请求；汇总覆盖当前保留记录，
原有累计模型统计继续使用原统计文件，不把旧数据清零或伪造成逐请求历史。

一行表示一次客户端请求；上游尝试包含重试和续写。
每次上游调用只取最后观测的累计 usage，再跨调用求和，失败尝试已报告的用量也计入。
缺失值为 `null`，明确的零保留；部分上游轮次没有 usage 时标为不完整。
这些数字是观测用量，不是账单或保证完整的实际消耗。
Build 的原始用量没有独立思考 token 细分，不据文本长度估算。

账本只保存请求 ID、模型、通道、固定结果、耗时及 token 数字；不存账号身份、
提示词、正文、思考、签名、Cookie、密钥或异常原文。实时错误的内存保留规则不变。

## 离线验证

```sh
python scripts/run_tests.py
python -m ruff check .
python -m pyright
cd native/waa-signer && go test ./internal/localapi
```

前端执行 `npm run type-check`、`npm run lint`、`npm run test:frontend`、`npm run build`。
测试只使用合成账号、替身 Worker 与协议回放。离线通过与原生构建不等于真实账号
已完成 Google 验证；发布验收不得激活账号或发真实模型请求。
