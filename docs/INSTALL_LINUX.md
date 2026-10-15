# Linux 安装洄海：电脑或自己的服务器

**适用**：Ubuntu/Debian 或其他已经安装 Python 3.10+ 的 Linux。以下首次 Python/Compose 路线来自已完成的独立 Linux 验收；不同发行版的包名/软件安装方式可能不同，没有逐一验证。**不需要模型 API Key**。

## 路线 A：已有 Python 3.10+（少一个 Docker 依赖）

1. 打开终端。检查有 Python 3.10+ 和 `git`：

   ```bash
   python3 --version
   git --version
   ```

   若提示“command not found”，先通过你使用的 Linux 发行版安装相应软件。Ubuntu/Debian 常见命令：`sudo apt update && sudo apt install -y python3 python3-venv python3-pip git`。

2. 在 GitHub Releases **确认已发布**后下载 v0.5.0 源码，并进入项目目录：

   ```bash
   git clone --depth 1 --branch v0.5.0 https://github.com/yuyu1838309000-cmd/Thalemora.git
   cd Thalemora
   ```

3. 逐行执行：

   ```bash
   python3 -m venv .venv
   ./.venv/bin/python -m pip install -c requirements.lock .
   export THALEMORA_TOKEN="$(./.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
   printf '%s\n' "$THALEMORA_TOKEN"
   ./.venv/bin/thalemora --data ./state serve
   ```

   第三行生成一个随机的**管理密钥**，第四行显示它。私下保存这个密钥；不发到聊天里。终端保持运行。

4. 在**同一台电脑**浏览器打开 **http://127.0.0.1:8765**，填入管理密钥，按 [第一次使用](GETTING_STARTED.md#2-安装后先做这四件事) 练习。

服务只监听当前电脑的 `127.0.0.1`。**如果安装在远程 VPS 上，不能直接在自己家电脑打开家里的 `127.0.0.1:8765` 就访问 VPS**。外部访问需要另行配置有身份保护的访问通道；不要直接把整个管理员接口公开到互联网。

**关闭**：回到终端按 `Ctrl+C`。**再次启动**：在 `Thalemora` 目录重新提供原管理密钥环境变量，执行第 3 步的最后一行，保持使用原 `state` 目录。切勿每次重新 `git clone` 一个实例来找旧数据。

## 路线 B：机器已经有 Docker Compose

适合熟悉容器的用户。如果还没有 Docker，首次安装 Docker 本身是一道技术门槛。

1. 和路线 A 一样，用 `git clone` 下载 v0.5.0。
2. 确认 `docker compose version` 有输出。
3. 在项目目录执行：

   ```bash
   export THALEMORA_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
   printf '%s\n' "$THALEMORA_TOKEN"
   docker compose -f deploy/compose.yaml up --build -d
   docker compose -f deploy/compose.yaml logs --tail 30
   ```

4. 打开同一台机器的 **http://127.0.0.1:8765**，输入终端显示的密钥。要停止服务，可执行 `docker compose -f deploy/compose.yaml stop`。

Docker 这条路线使用独立命名卷保存状态，不是 Python 路线里的 `./state`。**两种安装方法不能随意来回切换并期待读到同一个数据库**。先按 [备份和恢复说明](P4_USAGE.md#全量历史导出与恢复) 做停写快照，再规划迁移。

## 备份和换机器

Python 路线先停止运行中的所有洄海服务，在项目目录执行：

```bash
./.venv/bin/thalemora backup ./state ./snapshot
```

把整个 `snapshot` 文件夹复制到另一台可信的电脑（它可能包含你授权的私人历史）；在新机器**新空目录**恢复：

```bash
./.venv/bin/thalemora restore ./snapshot ./restored
```

之后用 `./.venv/bin/thalemora --data ./restored serve` 启动。**恢复不包括环境变量里的管理密钥或模型 API Key**。Docker 卷的备份不能直接照抄 `./state` 命令；需要在停止写入、确认卷对应实际数据路径后按 [完整备份说明](P4_USAGE.md#全量历史导出与恢复) 处理，不能用删除卷代替备份。

**下一步**：按 [普通用户入门](GETTING_STARTED.md) 导入合成数据；如需 HTTP MCP 和自动维护，请注意 [旧版 v0.4.0 与 v0.5.0 的差别](GETTING_STARTED.md#第一次使用洄海从安装到看到第一条记忆)。
