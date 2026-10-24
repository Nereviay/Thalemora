# Linux / 服务器安装

需要 Python 3.10+ 和 Git，或者 Docker Compose。第一次启动不需要模型 Key。

## 用 Python 安装

先检查环境：

```bash
python3 --version
git --version
```

Ubuntu/Debian 如缺少组件，可以先安装：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

下载并安装：

```bash
git clone --depth 1 --branch v0.8.0 https://github.com/Nereviay/Thalemora.git
cd Thalemora
python3 -m venv .venv
./.venv/bin/python -m pip install -c requirements.lock .
```

设置管理密钥并启动：

```bash
export THALEMORA_TOKEN="$(./.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf '%s\n' "$THALEMORA_TOKEN"
./.venv/bin/thalemora --data ./state serve
```

保存终端显示的密钥。在同一台设备上打开 **http://127.0.0.1:8765**，用它登录。

停止时按 `Ctrl+C`。下次在同一项目目录设置原密钥，再运行最后一行；继续使用原 `state`，不用重新安装。

## 服务在远程 VPS 上？

自己电脑的 `127.0.0.1` 不能直接访问 VPS。临时管理时，可以在自己电脑的终端建立 SSH 转发：

```bash
ssh -L 8765:127.0.0.1:8765 你的用户名@服务器地址
```

保持连接，再在自己电脑打开 `http://127.0.0.1:8765`。这只解决该电脑的访问；手机插件需要手机也能访问的可信通道，或由你配置 HTTPS 反向代理。

默认服务只监听本机。不要直接把整个管理接口裸露到公网。

## 用 Docker Compose 安装

下载相同版本源码，在项目目录设置并保存 `THALEMORA_TOKEN`，然后：

```bash
docker compose -f deploy/compose.yaml up --build -d
docker compose -f deploy/compose.yaml logs --tail 30
```

停止：`docker compose -f deploy/compose.yaml stop`。下次设置原密钥后用 `up -d` 启动。

数据在 Docker 命名卷中，不是 Python 路线的 `./state`。不要删除卷来更新程序。模型 API Key 如需在容器中使用，必须自行通过容器环境变量提供。

下一步：[新手指南](GETTING_STARTED.md) · [插件配置](PLUGIN_SETUP.md) · [备份与恢复](MAINTENANCE.md)。
