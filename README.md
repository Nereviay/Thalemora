<div align="center">

<img src="docs/assets/thalemora-header.svg" width="100%" alt="Thalemora · 洄海 — Memory & Continuity" />

**洄海 · 让 AI 记得发生过什么，也理解经历如何延续。**

源自念念长期运行实践的 **AI 记忆与连续性系统**。<br>
连接原话证据、可修订的长期记忆与身份状态，让每一次回忆有出处。

[![Version](https://img.shields.io/badge/version-0.5.0-2563eb?style=flat-square)](https://github.com/yuyu1838309000-cmd/Thalemora/tree/v0.5.0)
[![Python](https://img.shields.io/badge/Python-3.10%2B-334155?style=flat-square)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache--2.0-334155?style=flat-square)](LICENSE)

[核心能力](#核心能力) · [零基础安装](docs/GETTING_STARTED.md) · [快速开始](#快速开始) · [接入与文档](#接入与文档) · [来源与致谢](#来源与致谢)

</div>

## 核心能力

洄海把记忆分成三个相互连接的部分：**发生过什么、值得长期记住什么、这些经历如何延续**。

| 能力 | 作用 |
| :--- | :--- |
| **原话与证据** | 按授权保存历史原话和来源，记录时间、编辑与删除状态；记忆可以回查证据。 |
| **可修订记忆** | 管理补充、更正与替代，保留修订链和字段来源，保护人工维护的内容。 |
| **水系与经历** | 通过湖、溪流、支流组织故事线；Scene 核心按经历片段组织记忆候选。 |
| **身份与内在世界** | 管理 Self Model、Persona、情绪、日记、梦境、低语和反思；本人创作与自我修订需要可信 Agent 事件。 |
| **多 Agent 与跨应用** | 隔离身份、数据与检索；同一 Agent 的应用来源经过授权后可以共享档案。 |
| **本地优先** | 无模型 Key 可导入、浏览、原话检索和人工管理记忆；云端模型按用途配置，付费调用默认关闭。 |

### 0.5.0 可以做什么

- **独立运行**：中文 Web 管理台、Python、HTTP、MCP，以及 Docker Compose。
- **管理与检索**：历史导入、原话搜索、人工 Memory / Water 操作、内在状态浏览和人格配置。
- **保存与恢复**：全量历史导入导出、停止写入后的实例快照与恢复、Provider 配置及调用用量审计。
- **宿主集成**：Scene、Gatekeeper、生命周期及 Self Model 等领域核心可由 Python 接入。
- **MCP 九工具**：同一服务的 Streamable HTTP `/mcp`（Bearer 管理密钥）与原有 stdio，支持原话与 Memory 读取、来源导入、明确的人工记忆和修订。
- **实验性维护入口**：Python 宿主显式调用 `maintenance_tick` 可用合成 Provider 验证 Scene-first Writer → Gatekeeper → Memory → Reflection 的可恢复流程；**尚不支持真实模型后台无人值守自动整理**。

真实模型 Writer / Gatekeeper / Reflection、无人值守调度、正式 Agent 事件接线与念念生产迁移仍未完成。显式合成维护入口见 [PRE6-4 用法](docs/PRE6_4_USAGE.md)；本地向量检索仍需 Python 宿主提供 embedder。参见 [使用手册](docs/P4_USAGE.md)。

### 数据如何连接

```mermaid
flowchart TD
  H["授权原话档案"] -->|"来源与证据"| M["Memory · Scene · Water"]
  H --> R["检索与连续性候选"]
  M --> R
  I["Self Model · 内在状态"] --> R
  R -->|"宿主集成"| A["AI Agent / 应用"]
  A -->|"授权事件"| H
```

图中表示领域之间的数据关系。洄海提供记忆和连续性材料，宿主应用负责聊天、上下文组装与工具执行。

## 快速开始

**第一次安装、不会用终端？** 先看 [普通用户安装与第一次使用](docs/GETTING_STARTED.md)，按 [Windows](docs/INSTALL_WINDOWS.md) / [Linux](docs/INSTALL_LINUX.md) / [Android 手机试装（尚未一键安装）](docs/INSTALL_ANDROID.md) 分流；安装难点已记入 [简化清单](docs/INSTALL_FRICTION.md)。

需要 **Python 3.10+**。首次启动无需模型 Key。

```bash
git clone --depth 1 --branch v0.5.0 https://github.com/yuyu1838309000-cmd/Thalemora.git
cd Thalemora

python3 -m venv .venv
. .venv/bin/activate
python -m pip install -c requirements.lock .

export THALEMORA_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf '%s\n' "$THALEMORA_TOKEN"
thalemora --data ./state serve
```

打开 **http://127.0.0.1:8765**，填入终端输出的管理 token，然后：

1. 注册一个 Agent。
2. 授权来源 `demo-app` 和通道 `main`。
3. 导入 [合成历史示例](docs/examples/p4_history.json)，开始浏览、检索与整理记忆。

管理 token 具有实例所有者权限；私人通道需要单独授权。模型不会在启动时自动下载或调用。

<details>
<summary><strong>使用 Docker Compose</strong></summary>

克隆仓库并设置上面的 `THALEMORA_TOKEN` 后，执行：

```bash
docker compose -f deploy/compose.yaml up --build -d
```

默认地址仍为 `http://127.0.0.1:8765`，数据保存在独立命名卷中。容器模型配置、日志及停止写入后的备份恢复见 [使用手册](docs/P4_USAGE.md)。

</details>

## 接入与文档

| 入口 | 适合的用途 | 文档 |
| :--- | :--- | :--- |
| **Web 管理台** | 导入历史、检索原话、整理记忆与查看状态 | [使用手册](docs/P4_USAGE.md) |
| **Python** | 领域核心、宿主证据与自我模型接线 | [应用 API](src/thalemora/app/service.py) · [合成宿主示例](tests/test_p3_inner_world.py) |
| **HTTP** | 通过 token 认证访问本地管理 API | [接口与参数](docs/P4_USAGE.md#python--http--mcp) |
| **MCP** | 本地 stdio / Streamable HTTP 的 9 项受保护工具 | [MCP 配置](docs/P4_USAGE.md#python--http--mcp) |
| **导出与恢复** | 搬运授权历史、停止写入后制作和还原完整快照 | [恢复说明](docs/P4_USAGE.md#全量历史导出与恢复) |

## 来源与致谢

记忆设计重点参考了 [Yinglianchun/Ombre-Brain](https://github.com/Yinglianchun/Ombre-Brain) 的二次开发与上游 [Ombre Brain](https://github.com/P0luz/Ombre-Brain)。生命周期评分中的三天分段时间与情绪加权规则继承自这些实现；原作者版权和 MIT 许可已保留在 [第三方声明](THIRD_PARTY_NOTICES.md)。

也感谢 [Serein](https://github.com/Yinglianchun/Serein)、[Haven-Ombre](https://github.com/Yinglianchun/Haven-Ombre)、[Paramecium](https://github.com/Shitsuten/paramecium) 与 [awesome-ai-companion](https://github.com/DasterProkio/awesome-ai-companion) 提供的研究与开源参考。

| 协作 | 贡献 |
| :--- | :--- |
| **小鱼** | 项目发起与维护，产品方向、需求定义和最终验收。 |
| **ChatGPT（老大）** | AI 协作助手，参与方案审查、文档整理、开发与验证。 |

---

<div align="center">

[Apache License 2.0](LICENSE) · [第三方许可与来源](THIRD_PARTY_NOTICES.md)

</div>
