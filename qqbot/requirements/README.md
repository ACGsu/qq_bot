# 依赖与环境要求

本目录集中管理可纳入版本控制的依赖清单和环境说明；**不存放真实密码、Token、API Key 或虚拟环境**。

## 清单划分

| 文件 | 用途 |
| --- | --- |
| [bot.txt](bot.txt) | 机器人与 NoneBot2/OneBot Adapter 的维护输入；机器人镜像安装对应 `bot.lock` |
| [webui.txt](webui.txt) | 独立 WebUI 的维护输入；管理镜像安装对应 `webui.lock`，不依赖机器人容器启动 |
| [webui-dev.txt](webui-dev.txt) | 保留的宿主机 WebUI 预览依赖，包含 `webui.txt` 和 HTTPX；不包含完整 pytest/NoneBug 套件 |
| [dev.txt](dev.txt) | 完整测试环境的维护输入，包含 `bot.txt`、`webui.txt`、pytest、pytest-asyncio、NoneBug 和 PyYAML；测试镜像安装 `dev.lock` |
| `bot.lock` / `webui.lock` / `dev.lock` | Python 3.11 下解析出的直接与传递依赖精确版本及发行包 SHA-256；对应 Dockerfile 使用 `--require-hashes` 安装 |

`.txt` 是便于维护的输入清单；镜像实际安装 `.lock`。机器人锁定 NoneBot2 2.5.0、OneBot Adapter 2.4.6 和标准第三方插件 nonebot-plugin-apscheduler 0.5.0。测试依赖只进入 `Dockerfile.test`；前端回归的 Node.js 18.20.4 和 Noto CJK `1:20220127+repack1-1` 来自固定 Python 基础镜像对应的 Debian bookworm 仓库快照。`webui-dev.txt` 中的相对路径以本目录为基准。

## 环境要求

| 项目 | 要求与用途 |
| --- | --- |
| Python | 3.11 或更新版本；机器人镜像使用 Python 3.11 |
| Docker Desktop / Docker Engine + Compose v2 | 通过容器部署机器人、使用 WebUI 容器管理功能时需要；仅启动 WebUI 和编辑配置不需要运行 Docker |
| NapCat / OneBot v11 | 真实 QQ 消息功能需要，连接配置见根目录 README |
| 中文字体 | 图片课表需要；镜像安装 Noto CJK，宿主机运行的字体说明见根目录 README |
| Node.js | 仅运行 `.mjs` 前端回归时需要，支持 ES modules 和 `node:` 内置模块；日常使用不需要，也无需 npm 安装 |
| 浏览器 | 支持现代 JavaScript 的浏览器，如当前版本 Edge / Chrome |

## 安装命令（在项目根目录执行）

生产部署和完整回归都使用 Docker，宿主机不需要安装 NoneBot、业务依赖或恢复已删除的 `.venv`：

```powershell
Set-Location D:\qq_bot
docker compose -f compose.yaml build qq-bot webui
docker compose -f compose.test.yaml build tests
docker compose -f compose.test.yaml run --rm tests
```

只有选择宿主机直接运行 WebUI 时，才需要单独创建 `.venv-webui` 并安装 `webui.txt`；这不是 Docker 部署前置条件。生产和测试 Dockerfile 分别安装 `bot.lock`、`webui.lock`、`dev.lock`。输入清单变化后，应在隔离的 Python 3.11 环境用 `pip-compile --generate-hashes --strip-extras` 重新生成三个锁文件，再执行无缓存构建和完整回归。

## 为什么不把所有环境文件都移进来？

- `.env`：保留根目录，供 Docker Compose 及现有配置读取逻辑使用。
- `qqbot/config/webui.env`：保存正式 WebUI 的独立管理员密码；由 Compose 通过 `WEBUI_ENV_FILE` 指定。Docker 宿主机端口在根目录 `.env` 中设置，本文件的端口仅供宿主机 WebUI 使用。
- `qqbot/config/plugin_config.example.json` 和 `webui.env.example`：可提交的初始化示例；真实配置不提交 Git，整个 `config/` 不进入构建上下文。
- `.venv/`、`.venv-webui/`：不属于生产镜像；当前 Docker 部署不依赖它们。
- `compose.yaml`、旧文件名兼容入口和 `compose.test.yaml` 保留根目录；两个生产 Dockerfile 和一个测试 Dockerfile 位于 qqbot，构建上下文为 qqbot。
- 插件配置位于 qqbot/config；运行锁、数据库、下载和原有备份位于 qqbot/storage。

完整回归通过 `compose.test.yaml` 使用 Docker 测试镜像执行；分组方式和旧测试迁移边界见 [tests/README.md](../tests/README.md)。
