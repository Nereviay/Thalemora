# 连接 MCP 记忆工具

MCP 让聊天软件或 AI 调用洄海工具。**工具连上不等于每轮自动召回。** 想在发送前自动加入记忆，优先看[插件教程](PLUGIN_SETUP.md)。

## Streamable HTTP

先启动洄海：

```bash
thalemora --data ./state serve
```

在客户端添加 MCP 服务：

| 设置项 | 填写内容 |
| --- | --- |
| 类型 | Streamable HTTP，不是 SSE |
| 地址 | `http://127.0.0.1:8765/mcp`；远程服务换成实际地址 |
| 请求头名称 | `Authorization` |
| 请求头值 | `Bearer 你的管理密钥`，中间一个英文空格 |

管理密钥是运行服务时的 `THALEMORA_TOKEN`，具有实例管理权限，不能当成插件使用的受限召回凭据。不要把密钥放进 URL。

连接后，在目标助手的设置中启用这个 MCP 服务。例如 RikkaHub 需要在助手设置中选择要使用的 MCP；服务名称可以使用 `ThalemoraMCP`，避免特殊字符。

## 提供哪些工具？

| 工具 | 用途 |
| --- | --- |
| `list_agents` | 列出可用 AI 身份。 |
| `search` | 搜索授权的历史原话。 |
| `history` | 分页读取历史。 |
| `inner_state` | 读取内在状态。 |
| `memory` | 读取记忆和当前修订。 |
| `evidence` | 查记忆来源。 |
| `import_history` | 导入已授权来源的聊天记录。 |
| `remember` | 将一条已导入原话保存为人工记忆。 |
| `add_manual_memory` | 直接保存用户委托记录的笔记或摘要。 |
| `revise_memory` | 明确修改一条人工记忆。 |
| `trash_memory` | 把记忆移入回收站，保留卡片、修订和来源证据。 |
| `restore_memory` | 将回收站中的记忆恢复到移入前的状态。 |

先在管理台注册 AI、授权来源。工具调用仍需正确的 Agent ID、来源和通道。私人通道不会自动继承普通通道权限。

`memory`、`evidence`、`revise_memory`、`trash_memory`、`restore_memory` 默认使用 `scope="source-scoped"`，按 Agent、应用和通道检查卡片归属。管理员可显式传 `scope="owner-global"` 管理所选 Agent 当前通道的全部来源，此时 `app_id` 可传 `null`。同一实例的管理密钥可以选择该 owner 的其他 Agent。给单个 AI 接入共享实例时，应由可信宿主持有管理密钥并固定该 AI 的 Agent ID、应用、通道和来源范围。`remember` 始终从已授权来源创建卡片。见[范围说明](USAGE.md#查看范围应用来源与管理员)。

### 保存人工笔记

调用 `add_manual_memory(agent_id=目标AgentID, app_id="chatgpt", content="要记住的内容")`。目标 Agent 须已获 `chatgpt/main` 授权。笔记写入该 Agent 的正式 Memory，证据类型为 `manual_note`，可以用 `memory` 和 `evidence` 核对。历史档案只保存有来源的事件。更新笔记用 `revise_memory`，移入回收站和恢复沿用下方步骤。

### 手动导入 ChatGPT 官方导出

拿到自己的官方数据导出后，在洄海源码根目录使用已装好项目依赖的 Python 执行。第一条只预览计数。确认这份文件里的可见对话都适合交给目标 Agent 后，再用第二条写入已有的本地数据目录。把示例路径和 Agent ID 换成实际值。

```bash
python tools/import_chatgpt_export.py --export ./export.zip
PYTHONPATH=src python tools/import_chatgpt_export.py --export ./export.zip --data ./state --agent-id '目标 Agent ID'
```

命令也接受单个对话 JSON。ZIP 仅读取 `conversations.json` 和数字分卷（如 `conversations-001.json`）。预览按原因统计跳过项。导入仅写授权的 `chatgpt/main` 历史档案，无需模型或联网。以后有新导出时手动重跑，MCP 连接本身不会自动同步聊天。

**默认跳过其他自定义 GPT 的对话**（导出中的 `gizmo_id` 不为空）；同一 Agent 的自定义 GPT 须先确认身份，再通过 `--include-gizmo-id ID` 显式加入。

每个有来源 ID 的可见用户或助手纯文本节点单独保存，包含不同编辑分支。来源键由对话 ID 和消息 ID 组成，缺消息 ID 时使用源节点 ID。正文保留字符串片段和空白，消息时间转为 UTC，缺失时间或关联字段保留为空。隐藏分析、工具日志、混合附件、媒体等计入跳过项。档案不重建分支顺序。SHA-256 仅针对转换后的正文，标记为 `legacy_unverified`，不提供官方认证。

重复导入同一事件和内容会返回 `unchanged`。同一来源键的版本固定为 1，遇到同版本不同内容会报告 `version_conflict` 并停止。导入按最多 100 条一批处理，出错前已写入的记录保留，回执计数仅包含完整成功的批次。后续导出中缺席的节点会保留在档案中。

### 删除与恢复

1. 用 `memory` 读取卡片，将 `id`、`current_revision_id`、`trash_version` 分别作为 `card_id`、`expected_current_revision_id`、`expected_trash_version` 传给 `trash_memory`。
2. 删除后，普通记忆列表和记忆召回会跳过该卡。用相同范围调用 `memory(view="trash")` 可重新找到它，`evidence` 仍可查原有修订证据。
3. 用回收站条目或删除回执中的两个版本值调用 `restore_memory`。恢复后再次按原状态展示和召回。旧库中已在回收站且没有原状态记录的卡恢复为 `fresh`。

每次实际移入或恢复只增加 `trash_version`，正文修订 ID 保持原值。版本冲突时请重读卡片。正文版本匹配且卡片已在目标状态时，重复调用返回 `changed=false`。

## 本机 stdio

支持启动本地程序的 MCP 客户端，也可以使用：

```json
{
  "mcpServers": {
    "thalemora": {
      "command": "/absolute/path/.venv/bin/thalemora",
      "args": ["--data", "/absolute/path/state", "mcp"]
    }
  }
}
```

把程序和数据目录改成自己的绝对路径；Windows 程序位于 `.venv\\Scripts\\thalemora.exe`。stdio 直接访问本机数据目录，只交给你信任的客户端使用。

## 排查

- 401：检查密钥与 `Bearer` 后的空格。
- 地址打不开：确认服务仍运行，localhost 指向正确设备。
- 看见工具但没调用：确认目标助手启用了 MCP，模型可能自行选择不用工具。
- 导入失败：检查 Agent、来源/通道授权和[导入格式](USAGE.md#导入历史)。

连接 MCP 不会自动复制客户端里的全部聊天记录，也不自动启动后台整理。
