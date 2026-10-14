# P4 独立实例使用与实际边界

本地 Python/HTTP/MCP 共用 `Application`，管理台直接读取同一 API。当前为 P4 隔离实现，Docker 运行验收以阶段报告为准；P5 公开发布与 P6 生产接入尚未授权。

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

默认仅发布宿主 `127.0.0.1:8765`，命名卷与念念完全隔离。健康检查为 `/health`；API 需要认证。容器内部 `--container` 显式绑定 `0.0.0.0`，不改变 Compose 的宿主绑定。云端模型 Key 默认不传入；确需启用时自行将对应环境变量加入容器 environment。P4 已用隔离临时 Compose project 完成新空镜像构建与容器健康/API/原文/记忆操作验收并清理；这不是生产部署或公开发布验收。

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

提供 `list_agents/search/history/inner_state` 四个工具。stdio 依赖可信本地进程上下文，能访问同一数据根的进程已具有此 owner 的管理权限；不启动第二套 SQLite owner。

## 无 Key 与模型接线

词法搜索直接读取历史原文，中文、英文和 emoji 均可从任意位置查询。查询去除首尾空白后接受 1–200 个 Unicode 字符（按码点计数）；1–2 字符使用持久 Btree 短 gram，英文不区分大小写，3–200 字符沿用 FTS5 trigram。首次接线某个 Agent 的档案时，在一个事务内为已有有效正文构建去重的 1/2 字符索引；后续新增、修订和 tombstone 由 `Archive._upsert` 在档案写入事务内增量维护，包括 P2 直接导入/同步。查询不重新回填、不扫描原表、不加载模型；FTS5 仍由既有触发器维护。授权仍按 Agent、app 和 main/private 检查，结果按来源事件 ID 稳定排序，每页 1–100 条，超出末页返回空列表。

Memory 是原文人工入库与版本证据，Water 支持 Lake/Stream/Branch、归属调整及既有审计。历史编辑后证据会标明 `source_changed`，删除后不再展示当前来源正文；保留的人工 Memory 正文不自动重写。

本地向量复用 P1 `RetrievalIndex`，需要 `Application(root, embedder=("本地模型标识", already_loaded_embedder))`。embedder 实现 `encode_many(texts, batch_size=16)` 和 `encode_one(text)`，返回同维度归一化向量。显式运行 `build_local_index` 后查询；Memory 变更后需人工重建派生索引。不默认安装 SentenceTransformer，不下载权重。索引重建与 P1 向量查询属于显式慢路径，不在 Web 页面加载中触发。

Provider 配置按 `embedding/writer/gatekeeper/reflection` 各自持久保存 `base_url/model/key_env/capability/enabled/call_budget/token_budget`。Key 在服务器环境变量中，不写入配置或回包。初始 `unconfigured`；Key/URL/model 齐全但未开为 `disabled`；能力与用途不符为 `unsupported`；调用失败为 `error`；可调用为 `ready`。配置后须显式 `call_provider(module,text,max_tokens=128)` 才请求 `/embeddings` 或 `/chat/completions`。

**这些用途目前是显式 Provider 请求接口，未接入自动 Writer/Gatekeeper/Reflection 领域管线。** 调用结果只回给操作者，不自动写 Memory、反思或 Self Model。HTTP 支持配置与测试调用，本地 embedding 另由 Python 注入；不声称拥有自动提炼或云端向量同步。

调用预算为累计上限，事务预占 pending 及失败调用，防止并发突破调用次数。token 预算为已返回真实累计 token 的准入阈值，达到后阻止下一次；单次请求的总输入 token 由 Provider 决定，因此可能在最后一次调用越过阈值。错误/未返回 usage 的 token 为未知，不以字符数估算实际用量。费用未提供统一可信账单接口，`actual_cost` 始终为 `null`。日志保留失败类型及预算阻止，不保存输入/Key。预算独立于各 Agent，作用域是单用户实例的用途模块。

## 证据与 Agent 前置条件

HTTP 不接受用户自报的角色/已完成事件，不提供自主创作入口。可信 Python 宿主注入 `host_reader` 后可调用 `publish`；原 P3 `verified_event`、Self Model Gate 与 epoch 确认决定结果。Emotion、Diary、Dream、Whisper、Self Model 本人语义不由维护器生成。Continuity 只构造显式已保存来源的候选，不接管 Runtime 或 Prompt。Persona 仅配置单权威，不注入念念。

Scene、Gatekeeper 自动判定、生命周期批处理尚未接到本应用 API；它们依赖调用方的候选、计划、事务与模型协议，仍可直接使用 P1 Python 核心。P4 没有伪造对应空接口。真实 Agent 的持久证据 reader 与 Runtime 绑定属于之后另行授权的接入工作。

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
