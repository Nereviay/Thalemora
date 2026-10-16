# Windows 安装

适用于 Windows 10/11。第一次需要联网下载 Python 和依赖；安装后，基础检索不需要模型 API。

## 1. 下载程序

1. 从 [Python 官网](https://www.python.org/downloads/windows/)安装 Python 3.12，保留 Python Launcher 选项。
2. 下载 [洄海 v0.7.0 源码 ZIP](https://github.com/Nereviay/Thalemora/archive/refs/tags/v0.7.0.zip)，解压到自己容易找到的文件夹。
3. 在解压后的项目文件夹点击地址栏，输入 `powershell` 并回车。

不要在压缩包内部运行，也不要把记忆数据放进临时下载目录。

## 2. 安装依赖

逐行运行：

```powershell
py -3.12 --version
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c requirements.lock .
```

第一行应显示 Python 3.12.x。最后一行会下载组件，等它结束后继续。不需要更改 PowerShell 脚本执行策略。

## 3. 设置管理密钥并启动

```powershell
$env:THALEMORA_TOKEN = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:THALEMORA_TOKEN
.\.venv\Scripts\thalemora.exe --data .\state serve
```

第二行显示管理密钥，请保存到自己的密码管理器。它能管理整个实例，不要分享。

保持终端开着，用浏览器打开 **http://127.0.0.1:8765**，填入密钥。然后按[新手指南](GETTING_STARTED.md#2-安装后先做这四件事)注册 AI、授权来源和导入练习数据。

## 下次怎样启动？

进入同一个项目文件夹打开 PowerShell，使用原密钥：

```powershell
$env:THALEMORA_TOKEN = "自己保存的原管理密钥"
.\.venv\Scripts\thalemora.exe --data .\state serve
```

`state` 是数据目录，不要删除或换成另一个空目录。停止服务时在终端按 `Ctrl+C`，等程序退出。

## 已经装有 Docker Desktop？

启动 Docker Desktop，在项目目录设置好上面的管理密钥，再运行：

```powershell
docker compose -f deploy/compose.yaml up --build -d
```

管理台地址仍为 `http://127.0.0.1:8765`。停止用 `docker compose -f deploy/compose.yaml stop`。

Docker 数据保存在命名卷，不在 Python 的 `state` 文件夹里。不要执行 `down -v` 或删除数据卷来更新程序。第一次接触 Docker，优先使用上面的 Python 路线；Docker Desktop 本身的安装见[官方说明](https://docs.docker.com/desktop/setup/install/windows-install/)。

## 遇到问题

| 情况 | 处理方法 |
| --- | --- |
| 找不到 `py` | 确认 Python 和 Launcher 已安装，再重新打开 PowerShell。 |
| 安装依赖失败 | 查看终端最后的错误，先检查下载网络和 Python 版本。 |
| 管理台打不开 | 确认服务终端仍在运行、端口 8765 没被其他程序占用。 |
| 提示 401 | 浏览器中填写的密钥必须与当前终端设置一致。 |

下一步：[连接聊天插件](PLUGIN_SETUP.md) · [MCP](MCP.md) · [备份与升级](MAINTENANCE.md)。
