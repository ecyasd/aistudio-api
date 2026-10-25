# 部署与维护

本说明面向已有 Linux 主机上的维护，公开访问采用 HTTPS 反向代理、systemd
单进程服务和 `secure_runner.py`。普通入口也已实现管理密码与调用密钥分离；
加固入口额外限制失败认证、强制关闭敏感转储、保持后端与 CDP 私有监听，并禁用启动预热。

## 代码、配置与数据

| 内容 | 管理方式 |
|---|---|
| Python、浏览器脚本、已构建前端、运行时认证守卫 | 由经过 CI 的代码工件更新 |
| systemd 服务、启动器、反向代理与部署工具 | 单独维护，常规代码工件不覆盖 |
| `config.yaml` 与其中的 `api_keys` | 服务器上的持久配置；发布和回滚均不覆盖 |
| 网站管理密码 | 独立的 systemd credential；不进入 Git、构建工件或日志 |
| Google 账号与 Cookie、统计、配额及冷却状态 | 独立持久目录；代码发布不导入、激活、清空或替换 |
| 实时错误 | 仅当前 API 进程的内存；最近 10 分钟、最多 100 条，重启即清空 |

从本仓库构建或使用本仓库 CI 工件。上游容器镜像不包含本分支修复。
开发依赖安装在隔离验证环境中；代码更新不顺便升级生产依赖。

## 加固入口

先安装项目运行依赖与受控 Chromium，再为服务配置以下持久路径。
示例使用通用路径；已有部署应保留自己的路径和数据。

| 环境变量 | 示例 |
|---|---|
| `AISTUDIO_CONFIG_FILE` | `/var/lib/aistudio-api/config.yaml` |
| `AISTUDIO_DATA_DIR` | `/var/lib/aistudio-api` |
| `AISTUDIO_ACCOUNTS_DIR` | `/var/lib/aistudio-api/accounts` |
| `AISTUDIO_STATS_FILE` | `/var/lib/aistudio-api/stats.json` |
| `AISTUDIO_ROTATOR_STATE_FILE` | `/var/lib/aistudio-api/rotator_state.json` |

持久路径由服务用户读写，凭据来源文件只授予所需读取权限。`secure_runner.py`
从 systemd 提供的 `CREDENTIALS_DIRECTORY/app-secrets` 读取 JSON 中的
`admin_password` 字段。通过服务单元的
`LoadCredential=app-secrets:/etc/aistudio-api/secrets.json` 提供该文件；
不要把凭据复制到仓库或放入启动命令参数。

服务的 `ExecStart` 指向已安装虚拟环境的 Python 和本仓库
`deploy/secure_runner.py` 的绝对路径。该入口将工作目录设为源码目录，监听
`127.0.0.1:8080`，以一个 worker 运行，并将 Chromium 调试端口保持在 loopback。
不要把此 loopback 启动方式直接套入依赖容器外访问的网络布局。

代码文件需要服务用户可读。原子替换后应显式设置最终权限，不能仅依赖创建时的
mode；严格 umask 可能移除服务用户的读取权限。

## HTTPS 与认证

- 反向代理承接公网 HTTPS，后端 8080 与浏览器调试 9222 不公开。
- 代理覆盖 `X-Real-IP` 为实际连接来源，不能透传客户端自报值；访问日志省略查询字符串。
- 网站管理只使用独立管理密码的 Bearer 认证。API 只使用管理界面配置的调用密钥，密钥集合为空时拒绝调用。
- 普通入口和容器同样要求管理密码；未配置或仅含空白时拒绝全部管理请求。`/auth/check` 始终要求登录，公开最小健康仍可读取。
- 所有启动入口均禁用浏览器预热；仅启动服务、读取健康或诊断状态不会访问 Google。
- Gemini `key` 查询参数、`x-goog-api-key`、`x-api-key` 与 Bearer 调用方式均保持兼容。管理密码不能调用 API，调用密钥不能访问管理接口。
- 如有路由白名单，保留页面 `/error-records`、`/error-records/`，以及经管理认证的 `GET /errors` 和 `GET /browser/status`。
- `/__deployment/api-auth` 是本地、需调用密钥认证的无推理检查，不需要公网放行。公开 `/health` 只返回最小状态。

修改模型设置会保留当前 `api_keys`。重新导入有效 Google 登录状态或激活账号也不会
重置调用密钥；只有专用密钥管理操作会更改它。上游 Google 登录失效与调用密钥失效
分别处理，不应通过轮换 API 密钥解决 Google 登录问题。

管理页面保存 YAML 时同时发送加载基线，服务端只合并实际编辑的顶层字段。
模型草稿不恢复其他设置卡刚保存的日志值；显式修改的同一字段已被并发操作更新时
返回 `409`，草稿保留，刷新并合并后再保存。调用该接口的自定义客户端应传
`baseline_yaml_content`；省略基线仍按旧接口语义替换完整非密钥配置。

### 模型选择器的公网路由

若 HTTPS 网关按模型路径设置白名单，必须同时接受普通模型和目录中列出的
`抗截断/原模型名`。只允许单段 ASCII 模型名的旧规则会让模型列表正常返回，
但选择抗截断模型时在网关直接返回 `404`，请求不会到达 API。

Caddy 的两个命名匹配器应保留各自的 GET、POST 方法限制，并使用以下路径规则：

```caddyfile
path_regexp public_model_read ^/v1beta/models(/(抗截断/)?[A-Za-z0-9][A-Za-z0-9._-]*)?$
path_regexp public_model_generate ^/v1beta/models/(抗截断/)?[A-Za-z0-9][A-Za-z0-9._-]*:(generateContent|streamGenerateContent)$
```

[Caddy 的 `path_regexp`](https://caddyserver.com/docs/caddyfile/matchers#path-regexp)
匹配 URL 解码后的路径；客户端把中文或分隔斜杠编码为 `%xx` 时仍使用同一规则。
只追加这个明确的选择器前缀，保留现有认证、其他路由、未知方法的 `404` 和私有端口。
先校验候选 Caddy 配置，再 reload；常规代码工件不会替换现有网关配置。
无模型调用验证可确认：未认证的普通和抗截断生成路径均进入认证守卫，通常返回 `401`；
同一来源已触发认证失败限流时可返回 `429`。两者都应拒绝调用，不能因选择抗截断模型
在网关返回旧规则的 `404`；未知方法仍为 `404`。认证后的真实模型调用须单独按授权验证。

## 发布检查

发布前检查受影响的后端测试及前端类型、lint 和构建，并使用合成数据验证。
CI 构建的 `aistudio-code-<gitsha>` 工件包含 `release.tar.gz`、`manifest.json`
和 `release.tar.gz.sha256`。归档只包含 `src/aistudio_api/`（使用本次新构建的前端）
及由部署源码映射的 `runtime/public_access_guard.py`；运行配置、账号、Cookie、
`.env`、密码与 `secure_runner.py` 均不进入工件。构建工具为
`scripts/build_release.py --static-dir <fresh-built-static> --output-dir dist/release --expected-sha <gitsha>`。

自动部署的工作流与一次性接入配置见下一节。服务器接入成功以前，添加工作流文件
只代表流程已经定义，不代表生产已开始自动更新。

PR 合并后，须确认合并产生的 `master` 提交 SHA 有对应且成功的 `push` CI 运行，
并以该 SHA 生成发布工件。只有 PR CI 成功或页面显示已合并，不能证明部署已触发；
缺少这次 `master` CI 时应检查触发情况，保留现有来源校验，不跳过检查发布或部署。

每次更新应核对工件摘要与 Git 提交，串行替换代码并保存上一版本，再检查服务状态、
本地健康、公开页面与认证分离。验证不能发送测试提示词、启动账号探活或执行模型预热。
成功后记录提交与结果，不记录密钥、请求或 Cookie 内容。

## GitHub 自动部署

流程定义于 [CI](../.github/workflows/ci.yml) 与
[部署工作流](../.github/workflows/deploy.yml)：

1. `master` 推送先通过后端测试（含官方 SDK）及前端检查、构建，再生成按提交 SHA 命名的代码工件。
2. 部署工作流通过 `workflow_run` 等待 CI 成功，并确认来源是本仓库的 `master` 推送。PR、fork 和未通过 CI 的提交不进入部署。
3. 使用本次通过 CI 的工件创建固定版本 `deploy-<40位提交SHA>` 的 GitHub Release，附上 `release.tar.gz` 和摘要文件。
4. GitHub Actions 通过 OIDC 获取短期 AWS 权限，调用指定的 SSM Document；只传递 `CommitSha` 与 `ArtifactSha256`，不开放任意远程 shell。
5. 服务器固定部署工具校验提交、工件摘要与允许的归档路径，将版本放入 `/opt/aistudio-api/releases/<sha>/source`，再原子切换源码符号链接并检查服务。

一次性接入需要仓库管理员配置以下 **Repository variables**：

| 变量 | 内容 |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | 专用 IAM 角色 ARN；OIDC 信任精确限制为本仓库 `refs/heads/master` |
| `AWS_DEPLOY_DOCUMENT` | 受控部署 SSM Document 的名称 |
| `AWS_DEPLOY_REGION` | 部署目标区域 |
| `AWS_DEPLOY_NODE_ID` | 现有 SSM 托管节点 ID |

AWS 角色只需对指定 Document 和目标节点执行该部署及查询其结果；不需要长期 AWS
Access Key。该流程不使用 GitHub Environment，OIDC 信任须与分支型 subject 一致。
subject 须匹配仓库的实际 OIDC 格式；启用不可变 subject 的仓库还包含 owner/repository ID，
不能直接套用仅含名称的旧格式，详见 [GitHub OIDC 参考](https://docs.github.com/en/actions/reference/security/oidc)。
变量缺失、信任或权限未接入时，不能认为自动部署已经生效。

服务器需预先安装 root 所有且普通服务用户不可改写的
`/usr/local/sbin/aistudio-github-deploy`，参数只有 `--commit` 与 `--sha256`。
初始化还需把现有代码纳入版本目录，并将运行时守卫指向
`source/runtime/public_access_guard.py`。这些接入操作由管理员单独完成；
后续常规发布不替换启动器、虚拟环境、反向代理、配置、凭据或数据。

在 Actions 中查看关联 CI 与部署运行，核对部署提交和健康检查结果。
工作流文件存在、Release 已生成或 SSM 命令已发送，都不等于服务健康验证已经通过。

## 回滚

回滚仅恢复之前的代码及前端，使用原有运行依赖并重新启动服务。配置、网站密码、
调用密钥、账号和统计必须保持当前版本；不要把整个运行目录恢复为旧备份。
回滚后重复健康与认证检查。重启会清空实时错误，这是设计行为。

## 常见问题

| 现象 | 处理 |
|---|---|
| `401 / authentication_error` | 检查客户端发送的调用密钥及管理界面的密钥列表；未配置密钥也会拒绝调用 |
| `503 / upstream_auth_expired` | 在管理界面重新导入经授权且有效的 Google 登录状态，保留现有 API 密钥 |
| `503 / upstream_auth_failed` | 检查管理界面的账号状态和上游权限；不要把网站密码或调用密钥当作 Google 凭据 |
| `503 / upstream_account_mismatch` | 页面当前账号与所选账号记录不符，已阻止使用该会话；检查所选账号，不要轮换 API 密钥 |
| `503 / upstream_account_unavailable` | 页面暂未提供可核验的当前账号信息；稍后重试并查看实时错误，不等同于 Google 登录已过期 |
| `503 / service_busy` | 推理并发或等待容量已满，稍后重试；管理请求保留独立处理能力 |
| 浏览器显示未启动或休眠 | 首次推理之前及空闲回收之后属正常状态；`GET /browser/status` 本身不会启动浏览器 |
| API 返回 `502` 或流式 error | 查看实时错误中的分类与请求 ID；不完整的上游 HTTP 200 不能算正常生成完成 |
| 错误列表在重启后为空 | 记录仅在内存存在，不提供跨重启历史 |

实时错误页仅在可见时每 5 秒刷新，读取失败会明确显示；它不会保存原始异常、
提示词、模型回复、Cookie 或密钥。Cookie 检查点可以保存刷新后的有效会话，
但无法恢复 Google 已撤销或已过期的登录。

非流式 `generateContent` 在请求体接收完毕后继续监听客户端断连。收到 ASGI
`http.disconnect` 时取消生成，等待浏览器请求的有界 abort 和会话回滚，释放并发名额
与账号租约，并将被动诊断记为取消。流式接口保留原有断连处理。客户端仅隐藏界面或
关闭工具选项而没有中断网络请求时，服务器无法把它识别为取消；浏览器发出 abort
也不能证明上游服务内部的计算已立即停止。

签名超时或失败的页面不再用于新的签名请求，后续通过原有账号隔离流程在租约排干后
替换该页面，避免请求一直排在未完成的签名后面。若客户端取消时签名已经发出，只
等待这一次签名在原有 10 秒预算内收尾，不启动新的签名；等待签名锁时取消则直接退出。
内部超时的错误记录保留超时分类和实际 HTTP 状态，不将其底层取消机制误记为客户端断连。

同页的“响应探测”也显示正常完成的生成请求，可核对是否实际发生多轮上游调用、
重试、正文后再次出现思考，以及 `[:数字]` 形式标记的命中次数。信号表示观测到的
顺序，不直接证明存在缺陷。按服务端请求 ID 区分单次请求内的阶段变化与客户端
再次发出的请求；服务端不能据此判断用户正则如何显示文本，也不能识别没有原生
思考标志的文字是否属于思考。

响应诊断独立保留最近 10 分钟、最多 100 条，只保存固定字段和计数。
每次请求结束时还会向现有服务日志输出 `response_diagnostic` 白名单摘要，便于
超过管理页窗口后按时间和请求 ID 追查。正文标记、正文后思考及完成后内容使用
WARNING，失败和取消的摘要也使用 WARNING，普通成功和恢复使用 INFO；
是否可读取取决于现有日志级别和保留情况。
不需要打开请求或响应原文转储，不新增模型请求或持久化数据文件。

摘要还包含原生思考 Part 数、已报告思考 token 数，以及请求是否显式提供思考配置。
`thinking_mode` / `thinking_level` 只记录规范化覆盖配置中的已知枚举；`null` 表示未观测到
该覆盖值，不推断最终 wire 默认值。`thought_tokens` 使用每次上游调用最后报告的累计值
再跨调用相加，包含失败尝试已报告的用量；缺失为 `null`，明确的零保留为 `0`。
部分轮次缺失 usage 时只累计已观测到的用量，不把缺失部分推断为零或完整实际消耗。
这些计数帮助区分上游未提供思考与输出中途失败，不保存正文、思考、签名或原始配置数组。

身份判断仅采用当前账号控件中的明确邮箱，不使用提示词、整页文本、未选中的账号菜单、
Cookie 或任意全局 JSON 中出现的邮箱证明身份。页面尚未就绪与明确身份不符分别记录，
实时错误详情提供固定错误码、请求 ID 和安全堆栈位置。流式请求若已发送 HTTP 200，
之后的 SSE 错误仍按实际错误分类记录，并单独保留传输状态。
