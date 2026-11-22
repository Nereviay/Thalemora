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
| `revise_memory` | 明确修改一条人工记忆。 |
| `trash_memory` | 把记忆移入回收站，保留卡片、修订和来源证据。 |
| `restore_memory` | 将回收站中的记忆恢复到移入前的状态。 |

先在管理台注册 AI、授权来源。工具调用仍需正确的 Agent ID、来源和通道。私人通道不会自动继承普通通道权限。

`memory`、`evidence`、`revise_memory`、`trash_memory`、`restore_memory` 默认使用 `scope="source-scoped"`，按 Agent、应用和通道检查卡片归属。管理员可显式传 `scope="owner-global"` 管理所选 Agent 当前通道的全部来源，此时 `app_id` 可传 `null`。同一实例的管理密钥可以选择该 owner 的其他 Agent。给单个 AI 接入共享实例时，应由可信宿主持有管理密钥并固定该 AI 的 Agent ID、应用、通道和来源范围。`remember` 始终从已授权来源创建卡片。见[范围说明](USAGE.md#查看范围应用来源与管理员)。

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
