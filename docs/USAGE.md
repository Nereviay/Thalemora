# 使用手册

第一次使用请先看[入门教程](GETTING_STARTED.md)。本页介绍导入、搜索、API 和备份，适合需要管理数据或接入应用的使用者。

## 数据怎么组织

每个 Agent 有自己的记忆档案。一个 Agent 可以接收多个应用来源，来源由 `app_id` 区分，再按 `main`、`private` 通道隔离。先注册 Agent，再授权来源；授权 main 不会同时授权 private。

- **历史原文**：保存应用提供的消息、版本和删除标记。
- **记忆卡片**：保存从历史整理或人工保留的内容，支持修订和查看来源。
- **水系**：用 Lake、Stream、Branch 组织记忆及其归属。
- **内在状态**：记录反思、Persona 等内容；需要由宿主确认身份的写入，应按[应用接入说明](INTEGRATION.md)配置。

## 导入历史

管理台可导入 JSON 数组，每个文件 1–100 条。[示例文件](examples/p4_history.json)可以作为格式参考。下面是一条消息：

```json
{
  "app": "demo-app",
  "channel": "main",
  "source_event_id": "demo-1",
  "version": 1,
  "role": "user",
  "text": "一起整理旅行计划。",
  "occurred_at": "2026-10-14T10:00:00+08:00",
  "deleted": false,
  "hash_verification": "unknown"
}
```

消息 ID 应来自原应用，同一条消息编辑后保持 ID 并增加版本。相同版本、相同内容重复导入不会新增记录；相同版本却内容不同会报错；旧版本会被忽略。批量导入逐条保存，遇错前已经保存的条目会保留，可用原数据重试。

导入只保存历史。需要人工保留记忆时，在管理台对已有原文使用“记住”；需要自动整理时，配置[后台自动记忆](AUTOMATIC_MEMORY.md)。

## 搜索、修订和证据

原文搜索支持中文、英文和 emoji，查询长度为 1–200 个 Unicode 字符，无需模型 Key。向量搜索需要另行配置本地模型或模型接口。

人工记忆保留原文来源和修订记录。来源后来被编辑，证据会显示 `source_changed`；来源被删除后，不再展示其当前正文。已有人工记忆的正文不会因此自动重写。

通过 MCP 修订记忆时，先读取卡片的 `current_revision_id`，再把它作为 `expected_current_revision_id` 提交。这样可以避免覆盖他人刚做的修改。每次明确调用 `remember` 都会创建一张人工记忆卡，不应把它当成重复调用也只生成一张卡的接口。

### 查看范围：应用来源与管理员

`memory`、`evidence`、`revise_memory` 和本地索引/搜索默认 `scope="source-scoped"`：只处理指定 `agent_id/app_id/channel` 的卡片。列表先按归属筛选再分页；外域或归属不明的卡 ID 返回 `card_outside_scope`，不返回其正文、证据或当前修订 ID。来源后来改变或删除不影响历史归属；自动召回仍检查证据时效。

需要跨应用管理时，显式选择 `scope="owner-global"`；它只覆盖**该 Agent 的当前通道**，不跨 Agent、owner 或 main/private。管理台 Memory 页可切换查看范围并查看归属诊断。旧卡无法唯一解释归属或存在冲突时，仅管理员可查看/修订；编辑不会自动认领或迁移归属。

此模式是查看/操作范围，不是身份凭据。只有现有管理凭据、受保护 MCP 和可信本机 Python 宿主可用；专用召回凭据不接受此参数。owner 列表可不提供 `app_id`；需卡 ID 的 MCP 工具可传 `app_id: null`。例如：

```python
print(s.memory(agent, "demo-app"))  # 默认只看该来源
print(s.memory(agent, scope="owner-global"))  # 同 Agent/main 的全部来源
s.water_node(agent, None, "lake", "lake", "旅行湖", scope="owner-global")
```

Water 第一版整个管理面（包括节点、移动和成员）仅支持显式 `owner-global`；默认 source 调用返回 `scope_not_supported`，不假装按 app 分树。管理台会明确显示跨来源范围并发送此参数。本地 source/owner 索引各自隔离；owner 搜索仅为管理查看，不接入自动召回。

这是默认范围收紧，旧管理调用若确实需要跨应用，必须显式指定 owner 模式。HTTP 未开放 `revise_memory`；人工修订仍走既有 MCP/Python 入口。

## Python 接入

在安装洄海的 Python 环境中运行：

```python
from thalemora.app import Application

s = Application("./state")
agent = s.register_agent("旅行助手")["agent_id"]
s.grant_source(agent, "demo-app", "main")
events = [{
    "app": "demo-app", "channel": "main",
    "source_event_id": "demo-1", "version": 1,
    "role": "user", "text": "一起整理旅行计划。",
    "occurred_at": "2026-10-14T10:00:00+08:00",
    "deleted": False, "hash_verification": "unknown",
}]
s.import_history(agent, "demo-app", events)
print(s.search(agent, "demo-app", "旅行计划"))
print(s.remember(agent, "demo-app", "demo-1"))
```

本地 Python 进程可以管理该数据目录。不要把外部请求提供的身份直接当作管理员或可信 Agent 身份。

## HTTP API

启动 `thalemora --data ./state serve` 后，管理台和 API 共用同一服务。请求需要：

```http
Authorization: Bearer <THALEMORA_TOKEN>
```

管理 token 拥有整个实例的管理权限。聊天插件应使用[专用召回凭据](RECALL_API.md)，不要把管理 token 填入插件。

读取通常使用 `GET /api/<操作名>`，参数放在 query；写入使用 `POST /api/<操作名>`，参数放在 JSON 对象。响应为 `{"result": ...}`；失败返回对应 HTTP 状态码及 `{"error": ...}`。召回接口使用单独的返回格式，见[接口说明](RECALL_API.md)。

| 操作 | 方法与用途 |
| --- | --- |
| `list_agents` / `register_agent` | GET 查看 Agent；POST 用 display_name 注册 |
| `sources` / `grant_source` | GET 查看来源；POST 授权 agent_id、app_id、channel |
| `history` / `import_history` | GET 查看历史；POST 导入 events |
| `search` | GET 按 query 搜索历史 |
| `memory` / `remember` / `evidence` | GET 查看记忆；POST 按 source_event_id 记住原文；GET 按 card_id 查看证据 |
| `water` / `water_node` / `water_move` / `water_memberships` | 查看节点、创建节点、调整归属、查看成员 |
| `inner_state` | GET 按 kind 查看内在状态 |
| `configure_persona` / `reflect` | POST 配置 Persona 或提交人工反思建议 |
| `continuity` | POST 根据已保存来源生成连续性候选，需要宿主确认的 epoch |
| `capabilities` / `provider_status` / `usage` | GET 查看可用功能、模型配置状态及用量 |
| `configure_provider` / `call_provider` | POST 配置模型或明确发起一次调用 |
| `build_local_index` / `local_search` | POST 建本地向量索引；GET 查询 |

通常需要 `agent_id`、`app_id`、`channel` 来指定数据范围。例如：

```text
GET /api/history?agent_id=<Agent-UUID>&app_id=demo-app&channel=main&limit=20&offset=0
```

分页 limit 为 1–100。普通列表的 offset 是行偏移；内在状态使用上页最后的 seq，Persona 使用最后的 version，Self Model 使用行偏移。管理台会自动处理分页。

支持工具调用的客户端可优先按 [MCP 教程](MCP.md)接入。

## 历史导出

管理台导出当前页。需要导出全部历史时，使用 CLI：

```bash
thalemora --data ./state export-history <Agent-UUID> demo-app ./history.jsonl --channel main
```

文件是 JSONL，包含当前消息版本和删除标记，不包含每次编辑前的旧正文。导入另一实例前，先在目标实例注册 Agent、授权同名 app 和通道：

```bash
thalemora --data ./other-state import-history <目标-Agent-UUID> demo-app ./history.jsonl --channel main
```

## 完整备份与恢复

先停止使用同一数据目录的 Web、MCP、worker 和其他写入进程，再运行：

```bash
thalemora backup ./state ./snapshot
thalemora restore ./snapshot ./restored
thalemora --data ./restored serve
```

每次使用新的快照目录，恢复目标也必须是新的空目录。备份保留 Agent ID、授权、历史、记忆、修订、内在状态及模型配置。恢复会调整注册库中的数据路径。

管理 token 和环境变量中的模型 Key 需另行保存。快照可能包含私密内容，应按原数据的权限保管。

## Docker 数据备份

Compose 默认把数据保存在命名卷中。不要用 `down -v` 来做升级或备份，它会删除卷。

在项目根目录运行，保持 `THALEMORA_TOKEN` 环境变量可用，并先停止其他共享数据的 worker：

```bash
mkdir -p backups
docker compose -f deploy/compose.yaml stop
docker compose -f deploy/compose.yaml run --rm --no-deps -v "$PWD/backups:/backups" thalemora thalemora backup /data /backups/snapshot
docker compose -f deploy/compose.yaml up -d
```

宿主机的 `backups/snapshot` 就是备份。可在安装了洄海的 Python 环境中运行 `thalemora restore ./backups/snapshot ./restored`，先恢复到新目录检查。要供容器使用，再将恢复后的目录挂载为容器的 `/data`，不要直接覆盖正在使用的卷。

更新步骤见[维护与升级](MAINTENANCE.md)。
