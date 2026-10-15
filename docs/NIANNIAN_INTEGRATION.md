# 念念接入：宿主适配与历史同步

本项目已有念念生产接入案例：沿用公共核心、保留原有身份与宿主权威，完成隔离预演、有限真实对照和经批准的正式切换。下述工具不自动给予其他部署切换许可，独立使用不需要执行这些迁移工具。

## 路线

在念念现有记忆网关内使用本包 Python 核心，保留 Runtime/RawEvent/Prompt 权威。记忆维护在后台运行，正常回复不串行等待 Writer/Gatekeeper/Reflection。

现有 Memory/Water 与 Self Model 先直接使用明确提供的旧 DB 路径/连接，不创建新 Agent 身份，不重编号，不将内在领域旧表丢弃。Memory/Water 的 schema 初始化器用于空库，不能在已存在 Water 表的旧库重复运行。此预演只调用现有核心读接口，不创建另一套 schema 或身份。

## 离线工具

从源码目录执行，所有输出只含结构、计数和验收结果，不输出正文：

```bash
PYTHONPATH=src python3 tools/niannian_storage_rehearsal.py snapshot SOURCE_DIRECTORY NEW_SNAPSHOT_DIRECTORY
PYTHONPATH=src python3 tools/niannian_storage_rehearsal.py rehearse NEW_SNAPSHOT_DIRECTORY NEW_WORK_DIRECTORY
```

使用前按宿主实际授权确认来源目录。9 个文件由工具的 DATABASES 明确列出；私有通道文件、Prompt、密钥不在清单中。真实资料保留在同服务器私有目录，不放进 Git、示例或公开测试。

- SQLite backup 从 mode=ro 连接读取来源，包含已提交 WAL；目标必须全新，绝不覆盖旧快照。
- 每个 DB 单独记录完成时间；在线多库副本不是同一事务时点，正式切换仍需冻结/最终追赶。
- 副本 quick_check 通过后直接读取 Memory 人工只读投影、Water 正式归属、Self Model 声明和全部版本；核实既有 principal/namespace。
- 工作副本与快照逐表逐行比较，含 BLOB、旧内在领域、待处理工作及 SQLite/FTS 表；同条数但内容改变仍会失败。
- 失败保留局部副本与异常，不产出 PASS；修正后使用新的目标目录。
- 预演不初始化新身份、不调用模型、不启动服务；不修改来源或建立正式写入权。

## 隔离宿主接线与增量

网关在原接口内调用公共 Memory/Water/Gatekeeper/Scene/检索/旧内在领域；VNext、自然浮现和主动策略留在宿主。Self Model 从 Runtime canonical generation/tool-call/epoch 提供可信证据，并使用公共核心；不迁移 Prompt 或另建 Runtime。

- `adapters/niannian_history.install(connection)`：显式安装 canonical 事务内 feed。
- `adapters/niannian_raw_feed.install_raw(connection)`：显式安装 RawEvent feed。
- 两个安装动作会修改源 schema，只能在隔离副本或生产 Gate 明确授权后执行。安装时各自事务回填当前快照，不伪造历史修订。
- tools/niannian_history_sync.py：后台单次有界消费，复用持久游标，不安装 feed、不调用模型；生产案例由既有宿主按分钟调度，独立包不自行注册系统任务。
- 事件覆盖新增/编辑/替换/软删除与恢复/物理清除；main 转 private 会撤销普通档案；canonical 和 RawEvent 来源投影并列留存，不能相加当唯一聊天数。
- 旧 chenlin_mcp / chenlin_explicit 与 Water proposer 保留，人工卡只读参与关系判断；不批量重写旧来源。

## 已验案例与持续边界

已完成案例的隔离存储/宿主接线、冻结/增量追赶、唯一写入权、恢复与有界真实模型对照。完整长上下文缓存、所有场景时延/成本与长期质量仍依赖自然使用观察，不冒称全面不退步。

原始档案副本和宿主 delta 仅留本服务器私有目录，不进入公共包。6 项依赖完整 heartbeat 的未修改 exposure 测试未计 PASS；其他宿主接入前按精确部署影响决定验收范围。旧未知 evidence/hash 保留未知，生产切换仍需独立批准。
