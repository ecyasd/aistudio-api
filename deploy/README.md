# GitHub 与 Docker 部署

`master` 更新后，CI 在 Linux 检查 Python/SDK、前端和 amd64 容器；镜像内同时包含 API、前端和本地 WAA 签名器。镜像以 `container-<commit>` 的 GitHub Release 分发，服务器校验 SHA-256、镜像提交标签及当前 master，再通过既有 GitHub OIDC → 限定 SSM 文档更新 Docker Compose。服务器不拉取源码、不现场编译，不需要长期 AWS 密钥或容器仓库密码。

## 固定位置

| 内容 | 服务器位置 | 默认行为 |
| --- | --- | --- |
| Compose 定义 | `/srv/aistudio-console/compose.yaml` | 固定运行入口 |
| 管理密码与初始调用密钥 | `/etc/aistudio-console/runtime.env` | root 0600；全新安装生成 |
| 当前镜像版本 | `/etc/aistudio-console/image.env` | 固定提交标签 |
| 模型配置及当前动态调用密钥 | `/var/lib/aistudio-console/config.yaml` | 首次由环境变量初始化，之后更新保留 |
| 账号 | `/var/lib/aistudio-console/accounts/` | 首次为空，所有者自行导入 |
| 统计与冷却 | `/var/lib/aistudio-console/stats.json`、`rotator_state.json` | 自动保存 |
| WAA 状态 | `/var/lib/aistudio-console/waa/` | 默认启用，更新保留 |
| 请求与事件历史 | `/var/lib/aistudio-console/telemetry.sqlite3` | 默认启用；7 天；请求最多 5,000 条、事件最多 10,000 条 |
| Python 运行日志 | `/var/log/aistudio-console/runtime.log` | 默认启用；单文件 5 MiB，3 个轮转备份 |
| API 与 WAA 容器输出 | `docker logs aistudio-console` | 默认启用；Docker local 驱动，10 MiB × 3，压缩 |
| HTTPS 访问日志 | `/var/log/caddy/aistudio-access.log` | 默认启用；10 MiB × 4，7 天；去除 URI 和请求/响应头 |

请求页显示最近 500 条结束记录和进行中的请求，日志页读取最近 1,000 个事件。历史仅包含白名单元数据，不记录对话、正文、思考、签名或凭据。

用量页从当前留存的全部结束记录聚合，默认最多 5,000 条。引擎选择保存在 `config.yaml` 的 `runtime.engine`，初始未设置时读取 `AISTUDIO_ENGINE`，默认 `browserless`；可在设置页改为 `browser` 并重启。PG 与 `-build` 代码助手同时提供。CloakBrowser 随镜像分发，默认不启动。

## 环境与服务

环境示例见 `runtime.env.example`。`AISTUDIO_WEB_PASSWORD` 与 `AISTUDIO_BOOTSTRAP_API_KEY` 独立；后者只在空目录首次初始化配置时使用，以后通过管理界面维护调用密钥。

发布器使用管理认证读取当前动态密钥策略，在 `/v1/auth/check` 检查认证；不调用 Google 模型目录或生成接口，也不依赖已经被用户轮换的初始密钥。

容器以 UID/GID 10001 运行，数据和日志挂载目录由该 UID 拥有。根文件系统只读，临时文件使用有界 tmpfs。Linux 主机网络配合 `AISTUDIO_BIND_HOST=127.0.0.1`，API 8080 与本地签名器 8081 均仅监听 loopback；公网只通过既有 Caddy HTTPS。使用主机网络是为了保持按网卡拓扑检查的现有流量保护稳定。

`aistudio-api.service` 是产品的 Docker Compose 启动单元，替换旧 Python 单元；`ExecStartPre` 继续执行既有流量门禁。Docker 守护进程、SSM、HTTPS 证书和流量计数独立维护，发布不重置它们。

常用命令：

```sh
systemctl status aistudio-api.service
sudo systemctl restart aistudio-api.service
docker ps --filter name=aistudio-console
docker logs --tail 100 aistudio-console
tail -n 100 /var/log/aistudio-console/runtime.log
```

常规发布保留新版本的数据和凭据。镜像校验或健康检查失败时保留先前镜像配置；首次替换旧程序的清理须在新容器、公网健康和鉴权检查全部通过后单独执行。
