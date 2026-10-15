# 洄海 0.6.0｜使用手册

**第一次使用、不熟悉命令行？** 请先阅读 [零基础安装与第一条记忆](GETTING_STARTED.md)。[Windows](INSTALL_WINDOWS.md) · [Linux](INSTALL_LINUX.md) · [Android 手机试装（待增量版本发布）](INSTALL_ANDROID.md) · [安装难点与后续优化](INSTALL_FRICTION.md)。本页主要是 API 和运维参数，不要求普通用户从头读完。

**版本界限**：v0.4.0 提供四项 stdio 读取工具，v0.5.0 增加 HTTP MCP 九工具与合成维护，v0.6.0 增加真实模型协议适配、独立 worker 和念念适配。后台配置与启动见 [自动记忆](PRE6_4B_USAGE.md)，合成入口仍保留。

本地 Python/HTTP/MCP 共用 Application，管理台直接读取同一 API。worker 是共享同一数据目录的独立后台进程，不新增 Agent Runtime 或第二套配置权威。

## 快速启动

Python 3.10+；Dockerfile 使用 Python 3.12。无云端 Key、无模型下载、无启动提炼任务。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -c requirements.lock .
export THALEMORA_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
thalemora --data ./state serve
```

浏览器打开 `http://127.0.0.1:8765`，填入自己生成的 token。token 仅在页面内存保留。先注册 Agent，再显式授权 `demo-app/main`。private 需要另行授权，不继承 main。管理 token 代表实例唯一 owner，具有注册及授权权力；不同客户端不应把它当作权限更窄的第三方 app 凭据。

Docker Compose 的本地安装合同：

```bash
# 保持上面生成的 THALEMORA_TOKEN 环境变量
docker compose -f deploy/compose.yaml config --quiet
docker compose -f deploy/compose.yaml up --build -d
docker compose -f deploy/compose.yaml logs --tail 30
```

默认仅发布宿主 `127.0.0.1:8765`，命名卷与念念完全隔离。健康检查为 `/health`；API 需要认证。容器内部 `--container` 显式绑定 `0.0.0.0`，不改变 Compose 的宿主绑定。云端模型 Key 默认不传入；确需启用时自行将对应环境变量加入容器 environment。已用隔离临时 Compose project 完成新空镜像构建与容器健康/API/原文/记忆操作验收并清理；该验证不代表念念生产接入。

## Python / HTTP / MCP

```python
from thalemora.app import Application
s = Application('./state')  # 可信本地宿主；勿让外部请求构造 owner/reader
agent = s.register_agent('合成旅行助手')['agent_id']
s.grant_source(agent, 'demo-app', 'main')
events = [{"app": "demo-app", "channel": "main", "source_event_id": "demo-1",
           "version": 1, "role": "user", "text": "合成示例：一起整理旅行计划。",
           "occurred_at": "2026-10-14T10:00:00+08:00", "deleted": False,
           "hash_verification": "unknown"}]
s.import_history(agent, 'demo-app', events)
print(s.search(agent, 'demo-app', '旅行计划'))
print(s.remember(agent, 'demo-app', 'demo-1'))  # 人工记忆，保留来源和版本
```

合成文件见 `docs/examples/p4_history.json`；Web 可导入 JSON 数组（每文件 1..100 条）。重复同版本异文按 P2 合同报错，逐事件提交，失败前已提交项保留。

HTTP 使用 `Authorization: Bearer <本实例管理 token>`，`GET /api/<操作名>` 通过 query 传参，`POST /api/<操作名>` 通过 JSON 对象传参。GET 例：`/api/history?agent_id=<UUID>&app_id=demo-app&channel=main&limit=20&offset=0`。所有响应为 `{"result": ...}` 或带真实状态码的 `{"error": ...}`。不接受文件路径、eval、调用方 principal/actor。主体只由服务器配置确定。

| HTTP 操作 | 方法与用途 |
| --- | --- |
| `list_agents` / `register_agent` | GET 分页 / POST `display_name` |
| `sources` / `grant_source` | GET 分页 / POST `agent_id, app_id, channel` |
| `history` / `import_history` | GET 分页及 `include_deleted` / POST `agent_id, app_id, channel, events` |
| `search` | GET 上述域参数及 `query`；1–2 字符走持久 Btree 短 gram，3–200 字符沿用 FTS5 trigram；支持 `limit`/`offset` |
| `memory` / `remember` / `evidence` | GET 分页 / POST `source_event_id` 原文入人工记忆 / GET `card_id` 与分页 |
| `water` / `water_node` / `water_move` / `water_memberships` | 节点分页 / 显式创建节点 / 复用 P1 归属调整及审计 / 归属分页 |
| `inner_state` | GET 域参数、`kind` 和分页；包含全部内在领域及 Self Model/Persona |
| `configure_persona` | POST `agent_id,app_id,authority,expected_version,reason,recorded_at`，managed 带 `content` 或 external 带引用与版本 |
| `reflect` | POST 人工建议：域参数、`text,target_kind,target_id,target_version,recorded_at`，不改目标权威 |
| `continuity` | POST 域参数、`epoch_id,sources,recorded_at`；须有宿主确认 epoch，默认实例会明确拒绝 |
| `capabilities` / `provider_status` / `usage` | GET 实际接线能力 / `module` 状态 / 用量分页 |
| `configure_provider` / `call_provider` | POST 模型配置 / 显式输入调用，见下节 |
| `build_local_index` / `local_search` | POST 显式建本地向量索引 / GET 查询；须先由 Python 提供 embedder |

`limit` 为 1..100。普通列表 `offset` 为行偏移；内在领域为上页最后 `seq`，Persona 为最后 `version`；Self Model 仍为行偏移。Web 自动处理这些游标。`/health` 仅返回存活状态，不读全库。

MCP 使用已安装官方 `mcp` 的 stdio：

```json
{"mcpServers":{"thalemora":{"command":"/absolute/path/.venv/bin/thalemora",
  "args":["--data","/absolute/path/state","mcp"]}}}
```

原有 `list_agents/search/history/inner_state` 四个读取工具的参数和返回方式保持兼容；另增加 `memory/evidence/import_history/remember/revise_memory`。stdio 依赖可信本地进程上下文，能访问同一数据根的进程已具有此 owner 的管理权限；不启动第二套 SQLite owner。

### PRE6-2：HTTP MCP 与人工记忆操作

同一 `thalemora --data ./state serve` 同时提供管理台、HTTP API 和官方 SDK 的 **Streamable HTTP** 入口 `http://127.0.0.1:8765/mcp`。复用现有 Application/Registry/数据根及官方 session manager 生命周期，不启动第二个进程，不提供旧 SSE transport。stdio 仍使用上面的 `mcp` 命令。

HTTP MCP 的每次请求（含初始化、会话读取和删除）均须携带 `Authorization: Bearer <THALEMORA_TOKEN>`；缺失或不匹配返回 HTTP 401。不要将 token 写进 URL、命令行参数、日志或共享配置。该 token 代表本实例完整 owner，**不是按 app 缩权的凭据**。工具的 Agent、app、channel 授权仍由 Registry/Service 检查；private 不继承 main。MCP 不接受请求自报 owner/Agent 作者身份，也不创建认证用户。

使用官方 Python 客户端（token 从环境读取，不打印）：

```python
import asyncio
import os
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    async with httpx.AsyncClient(headers={
        "Authorization": "Bearer " + os.environ["THALEMORA_TOKEN"]
    }) as http:
        async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                print([tool.name for tool in tools.tools])

asyncio.run(main())
```

| 新增 MCP 工具 | 参数与真实回执 |
| --- | --- |
| `memory` | `agent_id, app_id`；可选 `channel="main", limit=20, offset=0`；返回记忆正文和 `current_revision_id` |
| `evidence` | 上述域及分页参数，加 `card_id`；返回各修订来源证据、`resolution`、当前来源正文；人工来源 `verified=false` |
| `import_history` | **显式** `agent_id, app_id, channel, events`；events 为 1..100 个来源 envelope；返回各事件的 `status/version/current_version` |
| `remember` | `agent_id, app_id, source_event_id`，可选 `channel="main"`；仅把已有未删除事件原文写成人工记忆，返回 `card_id/changed/current_revision_id` |
| `revise_memory` | `agent_id, app_id, card_id, content, expected_current_revision_id`，可选 `channel="main"`；只做明确的人工正文修订，返回实际 `card_id/changed/current_revision_id` |

先通过现有管理台/API 注册 Agent、授权来源，然后导入 envelope。例如工具 `import_history` 的 arguments：

```json
{
  "agent_id": "<已注册的 Agent UUID>",
  "app_id": "demo-app",
  "channel": "main",
  "events": [{
    "app": "demo-app", "channel": "main", "source_event_id": "demo-1",
    "version": 1, "role": "user", "text": "合成示例：一起整理旅行计划。",
    "occurred_at": "2026-10-15T10:00:00+08:00",
    "conversation_id": "demo-conversation", "epoch_id": null,
    "content_hash": null, "hash_verification": "unknown", "raw_event_id": null,
    "deleted": false
  }]
}
```

envelope 沿用已有来源合同，`app/channel` 必须匹配请求域；来源 ID、版本、时间、角色、原文、会话/epoch、hash 及验证状态、raw ID 和删除标记均保留。重复相同版本返回 `unchanged`，同版本异文失败，较旧版本返回 `ignored_stale`。逐事件提交，批中失败前已提交项保留，可按原 envelope 幂等重试。**导入仅保存授权历史，不自动运行 Writer，不自动生成 Memory，也不是可信 Agent 行动事件。**

`remember` 不接受自由正文或作者参数；每次显式调用会创建一张人工卡片。`revise_memory` 使用现有 `memory.revision_store.update_card` CAS：先从 `memory` 读取当前修订 ID；旧 ID 会得到 MCP `isError=true`，错误包含 expected/current revision；ID 正确但正文相同则 `changed=false` 且不追加版本。正文不能为空。固定人工 `manual_ui/manual_edit`，不接受自选 origin，不改其他字段来源语义或 Self Model。原始原话证据留在旧修订链中；新修订另记录 `mcp_explicit_manual_revision`，证据显示 `resolution=manual_revision`、`verified=false`，不把旧原话冒充新正文的证明。旧原话后续变化仍可标示 `source_changed/deleted`，不会重写人工 Memory。

失败通过真实 MCP `isError=true` 返回，不包装成写入成功。标准 MCP 不保证工具结果都是 structuredContent：原有列表工具保持原 TextContent 表示，客户端应按 SDK 的工具 schema/内容处理。

## 无 Key 与模型接线

词法搜索直接读取历史原文，中文、英文和 emoji 均可从任意位置查询。查询去除首尾空白后接受 1–200 个 Unicode 字符（按码点计数）；1–2 字符使用持久 Btree 短 gram，英文不区分大小写，3–200 字符沿用 FTS5 trigram。首次接线某个 Agent 的档案时，在一个事务内为已有有效正文构建去重的 1/2 字符索引；后续新增、修订和 tombstone 由 `Archive._upsert` 在档案写入事务内增量维护，包括 P2 直接导入/同步。查询不重新回填、不扫描原表、不加载模型；FTS5 仍由既有触发器维护。授权仍按 Agent、app 和 main/private 检查，结果按来源事件 ID 稳定排序，每页 1–100 条，超出末页返回空列表。

Memory 是原文人工入库与版本证据，Water 支持 Lake/Stream/Branch、归属调整及既有审计。历史编辑后证据会标明 `source_changed`，删除后不再展示当前来源正文；保留的人工 Memory 正文不自动重写。

本地向量复用 P1 `RetrievalIndex`，需要 `Application(root, embedder=("本地模型标识", already_loaded_embedder))`。embedder 实现 `encode_many(texts, batch_size=16)` 和 `encode_one(text)`，返回同维度归一化向量。显式运行 `build_local_index` 后查询；Memory 变更后需人工重建派生索引。不默认安装 SentenceTransformer，不下载权重。索引重建与 P1 向量查询属于显式慢路径，不在 Web 页面加载中触发。

Provider 配置按 `embedding/writer/gatekeeper/reflection` 各自持久保存 `base_url/model/key_env/capability/enabled/call_budget/token_budget`。Key 在服务器环境变量中，不写入配置或回包。初始 `unconfigured`；Key/URL/model 齐全但未开为 `disabled`；能力与用途不符为 `unsupported`；调用失败为 `error`；可调用为 `ready`。配置后须显式 `call_provider(module,text,max_tokens=128)` 才请求 `/embeddings` 或 `/chat/completions`。

**模型接线**：显式 call_provider 只返回供应商结果；配置后的独立 worker 则将 Writer/Gatekeeper/Reflection 接入领域管线并记录持久回执。默认不开启、启动 Web 不自动运行维护。Reflection 只提建议，不修改 Self Model 或 Persona。

调用预算为累计上限，事务预占 pending 及失败调用，防止并发突破调用次数。token 预算为已返回真实累计 token 的准入阈值，达到后阻止下一次；单次请求的总输入 token 由 Provider 决定，因此可能在最后一次调用越过阈值。错误/未返回 usage 的 token 为未知，不以字符数估算实际用量。费用未提供统一可信账单接口，`actual_cost` 始终为 `null`。日志保留失败类型及预算阻止，不保存输入/Key。预算独立于各 Agent，作用域是单用户实例的用途模块。

## 证据与 Agent 前置条件

HTTP 不接受用户自报的角色/已完成事件，不提供自主创作入口。可信 Python 宿主注入 `host_reader` 后可调用 `publish`；原 P3 `verified_event`、Self Model Gate 与 epoch 确认决定结果。Emotion、Diary、Dream、Whisper、Self Model 本人语义不由维护器生成。Continuity 只构造显式已保存来源的候选，不接管 Runtime 或 Prompt。Persona 仅配置单权威，不注入念念。

Scene 与 Gatekeeper 可由独立 worker 在后台驱动，生命周期批处理仍由 Python 宿主按需接入。可信 Agent reader/Runtime 绑定由宿主管理；念念已按自己的生产 Gate 接入，不代表任意外部应用可以自报身份或免验收切权。

## 全量历史导出与恢复

Web 仅导出当前页。大历史使用 CLI 流式 JSONL，每次至多读取 100 条，包含全部当前 envelope 和删除标记；P2 本来就不保存旧编辑正文。导入前显式创建目标 Agent 并授权同一 app/channel：

```bash
thalemora --data ./state export-history <Agent-UUID> demo-app ./history.jsonl --channel main
thalemora --data ./other-state import-history <目标-Agent-UUID> demo-app ./history.jsonl --channel main
```

完整实例快照需先停止所有 HTTP/MCP/Python 写入；本实现没有在线跨库一致性协议，不把运行中逐库备份宣称为一致快照。然后：

```bash
thalemora backup ./state ./snapshot
thalemora restore ./snapshot ./restored
thalemora --data ./restored serve
```

快照按 SQLite backup API 保存全部独立库（注册/授权、历史/FTS/短 gram、Memory/Water、内在状态、Self Model、Provider 配置及审计），逐库 `quick_check`；恢复到新空目录并重绑定注册库中的绝对路径，沿用 Agent ID。不会覆盖原实例。管理 token 和环境中的模型 Key 不在快照内，恢复时另设。快照保留 private 内容，应按原数据访问权限保管。合成恢复测试覆盖新增历史、证据、内在状态、用量和 private 拒绝；不是生产迁移验收。
