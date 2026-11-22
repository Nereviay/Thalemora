# 接入自己的应用

洄海负责记忆，聊天应用继续负责聊天、模型请求、Prompt、工具执行和消息保存。

## 先接哪部分？

| 功能 | 接入方式 |
| --- | --- |
| 保存聊天历史 | 注册身份、授权来源，使用历史导入或增量同步。 |
| 搜原话、读记忆、查证据 | [Python / HTTP 接口](USAGE.md)。 |
| 让模型按需调用 | [MCP](MCP.md)。 |
| 每轮发送前自动回忆 | [召回 API](RECALL_API.md)。 |
| 自我认识、日记等本人行为 | Python 宿主提供可信行为记录，再调用对应领域能力。 |

同一 AI 可以授权多个来源；来源不能只靠显示名字猜测合并。保留稳定身份、事件 ID、版本与删除状态，重复同步不要制造重复历史。

自动召回不等于自动同步。只拿到发送前草稿，不能把它当作已经保存的完整聊天；应等消息实际落地后再导入。

## v0.9.0 新能力的接入条件

当前正式版为 [v0.9.0](https://github.com/Nereviay/Thalemora/releases/tag/v0.9.0)。下面的公共记忆能力面向能够控制聊天流程的可信宿主，普通 MCP 工具连接不会自动触发它们。

公共记忆选择可为普通聊天、主动联系、寻找活动和行动前核对提供有来源的材料。客户端必须能在相应时机主动调用，并把结果加入最终模型请求；仅能连接 MCP 或搜索记忆，不代表已接通这些触发点。

- 普通聊天继续使用 `Application.recall_for_turn`；传入可信的已见来源、当前可见内容和上一条助手来源，帮助处理短句并避免重复引用。
- 主动联系、活动与行动前材料使用 Python `retrieval.intent.recall_for_intent`。行动前是否需要检查、是否拦截或批准，仍由宿主决定；回忆不代表行动已经完成。
- 自然浮现与余韵使用 `lifecycle.ambient.AmbientEngine`，由可信宿主提供已授权候选、有效修订、已有信号和真实回执，并在投送前核对来源。宿主控制调度、去重和最终上下文，不能把客户端自报字段当作可信行为。接线说明见[开发文档](DEVELOPMENT.md)。

结果中的记忆版本和原话引用用于追溯；来源失效或尚未核实的旧资料不能冒充当前已证实事实。

可信接入方也可为有原话证据的持续事项标记进行中、更新或结束状态。状态须走现有受控记忆修订与证据核验，不能只凭客户端提交一个标记就把事项当作已结束。

**既有 SillyTavern / Operit 插件没有自动升级到新宿主能力。新聊天同步仍需在消息实际保存后单独接线。** 本版不提供新的通用自然浮现 HTTP 开关，也不接管聊天、Prompt 或工具执行。

## 本地检索模型

管理台可选择本地模型目录，也可由 Python 提供已经加载的对象：

```python
from thalemora.app import Application

# my_embedder 提供 encode_one(text) 与 encode_many(texts, batch_size=16)
app = Application("./state", embedder=("本地模型标识", my_embedder))
```

返回同维度、归一化向量。更换模型需要重建派生索引，不改原话或正式记忆。可选重排器可以通过 `app.providers.local_models` 注入 `("模型标识", rerank_callable)`，回调接收 query/docs，返回每条候选的评分。

## 可信行为与内在状态

普通 HTTP 不接受请求自报“AI 本人已经完成某事”。宿主通过 `host_reader` 提供可核查的生成、工具调用和会话事件，才能接入本人创作或自我认识修订。

后台反思仅保存建议，不冒充 AI 作者。Persona 按每个 AI 选择一个配置权威，避免两处同时修改和注入。

## 已有应用的存储适配

源码提供念念适配器与离线工具，供具有相同来源结构的开发者参考；普通独立使用不需要运行它们。

```bash
PYTHONPATH=src python3 tools/niannian_storage_rehearsal.py snapshot SOURCE_DIRECTORY NEW_SNAPSHOT_DIRECTORY
PYTHONPATH=src python3 tools/niannian_storage_rehearsal.py rehearse NEW_SNAPSHOT_DIRECTORY NEW_WORK_DIRECTORY
```

快照只读来源、目标需全新；在线多库复制不是同一时点的一致快照。正式迁移前需停止相关写入，并保全迁移期间新增内容。

`adapters/niannian_history.install(connection)` 与 `adapters/niannian_raw_feed.install_raw(connection)` 为来源建立变更 feed，会修改来源 schema。只在自己授权的副本或迁移窗口执行。

`tools/niannian_history_sync.py` 按保存的游标有界同步，不自动安装 feed，也不自行注册系统定时任务。多个来源投影可能包含相同消息，不能简单相加当作唯一消息数。

不要将真实档案、Prompt、密钥或私有恢复材料放进源码仓库。应用升级按自己的依赖和数据兼容方式安排，洄海发布不会自动接管写入权。
