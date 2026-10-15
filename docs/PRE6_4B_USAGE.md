# 后台自动记忆：模型配置与独立 worker

v0.6.0 提供真实 Chat Completions 模型适配和独立 worker。沿用已验的公共核心，默认不开启付费调用；有限 DeepSeek Flash 小测已覆盖人工记忆冲突与 Writer 指令区分，后台进程另用合成数据/本地假模型完成有界运行与重启验收，不代表广泛质量或长期稳定基准。

## 模型合同与配置

复用管理台已有 Provider 配置（或 Python `app.providers.configure`），分别配置
`writer`、`gatekeeper`、`reflection`：`enabled`、`base_url`、`model`、
`key_env`、`capability="chat"`、`call_budget`、`token_budget`。可选 `thinking_enabled` 只在明确配置时发送思考开关，默认不干涉供应商设置；本轮 DeepSeek Flash 轻量小测显式设为 `False`，不更改念念现役模型配置。Key 仅由进程环境变量提供。
默认禁用、预算为零；只有用户主动配置并启动 worker 后才可能发生供应商调用。

要求供应商支持 Chat Completions 的 `messages`、`max_tokens`、
`response_format={"type":"json_object"}`，并返回 `usage.total_tokens`。
不支持时明确报错，不自动更换模型或修补 JSON。

| 用途 | 输入与输出 | 权威 |
| --- | --- | --- |
| Writer | 已授权、已终结 Scene → 最多 8 个带来源 ID/整数版本/逐字引用的候选；无长期价值时为空 | 来源、精确引用仍由现有管线核实 |
| Gatekeeper | 沿用现有核心 messages 与决策 Schema | 人工字段保护、CAS、准入规则不变 |
| Reflection | 准确记忆修订 → 简短 `text` 建议 | 只保存建议，不发布 Persona/Self Model |

各次输出上限 2048 token，HTTP 超时 30 秒；输入沿用现有 Provider 的 16000 字符上限，超限显式失败，不截断证据。只有显式注入 `MaintenanceProvider(app.providers)` 的 tick 或下面的 worker 会走真实模型；原来无 Provider 的 tick 仍不会付费。

人工卡仍参与同条记忆的关系判断，但标记为只读；同条重复/冲突不另建卡，也不自动改写，候选中保留正文、证据与拒绝写入原因。直接人工卡以最初创建证据确定来源，后续编辑不更换来源归属。Writer 仅在既有一次提炼中区分事实与当次保存/更新指令；纯指令不落记忆，未来长期适用的明确偏好仍可保留。

## 后台启动与状态

先在同一数据目录注册 Agent、授权来源并导入历史，再按实际 scope 启动：

```sh
thalemora --data ./state worker AGENT_ID APP_ID --once
thalemora --data ./state worker AGENT_ID APP_ID --interval 60 --limit 1
thalemora --data ./state maintenance-status AGENT_ID APP_ID
```

一个 worker 负责明确的 Agent/app/channel，私人通道须已有授权并传
`--channel private`。默认每轮最多一个 Scene 作业和一条待处理 Reflection，完成后等待 60 秒；不并行轰炸模型。
worker 与 HTTP/MCP 服务是独立进程，共用同一数据目录，不新增队列、前端轮询或第二个 Runtime。

Scene 沿用已有终结规则：冷却 20 分钟、空闲 120 分钟终结；新事件不保证导入后立刻成为长期记忆。已保存原话和待整理状态不冒充正式落卡成功。

SIGINT/SIGTERM 停止继续调度，当前批次结束后退出；持久进度、候选和回执沿用原管线。再次启动可恢复，不重复写入已提交记忆。

## 错误与费用边界

- 任一实际维护调用失败，本轮记录错误，worker 以 exit 1 退出；修配置/预算后人工重启。不提供循环重试、自动 fallback 或自动拉起服务。
- Reflection 失败时已提交的记忆保留，重启只补未完成部分，不把整轮说成全部成功。
- 用量与调用次数复用 `providers.sqlite` 权威审计；维护成功回执关联 `provider_call_id`。模型返回坏 JSON 时，HTTP 的真实 usage 仍保存在 Provider 审计，不因领域解析失败而抹去消耗。
- 调用次数预算为累计发起次数；token 预算是已报告累计消耗的**下一次调用拦截阈值**，不是预先精确计费的硬金额封顶，最后一次可超过阈值。网络中断或缺少 usage 时消耗为未知，不能宣称零费用；worker 停止，核查后再启动。
- 未配置供应商单价，不估算人民币/美元，`actual_cost` 仍为空；供应商 usage 不等于完整账单，有限小测不代表统一费用保证。
- v0.5.0 合成审计表首次由 v0.6.0 打开时会事务升级并保留旧记录；不会改 Archive/Memory 的正式语义。仅在独立隔离目录测试过。

验证已覆盖慢 Writer 尚未返回时，前台独立导入/检索在 1 秒测试超时内完成；这证明模型调用不持有前台 DB 写锁，**不代表大档案、所有设备或念念真实首字时间的性能承诺**。大档案下的全量对账/候选集合成本仍需随实际使用规模观察。

普通使用只需独立实例与已授权来源，无需念念。合成入口示例见 [合成维护](PRE6_4_USAGE.md)。
