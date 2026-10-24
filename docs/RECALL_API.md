# 聊天前召回 API

应用在向模型发送消息前调用 `POST /api/recall`，把返回的记忆文字加入本轮上下文，再按自己的方式请求聊天模型。

洄海不代理模型、不持有客户端模型 Key，也不会在这一步运行后台整理。MCP 工具调用是另一条按需路线。

## 生成和撤销召回凭据

先在管理台注册 AI，并授权对应的来源与通道。管理端使用 `THALEMORA_TOKEN`，插件只保存独立签发的召回凭据。

管理员在运行服务的设备上，用相同 Python 环境执行以下代码。把两处占位内容换成管理台中的 Agent ID 与来源名称；管理密钥从已设置的环境变量读取：

```python
import json
import os
import urllib.request

payload = {
    "agent_id": "这里填写 Agent ID",
    "app_id": "这里填写已授权的来源名称",
    "channel": "main",
}
request = urllib.request.Request(
    "http://127.0.0.1:8765/api/issue_recall_token",
    data=json.dumps(payload).encode(),
    headers={
        "Authorization": "Bearer " + os.environ["THALEMORA_TOKEN"],
        "Content-Type": "application/json",
    },
)
with urllib.request.urlopen(request) as response:
    result = json.load(response)["result"]
print("召回凭据：", result["token"])
print("撤销标识：", result["token_id"])
```

凭据只在签发时返回明文，服务端保存其哈希和范围。保存好凭据与撤销标识，不要公开；没有自动过期，不用时主动撤销。

对应 HTTP 接口：

```http
POST /api/issue_recall_token
Authorization: Bearer <管理密钥>
Content-Type: application/json

{"agent_id":"<Agent ID>","app_id":"demo-app","channel":"main"}
```

撤销：

```http
POST /api/revoke_recall_token
Authorization: Bearer <管理密钥>
Content-Type: application/json

{"token_id":"<撤销标识>"}
```

撤销后，新召回请求返回 401，已开始执行的请求不会被中途取消。管理密钥不能代替召回凭据；召回凭据也不能访问管理 API。

## 请求与响应

```http
POST /api/recall
Authorization: Bearer <专用召回凭据>
Content-Type: application/json

{"message":"以前喜欢什么茶？","visible_memory_ids":[],"visible_archive_ids":[],"visible_texts":[]}
```

- `message` 必填，最多 200 个 Unicode 字符。
- 其余三个字段可省略，分别填当前上下文已包含的整数记忆 ID、字符串原话 ID和正文。
- 总请求不超过 1 MiB。身份、来源与通道由凭据决定，不在请求中自报。
- 已见内容仅填写本轮实际可见内容，不用上轮已经移除的注入当作本轮已见。

```json
{
  "rendered": "[记忆 1] 我喜欢乌龙茶",
  "memories": [{"id": 1, "revision_id": 1}],
  "archive": [],
  "empty": false,
  "errors": []
}
```

`rendered` 是可以加入上下文的文字；原话引用包含 `id` 与 `version`。无匹配、低信息或已经可见时，返回空文字与 `empty:true`。

可选 Embedding 缺失、关闭、未就绪、服务调用失败或预算耗尽时，Application 本轮召回保留可用的词法记忆＋历史原话，在 `errors` 中注明 `embedding_unavailable`；不使用旧错误/不同模型向量，也不换模型、启用付费服务或在同轮自动重试。可选重排失败继续保留基础结果，注明 `rerank_unavailable`，两者可以同时出现。

已执行的词法召回无匹配时仍为 HTTP 200、`empty:true` 并带对应 warning，表示“词法无匹配、语义不可用”，**不等同于完整语义召回已成功**。HTTP 保持既有五个字段及 `errors: string[]`；Python 内部 `errors` 为模块诊断对象，Embedding 诊断含 `module=embedding`、`code=embedding_unavailable` 和异常类型，不对 HTTP 透传异常正文。低信息问题在模型前直接返回空结果，不额外加载模型或生成无意义 warning。

## 来源更新、删除与人工确认

- 由历史原话生成或通过 `remember` 复制的卡，只有**当前修订的全部证据**仍属于当前 Agent、应用、通道，且来源版本未改变、未删除时，才参与自动召回。`current` 仅代表版本一致，不代表 hash 已验证。
- 任一证据为 `source_changed`、`deleted`、`missing`、`invalid_reference`、`outside_scope` 或 `unsupported`，以及没有证据的卡，默认不作为当前事实自动注入。卡片、修订和旧证据仍保留供人工查看，不做物理删除。
- 已有的人工正文修订（`revise_memory` 实际产生新修订，证据为 `manual_revision`）视为独立确认，不再依赖旧原话存续；仅点击 `remember` 或原文不变的空修订不属于该例外。独立确认不把旧证据升级为 verified。
- 旧卡只抑制它引用的同范围、同事件、**同版本**原话，不抑制更新版本。已见原话参数仍是现有 event ID 提示，只填写本轮确实可见的内容。
- 模型检索/重排结束后会再核对候选来源与正式修订，丢弃期间失效的候选；并不承诺在响应之后取消已交给客户端的旧材料。来源状态变化后的下一轮会重新判断。

证据接口默认先核卡归属，外域/归属不明卡 ID 拒绝；同域卡的外域或不可解释 envelope 不对 source 调用返回原文或 `source_ref`。管理员显式 `owner-global` 可查看该 Agent 当前通道卡片的历史诊断；不解引用其他通道的当前正文。`current_source_text` 仅在允许范围、来源未删除时提供，`verified` 不会被本地版本判定擅自改为 true。

## extend 补充记忆（兼容性与证据）

- canonical `content` / `current_revision_id` 不变；`memory` 增加 `extensions` 数组（包括停用/失效历史）。扩展含 `extension_id`、`card_id`、`base_revision_id`、自身 `content` / `evidence_refs`、存储 `status`、动态 `eligible` / `eligibility`。客户端不能把整份管理读取直接当作当前事实注入。
- 每个有效扩展是独立检索单元，键为 `memory:<card_id>:extension:<extension_id>`；保留原 `source_kind=memory_card`、`source_id=card_id`，不改数据库 schema。不能再假定一个 card ID 只有一个检索 chunk。
- 只自动消费：扩展 `active`、基础卡 `fresh` / episodic / 未被 active lineage 淘汰、基础修订仍为当前且允许扩展、扩展**自己的全部证据**均 `current`。不继承基础卡证据，也不继承人工确认豁免。基础 v1 的来源失效不妨碍同事件 v2 的独立扩展，但不会附带注入基础 v1 正文。
- `correct` / `supersede` / 人工修订以及任何使 current revision ID 改变的刷新，均让旧扩展成为 `stale_base_revision`；不自动搬到新修订、新卡或重写状态。可由现有 Writer/Gatekeeper 在当前合法基础修订上重新建立扩展。trashed、非 fresh、retired lineage 或非 active 扩展停止使用；历史保留。
- 扩展来源 changed/deleted/missing/坏引用/越域/不支持/无证据时停止自动注入。历史扩展引用沿用 F1 的范围＋事件＋版本抑制；旧引用不挡新版原话。模型结束后重新核验扩展身份、基础修订及证据。
- `evidence` 合并修订和扩展证据后分页：先按修订证据 ID，再按扩展 ID/证据 ID。新增 `evidence_kind=revision|extension`，`id` 只在各自表内唯一；扩展证据带 `extension_id`、`base_revision_id`、`extension_status`、`eligibility`。`resolution` 始终表示来源状态，`verified` 不因版本相同而升级。
- HTTP recall 的扩展引用为 `{"id":1,"revision_id":1,"extension_id":2,"base_revision_id":1}`；正文标签为 `[记忆 1 扩展 2 基础修订 1] ...`。HTTP 仍只暴露标识，不透传完整证据正文；授权管理端通过 `evidence(card_id)` 查证。MCP `memory/evidence` 沿用相同服务返回，不新增工具。
- `visible_memory_ids` 仍表示整张卡（含扩展）；`visible_archive_ids` 对扩展仅按它自己的证据匹配。Related 每轮条数、截断和 token 预算不变，多扩展分别参与排序，不承诺同轮全部注入。
- 新消费的扩展在应用读取、evidence、本地检索与 Related 中按 Agent/app/channel 过滤；应用本地检索在模型前刷新过滤，返回前再次核验。低层 `RetrievalIndex` 只有 Memory 路径，提供结构投影而不声称 Archive 来源有效；它不是授权/当前事实注入入口。canonical 与扩展现共用同一历史归属判定：默认 source-scoped，仅显式 owner-global 跨应用管理；本地 source/owner 缓存隔离，owner 索引不进入本接口。unknown/conflict 不自动认领或召回。专用召回凭据仍固定来源范围，不接受 scope 参数。

## 向量数值、维度与派生缓存

- Memory 的整库/单卡、raw 的 rebuild/sync/refresh、query 和缓存复用使用同一契约：**一维、非空、实数（不接受 bool/字符串/复数）、有限、非零**。有限向量先按最大模缩放、再 L2 归一化，保存为 float32；不 flatten、不补维/截维、不丢坏行凑成功。多行 batch 数量或维度不一致则整批向量不发布，不写成功回执；词法元数据可能已按 F3 第一段事务发布。
- 同一派生缓存的 `model_dimensions` 按完整模型 ID 持久固定首次成功文档向量维度，Memory/raw 共用；同 ID 维度漂移、query 不匹配、有效缓存模型不匹配均明确拒绝。模型切换须使用新 ID 并重建。模型 ID 必须唯一对应权重/配置版本；无法自动识别宿主在同 ID、同维度背后偷偷换权重。API 响应提供 `model` 时必须与显式配置一致（别名不一致也拒绝，不猜映射）；一轮 API embedder 绑定配置身份，期间改模型不能把新模型向量标成旧模型。
- 点积仅在两端满足单位向量契约后作为 cosine，分数不先四舍五入再用于 `0.65` 门槛。float32 存储仍有正常的数值精度限制。
- **可重建 retrieval schema 1/2 → 3**：附加 `vector_contract`（当前 1）及模型维度表；旧行默认为 0，即便 F3 generation/hash/model 已绑定也不能证明曾规范化。保留旧 bytes/FTS/正文，不在初始化加载模型或触碰 Memory/History 权威 schema；首次需要语义时按来源惰性重建。raw 仍需显式 rebuild/sync/refresh。带新标记却零值/非有限/非单位/坏维度的缓存是明确契约错误，不会静默修复后冒充原来正确。
- F3 的来源前 generation fence、元数据 CAS、行 generation/hash/model 绑定、回填 rowcount、陈旧计算无成功回执均保留；模型推理及新向量转换/归一化仍在 SQLite 写事务之外。缓存复用校验不调用模型。
- 两个真实线程/独立连接同时初始化旧缓存时，WAL 模式切换可能立即返回 SQLite BUSY；仅该初始化语句使用约 2.1 秒内的有界重试，持续 BUSY 或其他数据库错误仍抛出，不转成 Embedding warning，不重试模型。
- 不支持旧 schema 1/2 写进程与 schema 3 混写；使用前须停旧索引写进程或隔离可重建缓存。回退旧代码使用其自己的缓存，不可只改 schema 数字。这里是兼容要求，不表示已执行生产升级。

## 降级边界（不是全系统吞错）

- 仅 `Application.recall_for_turn` 自动保留词法＋原话；授权和来源检查在模型之前，最终来源/修订/扩展复核不变。降级重读来源前取新 fence，不用旧快照覆盖并发新发布。
- 明确可选故障：`ProviderUnavailable`，或模型方法边界的 `TimeoutError` / `ConnectionError`；内置本地库加载的 ImportError/OSError/RuntimeError 和原生推理 RuntimeError 被翻译为可选服务不可用。宿主自定义 embedder 若要表达模型故障，应显式抛 `ProviderUnavailable`，任意 RuntimeError/TypeError/ValueError 不自动当作模型停机。
- 数值/维度/模型身份契约错误、权限、数据库、范围及编程错误继续 fail-closed，不映射成 `embedding_unavailable`。HTTP 对未知内部诊断也不返回可选降级成功。既有 Rerank 降级策略保留，未扩展到其他模块。
- `Application.local_search` / `build_local_index` 和低层 `RetrievalIndex` / raw API **保留 fail-closed**；传 `embedder=None` 才是调用方明确选择的低层纯词法路径（单卡 Memory 的 None 参数沿用“使用实例 embedder”，要纯词法需实例也无 embedder）。这些不是自动故障降级 API。关闭 Embedding 的 Application 召回不会加载以前坏掉的模型。
- API 网络超时仍为 Embedding 5 秒、Rerank 3 秒，调用无自动重试；HTTP 整轮最多等待 5 秒，若整体截止时间先到仍返回 504，不承诺超时计算能立即停止或必能赶在 HTTP 截止前完成词法降级。新一次显式召回可再次尝试已配置的模型，不是同轮后台重试。预算预约/在途计费（F6）未改变。

## 错误处理

| 状态码 | 含义 |
| --- | --- |
| 400 | 请求、消息或已见字段不合法。 |
| 401 | 凭据缺失、无效或已撤销。 |
| 403 | 来源授权已失效。 |
| 405 / 413 | 方法不支持 / 请求过大。 |
| 500 / 503 | 服务内部错误 / 召回组件不可用。 |
| 504 | 服务等待超时。 |

错误返回 `{"error":"稳定错误码"}`，不假装成功的空召回。应用应明确记录召回失败并继续原聊天，不输出凭据或私人响应。

服务端最多等待 5 秒；应用宜设置更短的等待时间。服务超时不保证底层计算立即终止，也不会自动重试。

## 放进模型请求

1. 在应用真实的发送前入口等待召回。
2. 每轮先清除自己上次注入的槽位。
3. 只插入本轮返回的非空 `rendered`，按应用约定选择角色和位置。
4. 继续原发送流程；不要把注入资料另存为用户原话。
5. 聊天保存后，如需同步，再调用[历史导入接口](USAGE.md#导入历史)。

浏览器跨源配置见[插件教程](PLUGIN_SETUP.md#浏览器跨设备连接)。凭据持有者可以读取绑定范围内的相关记忆，应只交给获授权的应用。远程传输使用 HTTPS 或可信网络。

## Python 发送前示例

宿主传入 HTTP 客户端、原模型发送函数和错误记录函数。异常只记录类型，不记录凭据和正文。

```python
import asyncio
import httpx


async def before_send(message, context, *, recall_url, recall_token,
                      client, send_upstream, report_error, **visible):
    outgoing = list(context)
    try:
        response = await asyncio.wait_for(
            client.post(recall_url, headers={"Authorization": "Bearer " + recall_token},
                        json={"message": message, **visible}, timeout=2.0),
            timeout=2.0)
        response.raise_for_status()
        payload = response.json()
        rendered = payload["rendered"]
        if not isinstance(rendered, str):
            raise ValueError("invalid rendered")
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError, KeyError):
        report_error("本轮召回失败或超时，已跳过记忆")
    else:
        if rendered:
            outgoing.append({"role": "system", "content": rendered})
    return await send_upstream(outgoing)
```
