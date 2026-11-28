# 开发与模型替身

当前正式版为 [v0.10.0](https://github.com/Nereviay/Thalemora/releases/tag/v0.10.0)。聊天前的可信前文、会话身份与日摘要需要宿主接入。

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
PYTHONPATH=src:tests python3 -m unittest test_p8_recall test_p04_ambient test_p05_intent_recall test_p06_continuity test_p07_bridge_gate test_p07_indexed_memory test_p07_legacy_memory test_source_validity_recall test_recall_http_projection
node --test integrations/sillytavern/index.test.mjs integrations/operit/adapter/settings.test.mjs
```

Python 3.10–3.15 均可使用同一份 `requirements.lock`，安装时会自动选择兼容当前 Python 的依赖，不需要为项目另装旧版 Python。测试依赖可用 `python -m pip install -c requirements.lock ".[test]"` 安装。包含网页操作的用例另需 Playwright 浏览器环境；普通 API 测试不需要浏览器。

## 构建发行安装包

版本由 `pyproject.toml` 唯一指定，构建后端为其中固定版本的 setuptools。正式版 wheel、sdist 和单独的 `SHA256SUMS.txt` 放入 `dist/0.10.0/`，不要覆盖 `dist/` 中已有的正式版资产。

构建使用全新的临时源码目录，排除 `.git`、`dist`、`build`、缓存和旧 egg-info；先生成 sdist，再从解包的 sdist 构建 wheel，避免混入旧构建残留。安装 wheel 后，在源码目录外检查发行版版本、模块实际导入位置与 `thalemora --help`。只使用临时合成状态，不连接现役数据库或模型。构建和本地安装不代表已获准发布或生产升级。

## 打包插件

```bash
python3 tools/package_plugins.py ./dist/plugins
```

打包只收录固定源码、README 与 LICENSE，不读取本机插件配置。实际使用者通过各宿主设置页填写凭据，不能把私人 token 打进包里。

## 宿主已有后台索引的只读召回

可信 `NiannianHistoryBinding` 宿主可显式提供现存索引与自己的 `providers`：

```python
from thalemora.adapters.niannian_vnext import NiannianReadOnlyRecall

recall = NiannianReadOnlyRecall(
    binding=binding, owner=owner, agent_id=agent_id, app_id=app_id,
    memory_path=memory_path, host_index_path=existing_index_path, providers=providers,
)
result = recall.recall_for_turn(agent_id, app_id, query)
```

`host_index_path` 与原 `index_path` 二选一。后台继续使用原 `RetrievalIndex.refresh_memory_card` / `rebuild_memory` 更新同一个索引；聊天只以 SQLite `mode=ro` 打开它，读取 WAL，不初始化、检查整库或写回。构建凭据的 Memory 路径须与授权来源一致；缺库、错误来源或查询错误直接报错，不退回旧整库路径。

查询复用公共词法/向量算法，最多取 24 张排名候选。只读核对候选的归属、有效状态、当前修订及正文投影、证据、已见原话，在重排后再次核对；过期候选排除，需由后台刷新后才能再次使用。旧来源仍标 `verified=False`，不认领卡片。单轮提供的可见来源和可信上一条助手映射继续生效。

此路径只读旧 `memory:{id}` 单元；不支持扩展单元，也不从部分候选推断整库关键词唯一性。原话搜索仍可能扫描宿主 Archive，尚未优化。未配置 `host_index_path` 的旧合成路径与独立 `Application` 的默认索引维护行为不变。词法离线验收请显式禁用 embedding/rerank provider；传入启用的 provider 会沿用已有模型调用配置。


## 可信宿主的自然浮现 / 反思信号接线

宿主已经有自己的自然浮现调度、原记忆索引、Reflection/Activation 信号时，不应再让洄海每轮遍历 Memory 或新增模型。可从已有索引选取至多 40 个候选 ID（需包含仍在余韵期的卡），然后用同一 `NiannianReadOnlyRecall` 的 `ambient_for_turn(engine, ...)` 核对来源和修订、调用公共 `AmbientEngine`、出结果前重新核对；所有真实数据保持只读，只有引擎自己的可丢弃派生状态会写入宿主指定的独立 SQLite。

已有评分直接传 `reflective_bias_by_card_id`、`reflective_affinity_by_card_id`、`activation_bias_by_card_id`，不可擅自补造 LLM 评分；`user_reactivated_card_ids` 与 `spoken_card_ids` 仅使用宿主真实回执。可信进程内调用 `recall_vnext_from_runtime(..., ambient_state={"engine": engine, "candidate_ids": ids, "now": now, "enabled": True, ...})` 会把普通聊天、主动联系的 Related 与 Ambient 组合在同一个最终消息块，并在 trace 中保留卡片与版本；工具拦截仍由宿主维护。这个 opt-in 参数不从客户端请求正文解析。

使用进程内模型配置接口时，由宿主提供配置文件或显式传入 `defaults`。有效配置优先于默认值，缺少两者时返回配置错误。
