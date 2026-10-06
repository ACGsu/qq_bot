# 依赖与环境要求

本目录集中管理可纳入版本控制的依赖清单和环境说明；**不存放真实密码、Token、API Key 或虚拟环境**。

## 清单划分

| 文件 | 用途 |
| --- | --- |
| [bot.txt](bot.txt) | 机器人运行依赖：课表解析、时区和图片绘制；Docker 镜像只安装此清单 |
| [webui.txt](webui.txt) | 宿主机 WebUI 的运行依赖，不依赖机器人容器启动 |
| [webui-dev.txt](webui-dev.txt) | WebUI 测试和隔离预览依赖，通过 `-r webui.txt` 包含 WebUI 运行依赖；全项目回归另需安装 `bot.txt` |

此次只调整清单位置，不改变依赖版本。`webui-dev.txt` 中的相对路径以该清单所在目录为基准。

## 环境要求

| 项目 | 要求与用途 |
| --- | --- |
| Python | 3.11 或更新版本；机器人镜像使用 Python 3.11 |
| Docker Desktop / Docker Engine + Compose v2 | 通过容器部署机器人、使用 WebUI 容器管理功能时需要；仅启动 WebUI 和编辑配置不需要运行 Docker |
| NapCat / OneBot v11 | 真实 QQ 消息功能需要，连接配置见根目录 README |
| FFmpeg | 歌曲音频转换需要；Dockerfile 已安装，宿主机直接运行机器人时需自行安装并加入 PATH |
| 中文字体 | 图片课表需要；镜像安装 Noto CJK，宿主机运行的字体说明见根目录 README |
| Node.js | 仅运行 `.mjs` 前端回归时需要，支持 ES modules 和 `node:` 内置模块；日常使用不需要，也无需 npm 安装 |
| 浏览器 | 支持现代 JavaScript 的浏览器，如当前版本 Edge / Chrome |

## 安装命令（在项目根目录执行）

以下为 Windows PowerShell 示例。已有虚拟环境就复用，不删除或重新创建。安装依赖需要访问包索引；日常启动无需重复安装。

### WebUI 日常使用

```powershell
if (-not (Test-Path -LiteralPath ".venv-webui\Scripts\python.exe")) {
    python -m venv .venv-webui
}
.\.venv-webui\Scripts\python.exe -m pip install -r requirements/webui.txt
```

在根目录设置好 `.webui.env` 后，双击 `启动WebUI.cmd`。密码设置与启动步骤见 [使用说明](../使用说明.md)。

### 宿主机直接运行机器人（可选）

使用 Docker 部署时无需在宿主机安装这组 Python 依赖，镜像构建会自行安装。

```powershell
if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    python -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install -r requirements/bot.txt
```

这只是依赖安装步骤。宿主机启动时的环境变量加载、FFmpeg 与字体配置见 [README](../README.md)。

### 开发与全项目回归（可选）

```powershell
.\.venv-webui\Scripts\python.exe -m pip install -r requirements/webui-dev.txt -r requirements/bot.txt
```

这里只列安装命令，不自动执行测试。测试入口见根目录 README。

## 为什么不把所有环境文件都移进来？

- `.env`：保留根目录，供 Docker Compose 及现有配置读取逻辑使用。
- `.webui.env`：保留根目录，供正式 WebUI 和快捷启动检查使用。管理员密码仍在此设置。
- `.venv/`、`.venv-webui/`：保留原位置；虚拟环境可能记录绝对路径，不宜直接移动。
- `Dockerfile`、`docker-compose.yml`：保留根目录，保持构建上下文、配置挂载和现有操作命令不变。
- `plugin_config.json`、运行锁、数据库和备份：属于配置或运行数据，不是依赖清单，不移动到本目录。

**已有环境无需因本次整理重新安装，现有容器也无需仅因此重建。** 以后安装或构建时使用新路径即可。
