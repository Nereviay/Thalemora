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

可选重排不可用时，如果基础召回仍有结果，会返回结果并在 `errors` 中注明 `rerank_unavailable`。

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
