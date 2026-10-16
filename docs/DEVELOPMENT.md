# 开发与模型替身

开发接入时，可以使用临时数据和模型替身，不需要付费 API。正常使用后台整理请看[模型与后台整理](AUTOMATIC_MEMORY.md)。

## 显式运行一轮维护

`Application.maintenance_tick(agent_id, app_id, channel="main", limit=4, provider=None)` 读取授权历史，处理经历、候选和反思。没有提供模型时保留待处理状态，不会偷偷调用付费服务。

模型替身需要 `synthetic = True`，并提供 `call(module, payload)`，返回 `{"output": ..., "usage": ...}`：

| 用途 | 返回格式 |
| --- | --- |
| writer | `{"candidates":[...]}`；候选含内容、来源 ID、整数版本和逐字引用。 |
| gatekeeper | 记忆关系决策；可新增、补充、更正、替代或拒绝。 |
| reflection | `{"text":"整理建议"}`。 |

完整可运行示例见[维护测试](../tests/test_pre6_maintenance.py)。

候选每轮最多 8 项；空列表表示不需要新增记忆。引用必须对应当前授权来源，删除或修改过的来源会影响待处理候选；人工编辑不由自动整理覆盖。

## 运行相关检查

```bash
PYTHONPATH=src:tests python3 -m unittest test_p8_recall test_p8_connector
node --test integrations/sillytavern/index.test.mjs integrations/operit/adapter/settings.test.mjs
```

Python 测试依赖可用 `python -m pip install -c requirements.lock ".[test]"` 安装。包含网页操作的用例另需 Playwright 浏览器环境；普通 API 测试不需要浏览器。

## 打包插件

```bash
python3 tools/package_plugins.py ./dist/plugins
```

打包只收录固定源码、README 与 LICENSE，不读取本机插件配置。实际使用者通过各宿主设置页填写凭据，不能把私人 token 打进包里。
