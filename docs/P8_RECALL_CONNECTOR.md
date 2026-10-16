# 聊天前召回协议（P8-3）

仅具备真实 before-context-send / pre-send Hook 的宿主才能自动注入。
手机工作区、电脑或服务器能运行洄海，不等于聊天客户端开放了该 Hook。
此阶段验证标准 HTTP 和合成宿主；真实客户端 Hook 接线与验收属于 **P8-4**。
模型 Key、原聊天请求和发送权仍由宿主持有；洄海不代理聊天，也不运行 Writer/Gatekeeper。
MCP 工具调用不等于聊天前自动召回。

## 管理员签发与撤销

沿用部署时配置的管理 Bearer；管理凭据只留在可信管理端，不交给外部 Hook。
先注册 Agent 并通过已有 `grant_source` 授权，再签发绑定到该来源的独立凭据。

```http
POST /api/issue_recall_token
Authorization: Bearer <ADMIN_TOKEN>
Content-Type: application/json

{"agent_id":"<AGENT_UUID>","app_id":"demo","channel":"main"}
```

```json
{"result":{"token_id":"<短标识>","token":"<随机召回凭据>","agent_id":"<AGENT_UUID>","app_id":"demo","channel":"main"}}
```

明文仅签发时返回；registry.sqlite 只存 SHA-256 哈希、标识及作用域。
管理员保存 `token_id` 用于撤销；令牌无自动过期，丢失或不再使用时显式撤销。

```http
POST /api/revoke_recall_token
Authorization: Bearer <ADMIN_TOKEN>
Content-Type: application/json

{"token_id":"<短标识>"}
```

成功返回 `{"result":{"revoked":true}}`，不存在时为 `false`。
撤销提交后新请求立即返回 401；已经授权并开始执行的请求不会被中途取消。
每次召回都重新检查 Agent 所有权和 source 授权，无身份缓存。
召回凭据不能访问管理 API，管理凭据也不能代替召回凭据。

## 召回请求与响应

```http
POST /api/recall
Authorization: Bearer <RECALL_TOKEN>
Content-Type: application/json

{"message":"以前喜欢什么茶？","visible_memory_ids":[],"visible_archive_ids":[],"visible_texts":[]}
```

只要求 `message`（文本，最多 200 字符）；三个可选已见字段须为数组，
分别包含整数卡片 ID、字符串原话 ID、字符串可见正文。总请求最多 1 MiB。
Agent/app/channel 只来自 token；请求体出现这些字段、source 或其他未知字段返回 400。
已见 ID 均按绑定来源解释，只提交实际已在本次 Context 中的内容。

```json
{"rendered":"[记忆 1] 我喜欢乌龙茶","memories":[{"id":1,"revision_id":1}],"archive":[],"empty":false,"errors":[]}
```

原话引用为 `{"id":"事件ID","version":1}`。引用属于 token 的绑定来源；
响应只提供插入文本和引用标识，不提供评分、完整内部记录或私有异常。
无关、低信息、已见或无可用记忆时，200 返回 `rendered:""`、空引用、`empty:true`。可选的 Rerank 出错时若基础召回已有结果，仍返回 200 和原 `rendered`，`errors:["rerank_unavailable"]` 表示本轮跳过重排；不透出私有异常正文。

失败均返回非 2xx 及 `{"error":"稳定错误码"}`，不伪装为空召回：

| HTTP | 含义 / 错误码 |
| --- | --- |
| 400 | 请求格式、消息或已见字段不合法：`invalid_request` / `invalid_message` / `invalid_visible_items` |
| 401 | 缺失、无效或已撤销的召回 token：`invalid_recall_token` |
| 403 | 来源授权已失效：`source_not_authorized` |
| 405 | 方法不支持 |
| 413 | 请求过大：`request_too_large` |
| 500 | 服务端内部失败：`recall_failed` |
| 503 | 召回组件不可用或核心报告错误：`recall_unavailable` |
| 504 | 服务端 5 秒等待到期：`recall_timeout` |

服务端超时后不再等待同步工作线程，但已启动的本地计算或提供商请求可能继续完成；
不承诺强制终止底层操作，不自动重试。宿主应使用更短的等待预算。
核心沿用现有 embedding/rerank 配置；本次合成验收只用本地词法路径，无网络或付费调用。

## 宿主 pre-send 示例

以下函数需要宿主真正注册到发送前 Hook；依赖已有 `httpx`。
宿主传入 `httpx.AsyncClient()`、自己的上游发送函数和错误记录回调。
只追加 `rendered`，不在客户端重复实现相关性；错误回调不要记录 token、请求体或私有响应。
失败只跳过本轮记忆，仍发送原请求；上游自身失败继续向宿主抛出。

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

`context` 应包含本轮用户消息；宿主按自己的 Context 契约选择插入位置/角色，
并把已有记忆通过可选 visible 字段提交。该函数不持有模型 Key，不发送任何模型请求；
`send_upstream` 才是宿主原有的发送路径。

## 传输及本机信任边界

Bearer 的持有者能读取绑定域内与输入相关的记忆，应只交给该域的获授权 Hook。
本机 HTTP 应仅绑定可信 loopback；同机其他进程仍可能访问端口，token 不能公开或写入日志。
跨设备应由部署方配置 HTTPS 和真实网络访问权限，避免明文传输；本协议不会自动提供 TLS。
registry 文件、管理端及服务进程属于可信本机边界；独立 token 只缩小外部 Hook 权限，
不隔离能直接读取本机状态文件的主体。此阶段不部署、不操作私人手机。
