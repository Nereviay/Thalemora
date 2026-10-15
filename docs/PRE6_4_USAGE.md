# PRE6-4 显式后台维护（合成依赖注入）

本文记录公开 v0.5.0 的合成入口。开发分支的真实模型适配、独立 worker 与实际用量边界见 [PRE6-4B](PRE6_4B_USAGE.md)；原合成示例仍适用，未显式注入 Provider 的 tick 不付费。

入口为 Python 宿主的 `Application.maintenance_tick(agent_id, app_id,
channel="main", limit=4, provider=None)`。`limit` 为本轮最多处理的 Scene 作业数
及 Reflection 建议数（1..100）；每轮 Scene ingestion 最多读取 1000 个来源事件。
导入、search、HTTP/MCP 前台请求不会隐式执行这条模型链。

本阶段只接受显式 `synthetic=True` Provider；不会使用 `Application.providers`
中配置的付费客户端。没有注入时，已终结 Scene 保存为
`blocked / provider_unconfigured`，不创建记忆。开放 Scene 等待现有规则：
20 分钟 cooling、120 分钟 idle finalized；事件间隔等边界继续由 SceneRuntime
判断。尚有 ingestion backlog 时不因分页边缘触发 idle finalization，也不调用 Writer。
缺少可解析时间的 Scene 不会被强制终结。

以下示例只使用临时目录、合成正文及合成模型响应，可以直接运行：

```python
from tempfile import TemporaryDirectory
from thalemora.app import Application

class SyntheticProvider:
    synthetic = True

    def call(self, module, payload):
        if module == "writer":
            source = payload["sources"][0]
            output = {"candidates": [{
                "content": source["text"], "title": "合成长期约定",
                "memory_type": "event", "claim_scope": "user_fact",
                "evidence": [{"source_event_id": source["source_event_id"],
                              "version": source["version"], "quote": source["text"]}],
            }]}
        elif module == "gatekeeper":
            output = {"action": "new", "importance": 6,
                      "long_term_value": True, "reason": "synthetic durable fact"}
        else:
            output = {"text": "合成建议：后续核对该明确版本的约定。"}
        return {"output": output, "usage": {"synthetic_units": 1}}

with TemporaryDirectory(dir="/tmp") as directory:
    app = Application(directory)
    agent = app.register_agent("synthetic-agent")["agent_id"]
    app.grant_source(agent, "synthetic-app")
    app.import_history(agent, "synthetic-app", [{
        "app": "synthetic-app", "source_event_id": "synthetic-1", "version": 1,
        "role": "user", "text": "合成项目长期采用紫色符号。",
        "occurred_at": "2001-01-01T00:00:00+00:00",
    }])
    result = app.maintenance_tick(agent, "synthetic-app", provider=SyntheticProvider())
    assert result["jobs"][0]["status"] == "done"
    print(app.maintenance_status(agent, "synthetic-app"))
```

Provider 的 `call(module, payload)` 返回 `{output, usage}`；Writer 的 output
为 `{candidates: [...]}`，每轮最多 8 项，每项必须有正文和当前来源版本的
精确引用。空列表表示 Writer 丢弃整个 Scene。Gatekeeper 接收既有 messages
协议，支持 new/extend/correct/supersede/drop/uncertain，并执行真实长期价值、
同一记忆关系及目标校验。Reflection 返回 `{text: ...}`，输入包含准确的
Memory revision 正文；建议的 `target_version` 是 canonical revision ID，
不会主动发布 Self Model、Persona、日记等领域状态。

`maintenance_status` 返回当前授权 scope 的 jobs、candidates、calls、reflections。
jobs 区分 pending、blocked、extracted、error、stale、done；候选沿用既有
pending/applied/dropped。错误后再次显式 tick 即可重试；成功 Writer 批次会复用，
已提交 canonical candidate ID 会恢复回执。Reflection 使用稳定 source_ref 幂等追加。
调用审计保留模块、响应、错误及原样 usage，始终标记 synthetic，actual_tokens 永远
为 NULL；没有真实 token、费用或付费预算验收。

Archive 是唯一来源权威。Scene 中既有 `raw_event_id` 字段在适配器内仅表示
Archive rowid；`event_hash` 仅为来源版本标记，不是 canonical RawEvent/hash。
正式证据为 `derived / archive_quote`，保留 Agent/app/channel/source_event_id/
version/quote/原始验证状态，不宣称 raw verified。落卡期间持有 Archive 写锁，
再次核对所有 Scene 来源版本和 tombstone；Memory 仍用 revision_store 的事务及 CAS。
模型执行期间不持有这两个写锁。

本管线按 Agent/app/channel 保存进度和筛选可修订卡片；现有 Memory 文件仍按
Agent/channel 共用，现有全 Agent 记忆浏览接口语义未改。来源更新或删除会提高
派生 Scene 版本并废弃旧未提交候选；过去已合法提交的卡片不会自动撤销，
其证据读取会明确报告 source_changed/deleted。人工卡、人工当前修订及显式字段
不会被自动更新。旧来源删除后的卡片撤回策略、大档案增量优化、真实 Provider
接线和持续调度均留待后续独立验收；本阶段使用 POSIX flock 串行化同 scope 的 tick。
