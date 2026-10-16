# 自动召回本地候选｜0.7.0.dev1

这是 P8 完成后的本地整合候选，尚未公开发布；公开可安装版仍为 v0.6.0。念念不因候选生成而升级。

## 包含什么

- 一套独立 Related 召回；Embedding、Rerank 分别选择本地、API 或关闭，沿用授权、派生索引和模型预算。
- 来源受限召回凭据与 `POST /api/recall`，不代理聊天模型、不把 Writer/Gatekeeper 放入发送前热路径。
- SillyTavern 的最薄发送前适配及 Operit 的追加实验源码。
- 按设备、能力和所测版本选择的中文指南。

Android 浏览器中的 SillyTavern 1.19.0 已通过六场景真实发送；Operit 官方 v1.12.2 已通过最小真实召回注入实验。两者均使用服务器洄海、合成数据和假模型观察实际请求；不是纯手机首装或真实模型质量验收。

## 产物与用途

| 产物 | 用途与限制 |
| --- | --- |
| Python wheel | 安装洄海核心、HTTP 服务与管理台；不自动向聊天软件安装插件 |
| 源码 sdist | 包含指南及 `integrations/`，可查看和自行接线 |
| SillyTavern 实验 ZIP | 仅扩展文件，需要开发者安装并在页面内存配置；不是独立扩展仓库 URL |
| Operit 实验 `.toolpkg` | `adapter/` 的固定版本，配置默认空白；开发者需在自己的私有副本填写 URL 与受限 token 后重新打包，尚无图形配置页 |

Operit 包沿用所测实验 ID `org.thalemora.operit_probe`，不是新的生产插件身份。`probe/` 固定标记诊断包仅保留源码，不另发安装包。分发产物不含测试数据库、真实地址、token 或 APK。

## 开始前阅读

- [我该怎么用洄海](CHOOSE_YOUR_SETUP.md)：四个状态与设备/客户端选型。
- [召回协议](P8_RECALL_CONNECTOR.md)：凭据签发/撤销、请求与网络条件。
- [SillyTavern 实验说明](../integrations/sillytavern/README.md)。
- [Operit 实验说明](../integrations/operit/adapter/README.md)。

当前没有普通用户一键插件配置、通用 API 代理、任意 App 聊天自动同步、完整手机独立安装/恢复或长时后台验收。这些不因候选打包而变为已交付。
