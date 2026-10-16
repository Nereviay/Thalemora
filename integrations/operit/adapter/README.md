# 洄海 · Operit 自动召回插件 0.2.0

发送消息前查找洄海记忆，并附加到当轮模型请求。聊天界面保留用户原话。为 Operit 1.12.2 的发送前插件接口和工具箱设置页面做了适配。

## 安装

1. 准备洄海 v0.7.0 服务，并按[插件教程](https://github.com/yuyu1838309000-cmd/Thalemora/blob/main/docs/PLUGIN_SETUP.md)签发专用召回凭据。
2. 从 [Releases](https://github.com/yuyu1838309000-cmd/Thalemora/releases/latest) 下载 `thalemora-operit-0.2.0.toolpkg`，保存到手机。
3. 在 Operit 包管理页面选择「插件」分类，用导入文件功能选择该文件。不要选择只接收 js、ts、hjson 的脚本包分类。
4. 启用插件，在工具箱打开「洄海 · 自动召回」。
5. 填服务根地址和召回凭据，打开「启用聊天前召回」，点击「保存设置」。

插件 ID 为 `org.thalemora.recall`。如装过实验包 `org.thalemora.operit_probe`，请先停用或移除，避免重复召回。

## 设置

- 地址示例：`https://你的洄海地址`，不带 `/api/recall`。手机上的 localhost 指向手机自身。
- 只填专用召回凭据，不填管理 token 或模型 Key。
- 设置保存在 Operit 本机 App 私有存储中，重开后保留；插件文件不包含用户设置。
- 密码框只遮挡显示，配置的访问权限由 Operit App 管理。
- 不需要 Root、Shizuku、无障碍等额外系统权限。

## 使用与停用

正常发送消息即可，模型不必主动调用工具。插件使用 Operit 的发送前处理接口，只改本轮请求，不把召回文字写入聊天历史。

超过 200 个 Unicode 字符的消息跳过本轮召回。连接和读取分别设置了 2 秒超时；整轮发送时间仍由 Operit 控制。无结果、服务出错或超时后继续原消息。

关闭开关并保存即可停用。清空地址、凭据并保存可删除本地配置；不再使用时，还应在洄海撤销服务端凭据。

插件只读取记忆，不自动同步新聊天。模型与 Key 继续在 Operit 中配置。

## 开发

在 Thalemora 源码根运行：

```bash
node --test integrations/operit/adapter/settings.test.mjs
python3 tools/package_plugins.py ./dist-plugins
```

插件采用 Apache-2.0 许可。
