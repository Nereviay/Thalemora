# SillyTavern 聊天前召回试验

这是 Thalemora P8-3 `/api/recall` 的一个薄客户端适配。每次真实生成前读取 Hook 的最新用户消息，异步召回，将服务返回的 `rendered` 注入本次模型 Context。记忆核心和模型发送权仍分别属于 Thalemora 与宿主。

**状态：服务器隔离的真实 SillyTavern 页面已完成一次发送前 Hook → 模拟召回 → 模拟模型最终请求闭环；手机本地、真实洄海受限令牌的浏览器端到端仍未通过，P8-4 保持 PARTIAL/HOLD。** 无付费模型、真实聊天或聊天后写入。

## 安装与最少配置

在获授权的 SillyTavern 测试环境中，把本目录的 `manifest.json` 和 `index.js` 放入 `data/<user-handle>/extensions/thalemora-recall/`，刷新页面并确认扩展已启用。当前仓库不是独立扩展仓库，不能直接把项目根目录作为扩展 URL 安装。本次交付没有执行宿主安装或部署。

在该页面的浏览器开发者控制台设置两个参数（下面只有合成占位值）：

```js
Object.assign(globalThis.thalemoraRecallConfig, {
    serviceUrl: 'https://recall.example.invalid',
    recallToken: 'synthetic-recall-token'
});
```

`serviceUrl` 是服务根 URL，适配追加 `/api/recall`。token 必须是可信管理端事先签发、绑定正确 Agent/app/channel 的**专用召回 token**，不能填写管理员 token。适配不签发 token，也不调用管理 API。配置只保留在当前页面内存，刷新需重新填写；没有设置面板或持久化凭据。将两项设为空可停止召回，下次 Hook 会清掉旧注入。

发送消息时，宿主通过 manifest 注册的全局异步函数执行召回。HTTP 失败、网络/CORS 错误、响应不合法或 2 秒超时均记录不含正文/凭据的固定警告，跳过记忆并继续宿主生成；不重试、不调用 `abort`。空响应清空上一轮内容。P8-3 最多接受 200 个 Unicode 字符，超长消息会明确警告并跳过召回，不截断问题，聊天照常发送。

## 上游契约核对

核对依据为[官方扩展文档](https://docs.sillytavern.app/for-contributors/writing-extensions/#prompt-interceptors)及官方 `release` 分支在本次核对时的提交 `06bde939fb1e9c4c8d8641d810f0a916b5bce127`：

- [extensions.js](https://github.com/SillyTavern/SillyTavern/blob/06bde939fb1e9c4c8d8641d810f0a916b5bce127/public/scripts/extensions.js#L2024)：`runGenerationInterceptors` 按 manifest 顺序 `await globalThis[name](chat, contextSize, abort, type)`。
- [script.js 生成链](https://github.com/SillyTavern/SillyTavern/blob/06bde939fb1e9c4c8d8641d810f0a916b5bce127/public/script.js#L4561)：非 dry run 会等待拦截器，然后组装实际提示词。预览 dry run 不调用 Hook，不能用预览判定一次真实召回结果。
- [st-context.js](https://github.com/SillyTavern/SillyTavern/blob/06bde939fb1e9c4c8d8641d810f0a916b5bce127/public/scripts/st-context.js#L152)：Context 暴露 `setExtensionPrompt` 与内存中的 `extensionPrompts`。
- [script.js API 实现](https://github.com/SillyTavern/SillyTavern/blob/06bde939fb1e9c4c8d8641d810f0a916b5bce127/public/script.js#L8926)：准确签名为 `setExtensionPrompt(key, value, position, depth, scan = false, role = SYSTEM, filter = null)`。注意函数旁 JSDoc 的 role/scan 描述顺序不能当作实参顺序。函数只赋值到内存 `extension_prompts[key]`，没有写聊天、metadata 或保存调用。
- [枚举](https://github.com/SillyTavern/SillyTavern/blob/06bde939fb1e9c4c8d8641d810f0a916b5bce127/public/script.js#L484)：`IN_CHAT=1`、`SYSTEM=0`。本适配使用 `('thalemora-recall', rendered, 1, 0, false, 0)`，固定槽位替换，深度 0，系统角色，不参与 World Info 扫描。
- [openai.js 消息组装](https://github.com/SillyTavern/SillyTavern/blob/06bde939fb1e9c4c8d8641d810f0a916b5bce127/public/scripts/openai.js#L810)：`populationInjectionPrompts` 读取 `getExtensionPrompt(IN_CHAT, depth, ..., role, ...)`，在反向消息数组插入并最终反转，深度 0 的注入进入末端系统消息；文本补全链也由 `script.js` 的 `doChatInject` 消费此槽位。最终仍受宿主上下文预算、预设和其他扩展影响，需要真实请求验收。

适配不修改 `chat` 数组或消息。每轮先清空自己的槽位，提交 Hook 收到的正文为 `visible_texts`，由现有服务判定相关性和已见内容；不把上一轮已清除注入的 ID 当成本轮已见，不缓存旧 revision。重复生成只替换同一槽位。Hook 的 chat 之后仍可能被宿主按预算裁剪；这里不声称能获知最终裁剪结果，也不推断其他扩展的私有已见引用。

## 真实网络障碍与未验收项

**本 P8-4 隔离分支**在 `app/http.py` 新增可选 `THALEMORA_RECALL_CORS_ORIGIN`（精确匹配可信网页 Origin，例如 `http://127.0.0.1:18766`）。默认不启用；指定时允许该 Origin 的 POST 与 Authorization/Content-Type 预检，本机实际 OPTIONS 返回 200。该允许规则应用于同一个 HTTP App 的所有路由，**管理 API 仍需单独的管理员 Bearer，外部插件不得持有它**。真实受限令牌浏览器请求因远程安全检查中断，还未通过；不能将预检 200 视作召回成功。跨设备还要解决 HTTPS 和网络可达。

### 本轮真实宿主试验（2026-10-16）

服务器临时克隆官方 SillyTavern release `06bde93`，只在 `127.0.0.1:18766` 启动；把本插件装入**隔离的** `default-user/extensions/thalemora-recall`，实际浏览器页面报告 Hook 是 `function`。通过 Playwright 在这个**真实宿主页面**选择合成角色、配置本地假模型并正常点击发送 `以前喜欢什么茶？`：本机合成召回服务记录此问题与虚构专用 token 被使用，合成模型收到 **7 条消息，`has_memory=True`**，其 Context 含 `[记忆 1] 喜欢乌龙茶`。这一证据证明第三方宿主在生成前等待插件并能把记忆交给模型，不是手动调用插件函数。合成端点彼此隔离，没有读取用户聊天、没有实际模型服务或云端花费。**尚未在 Android 真机运行该工作区或测本地耗时；假召回成功不等于真实洄海 API 浏览器端到端成功**。

手机中的 `127.0.0.1` 指向手机自身，不指向电脑或服务器。尚未确认手机服务运行、监听地址、局域网可达性、HTTPS/混合内容限制及浏览器访问许可；没有网络测量结果，不能声称已连通。没有安装 Operit/APK、修改其他客户端或接触真实数据库、API Key、付费模型。

最低后续验收仍需真实宿主页面 + 合成来源专用 token + 授权的无付费召回服务 + 本地假模型，观察最终请求中的记忆、无关清除、去重、失败/超时和长度/时延。以下测试不替代这一验收。

## 合成测试

从本 worktree 根目录运行一次：

```sh
node --test integrations/sillytavern/index.test.mjs
```

仅用 Node 内置 test/assert/vm、假 fetch 和假宿主；覆盖相关、无关、失败/模拟超时、去重与模拟上游 Context。测试按核实的固定槽位和深度 0 规则构造消息，没有运行真实 SillyTavern，也没有测手机性能、浏览器预检或真实上游请求。
