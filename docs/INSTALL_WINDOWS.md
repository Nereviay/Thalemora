# Windows 安装洄海：第一次使用指南

**适用**：Windows 10/11 用户。**验证程度**：命令依据 v0.5.0 候选的 Python/Compose 安装合同编写，Linux/Docker 已验；**还没有在 Windows 原生系统完成一轮从零安装到浏览器操作的实测**。如果报错，请按原样记录文字供后续修订，不要把本页当作已全版本验证的“一键安装”。

## 路线 A：用 Python 安装（不必先学 Docker）

你需要安装一次 [Python 3.12（Python 官网）](https://www.python.org/downloads/windows/)，以及下载洄海源码。第一次安装时需要联网，后续本地原话检索不需要模型 API。

1. **确认 GitHub 已发布 v0.5.0 后**，打开 [Thalemora v0.5.0 源码页面](https://github.com/yuyu1838309000-cmd/Thalemora/tree/v0.5.0)，点击 **Code → Download ZIP**，把压缩包解压到一个你记得住的文件夹，例如桌面。不要直接在压缩包里运行。
2. 在这个解压文件夹中，点击文件管理器的地址栏，输入 `powershell` 并按回车。此时出现的蓝色或黑色窗口叫“终端”，后面只需要复制命令。
3. 先检查 Python 安装好了没有：

   ```powershell
   py -3.12 --version
   ```

   应该能看到 `Python 3.12.x`。如果提示“找不到 py”，通常是 Python/启动器没有正常安装，不要继续执行后面的步骤。

4. 依次复制下面两行，每一行运行结束再复制下一行：

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -c requirements.lock .
   ```

   第一行在当前文件夹创建洄海独立的 Python 环境，第二行联网安装必要组件。**第一次可能需要等待下载完成。**不需要运行 PowerShell 的激活脚本，也不需要更改系统脚本执行策略。

5. 生成一次管理密钥（相当于整个记忆库的管理员密码），记得保存到自己的密码管理器：

   ```powershell
   $env:THALEMORA_TOKEN = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
   $env:THALEMORA_TOKEN
   ```

   第二行会显示密钥，复制保存，**不要发到聊天或截图里**。下次启动时要用同一个密钥重新设置环境变量，否则之前在浏览器或 MCP 中保存的认证会失效；但更换密钥本身不会删掉本地数据。

6. 启动服务：

   ```powershell
   .\.venv\Scripts\thalemora.exe --data .\state serve
   ```

   保持终端窗口开着，浏览器打开 **http://127.0.0.1:8765**。填入上一步的密钥；成功进入后按 [第一次使用教程](GETTING_STARTED.md#2-安装后先做这四件事) 注册 Agent 和导入练习数据。

**以后怎么开？** 重新打开此文件夹的 PowerShell，先执行 `$env:THALEMORA_TOKEN = "自己保存的原密钥"`（不要把密钥发给别人），再执行第 6 步；**`state` 文件夹不要删除**。

**如何结束？** 回到运行服务的 PowerShell，按 `Ctrl+C`，等待它正常退出。关闭终端不等于备份。

## 路线 B：电脑已经有 Docker Desktop

Docker 可以把 Python 依赖放进隔离容器，但**如果还没有 Docker，先安装 Docker Desktop/WSL2 也可能很费时**。不要仅为了“省步骤”盲目选择它。[Docker Desktop Windows 官方安装文档](https://docs.docker.com/desktop/setup/install/windows-install/)

1. 打开并启动 Docker Desktop，确认显示 Docker Engine 正在运行。
2. 下载并解压同一个 v0.5.0 ZIP，进入解压目录打开 PowerShell。
3. 检查 Docker：

   ```powershell
   docker compose version
   ```

4. 在 PowerShell 中生成并保存管理密钥（可直接复用路线 A 第 5 步），执行：

   ```powershell
   $env:THALEMORA_TOKEN = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
   $env:THALEMORA_TOKEN
   docker compose -f deploy/compose.yaml up --build -d
   ```

5. 浏览器打开 **http://127.0.0.1:8765**，用同一个密钥登录。容器数据保存在 Docker 命名卷中，不是当前文件夹的 `state`。

以后启动：先启动 Docker Desktop，并在当前终端设置原密钥，再执行 `docker compose -f deploy/compose.yaml up -d`；停止：`docker compose -f deploy/compose.yaml stop`。**不要随意执行 `down -v`、删除 Docker 卷或重置 Docker Desktop**，否则可能删除记忆数据。修改运行方式前先按 [备份说明](P4_USAGE.md#全量历史导出与恢复) 备份。

## 你可能遇到的四类问题

| 情况 | 如何判断 |
| --- | --- |
| `py` / `python` 找不到 | Python 安装或 PATH/Launcher 有问题；重开 PowerShell 再试 |
| `pip install` 失败 | 保存完整错误；可能是网络、包下载、Python 版本或依赖兼容性，不要直接改项目源码 |
| Docker Engine 无法启动 | 先按 Docker 官方说明检查 WSL2/虚拟化与当前 Windows 版本；这不是洄海服务已经启动 |
| 网页显示 401 / 无法访问 | 核对服务终端是否仍在运行、端口 8765 是否冲突、密钥是否与本次启动相同 |

这两条 Windows 路线均尚未完成真机全流程验收。我们已把 **Python 安装、首次依赖下载、管理密钥保存、Docker 前置安装和对终端的依赖**列进 [安装简化待办](INSTALL_FRICTION.md)，后续可据真实失败做安装向导或自动脚本，而不是要求每个用户都去学命令行。
