# 洄海 · SillyTavern 自动召回插件 0.2.0

发送消息前，从洄海查找相关记忆并加入本轮模型上下文。为 SillyTavern 1.19.0 的扩展设置和提示词插入接口做了适配。

## 安装

1. 准备洄海 v0.7.0 服务，并按[插件教程](https://github.com/yuyu1838309000-cmd/Thalemora/blob/main/docs/PLUGIN_SETUP.md)签发专用召回凭据。
2. 从 [Releases](https://github.com/yuyu1838309000-cmd/Thalemora/releases/latest) 下载 `thalemora-sillytavern-0.2.0.zip`。
3. 解压到酒馆的 `data/<user-handle>/extensions/thalemora-recall/`。`manifest.json` 必须直接位于这个目录。
4. 刷新酒馆，进入扩展设置，展开「洄海 · 自动召回」。
5. 填服务根地址和召回凭据，勾选「启用聊天前召回」，点击「保存设置」。

请使用 ZIP 文件安装。项目根地址不能填入酒馆的扩展 Git URL 安装框。

## 设置

- 地址示例：`https://你的洄海地址`，不带 `/api/recall`。
- 凭据使用洄海签发的专用召回凭据，不填管理 token 或模型 Key。
- 地址和开关保存在酒馆设置中；凭据保存在当前浏览器标签页，刷新保留，关闭标签页后需重填。
- 跨源访问时，在洄海设置 `THALEMORA_RECALL_CORS_ORIGIN` 为酒馆页面的精确 Origin，包括协议、主机和端口。HTTPS 页面需要连接可访问的 HTTPS 服务。
- 密码框只遮挡显示。凭据仍属于当前浏览器页面的访问范围。

## 使用与停用

正常发送消息即可，不需要模型主动调用工具。设置区会显示本轮召回状态。插件每轮替换自己的系统提示词槽位，最终上下文长度仍由酒馆控制。

超过 200 个 Unicode 字符的消息跳过本轮召回；约 2 秒仍未收到结果时继续原聊天。无结果或服务出错也会继续发送。

取消勾选并保存即可停用。清空凭据再保存可忘记当前凭据；不再使用时，也应在洄海撤销它。

插件保留原聊天内容，不自动保存新消息到洄海。聊天模型与 Key 仍在酒馆中配置。

## 开发

在 Thalemora 源码根运行：

```bash
node --test integrations/sillytavern/index.test.mjs
python3 tools/package_plugins.py ./dist-plugins
```

插件采用 Apache-2.0 许可。
