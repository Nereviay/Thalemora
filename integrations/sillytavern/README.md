# SillyTavern 聊天前召回实验适配

**2026-10-16 已验收，尚未公开发布。** 所测为 Android 17 真机浏览器、官方 SillyTavern 1.19.0 / `06bde93`，服务运行在服务器。正常输入和发送自动触发 manifest 注册的 Hook，再调用真实洄海受限 `POST /api/recall`，记忆进入假模型实际 Context。

相关召回 HTTP 200 / 1271ms（Hook 1272ms）；无关、已见不重复注入，更正后采用新修订，401 和人为网络暂停导致的约 2 秒超时仍能继续回复，共六场景通过。它不是纯手机本地首装、长期后台或真实模型效果验收。旧 Android `memory=False` 的原因未定，不因后来通过而猜测根因。

## 安装与配置（开发者实验）

将本目录的 `manifest.json` 和 `index.js` 放入获授权酒馆环境的 `data/<user-handle>/extensions/thalemora-recall/`，刷新并确认扩展启用。当前不是独立扩展仓库，不能把 Thalemora 仓库根地址直接作为扩展 URL 安装。

洄海需要包含 P8 接口的开发版，公开 v0.6.0 不含此接口。在酒馆页面开发者控制台填写：

```js
Object.assign(globalThis.thalemoraRecallConfig, {
    serviceUrl: 'https://recall.example.invalid',
    recallToken: '这里替换成自己的受限召回 token'
});
```

URL 是服务根地址，插件追加 `/api/recall`；不能填写管理 token。配置只保留在当前页面内存，刷新需重填，尚无设置面板或持久化凭据。两项设为空可停止召回，下一轮 Hook 会先清掉旧槽位。测试转发与端点已撤除，示例地址是占位值。

浏览器跨源调用时，在洄海服务设置 `THALEMORA_RECALL_CORS_ORIGIN` 为酒馆的精确可信页面 Origin（含协议与端口），默认不启用。允许规则应用于同一个 HTTP App，管理 API 仍需管理员 Bearer；插件不得持有它。跨设备需可信连接并注意 HTTPS/混合内容限制；手机 localhost 指手机自己。

签发、撤销与错误合同见 [召回协议](../../docs/P8_RECALL_CONNECTOR.md)，客户端范围见 [用户指南](../../docs/CHOOSE_YOUR_SETUP.md)。

## 当轮行为

每轮先清空自己的固定注入槽位，再提交本次可见正文，由洄海判断相关性和已见内容；不缓存旧 revision，不把已清除的前轮注入当作本轮已见。仅追加服务返回的 `rendered`，不写原始聊天或同步新消息，也不代理模型。

异步 Hook 使用宿主 `setExtensionPrompt('thalemora-recall', rendered, 1, 0, false, 0)`，深度 0、SYSTEM、不参与 World Info 扫描。HTTP/网络/CORS/响应错误或约 2 秒等待超时会记录固定警告，本轮跳过记忆并继续生成；不自动重试。空结果保持槽位空白。超过 200 个 Unicode 字符的消息明确警告并跳过召回，不截断用户输入。

最终 Context 仍受宿主预算、预设和其他扩展影响；本适配不推断后续裁剪结果。

## 官方契约与最低测试

核对官方 [Prompt Interceptors](https://docs.sillytavern.app/for-contributors/writing-extensions/#prompt-interceptors) 及固定提交 `06bde939fb1e9c4c8d8641d810f0a916b5bce127`：

- `public/scripts/extensions.js` 的 `runGenerationInterceptors` 等待 manifest 注册的全局异步函数。
- `public/script.js` 的真实非 dry-run 生成路径等待拦截器后组装 Context；预览不能替代实际请求验收。
- `setExtensionPrompt` 参数为 `(key, value, position, depth, scan, role, filter)`；`IN_CHAT=1`、`SYSTEM=0`。内存注入不是聊天持久化写入。

从源码根运行 `node --test integrations/sillytavern/index.test.mjs` 可验证假 fetch/假宿主的既有专项，不替代上述已完成真机证据。此候选仅整理与打包，没有改动已经验收的插件业务代码。
