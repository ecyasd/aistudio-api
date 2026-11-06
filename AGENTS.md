# Browserless API 协作规则

- 后端以 DeconstructedCube/aistudio-api 的 feat/browserless-waa-engine 为基础；沿用现有 application、gateway 和 WAA 实现。协议适配器共用同一生成流程，定制能力只保留一个实现。
- 管理界面以 AIS2 的独立日志页、请求页与筛选交互为参考；主要服务 SillyTavern，Gemini 与 OpenAI Chat Completions 是对外协议。
- 管理接口仅接受独立 Bearer 管理密码；模型接口使用动态调用密钥，空密钥拒绝。测试和文档使用虚构凭据。
- 请求诊断只记录有界元数据与固定事件，不保存提示词、正文、思考、签名、Cookie 或原始异常。活动请求和结束记录分别有界，取消与失败不能记为成功。
- 账号归属、签名顺序、流式背压、取消与资源释放沿用上游链路；修改这些行为须补离线回归。启动不主动探测 Google 账号或发起推理。
- Python 检查使用项目隔离环境及 `scripts/run_tests.py`；前端检查按 `web/package.json`。提交前审查 diff，不提交运行配置、账号、构建缓存或私有运维资料。
- 整合工作在开发分支完成；发布与代码回滚保留所有运行数据。架构入口见 `docs/ARCHITECTURE.md`，控制台与协议适配见 `docs/CONSOLE.md`。
