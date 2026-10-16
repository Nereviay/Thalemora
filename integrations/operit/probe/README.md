# Operit 发送前接口诊断

此目录供开发者排查 Operit 插件接口使用。普通用户请安装[自动召回插件](../adapter/README.md)。

探针参考 Operit 1.12.2 的 `examples/message_insert`，注册 `before_send_to_model`，向 `processedInput` 添加固定诊断标记，不修改聊天历史。

在独立的示例聊天与本地请求接收端使用，比较实际模型请求和聊天界面，检查标记是否只出现在本轮请求中。诊断结束后停用并移除探针，避免与正式插件同时运行。不要向真实付费模型发送诊断消息。
