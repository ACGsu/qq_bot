# Docker 全新部署说明

更新日期：2026-10-26。本说明使用 `D:/qq_bot/compose.yaml` 创建 NapCat、机器人和 WebUI 三个容器；阶段 0～12 已全部完成，数据库／缓存重建保留、OneBot 恢复连接和授权群内既有课表查询均已验证。

## 1. 准备配置

启动 Docker Desktop，使用 Linux 容器模式。在 PowerShell 中进入项目目录：

```powershell
Set-Location D:\qq_bot
```

保留现有 `.env`，对照 `.env.example` 补充需要的变量，不要直接覆盖真实配置。

首次克隆时，仅为缺失的文件复制示例：

```powershell
$initialConfigs = @{
    '.env.example' = '.env'
    'qqbot/config/plugin_config.example.json' = 'qqbot/config/plugin_config.json'
    'qqbot/config/webui.env.example' = 'qqbot/config/webui.env'
}
foreach ($source in $initialConfigs.Keys) {
    $destination = $initialConfigs[$source]
    if (-not (Test-Path -LiteralPath $destination)) {
        Copy-Item -LiteralPath $source -Destination $destination
    }
}
```

复制后填写自己的 QQ、token 和 WebUI 密码。`webui.env.example` 的密码为空，未填写时 WebUI 会拒绝启动；插件示例只启用 `basic`，其他功能通过 WebUI 开启。原生机器人要求 `plugin_config.json` 存在并通过严格校验，缺失或损坏时不会默认启用全部插件。真实文件均被 Git 忽略，只有两个无秘密值的示例允许提交。

| 配置 | 文件与用途 |
| --- | --- |
| `BOT_QQ` | 根目录 `.env`，机器人 QQ 账号 |
| `NAPCAT_ACCESS_TOKEN` | 根目录 `.env`，OneBot token，NapCat 反向客户端需设置相同值 |
| `NAPCAT_WEBUI_TOKEN` | 根目录 `.env`，NapCat 管理页面 token |
| `WEBUI_PORT` | 根目录 `.env`，机器人 WebUI 的宿主机端口，默认 `8765` |
| `NAPCAT_WEBUI_PORT` | 根目录 `.env`，NapCat 管理页面端口，默认 `6099` |
| `WEBUI_PASSWORD` | `qqbot/config/webui.env`，16～512 字符的独立管理员密码 |
| `MANAGEMENT_ACCESS_TOKEN` | 根目录 `.env`，可选的独立管理令牌；指定时至少 32 字符且不能与 OneBot token 相同，留空时自动生成并写入 `qqbot/storage/webui/management.token` |

Docker 对外 WebUI 端口由根目录 `.env` 的 `WEBUI_PORT` 控制；容器内固定为 `8765`，不使用 `qqbot/config/webui.env` 的端口值。修改密码或端口后执行 `docker compose -f compose.yaml up -d --no-deps --force-recreate webui`。

确认 `qqbot/config/plugin_config.json` 和以下挂载目录存在；仓库用 `.gitkeep` 保留空目录，Compose 的 `create_host_path: false` 不会自动创建缺失的宿主机目录：

```text
D:/qq_bot/
├─ napcat/
│  ├─ config/
│  ├─ qq/
│  └─ plugins/
└─ qqbot/
   ├─ config/
   └─ storage/
      ├─ db/
      ├─ downloads/
      ├─ cache/
      ├─ backups/
      └─ webui/
```

## 2. 构建镜像

检查 Compose 配置，再构建机器人和 WebUI 镜像；每一步成功后再执行下一步：

```powershell
docker compose -f compose.yaml config --quiet
docker compose -f compose.yaml build qq-bot webui
```

生成 `qqbot-app:local` 和 `qqbot-webui:local`。Dockerfile 会安装各自的依赖，宿主机无需安装 NoneBot 或 Python 依赖。

隔离回归使用独立测试镜像，不挂载正式配置、数据库、NapCat 登录目录，也不连接真实 QQ：

```powershell
docker compose -f compose.test.yaml build tests
docker compose -f compose.test.yaml run --rm tests
```

测试镜像和测试 Compose 不会替换 `compose.yaml` 的生产容器。

2026-10-25 锁定依赖后的测试记录包含基础与课表 pytest 223 项、原生 Matcher／Adapter API／HTTP Driver／业务边界回归 44 项、WebUI Python 回归 58 项，以及全部前端 JavaScript 回归。歌曲下载文件会写入持久化的 `storage/downloads/song-cache`，默认缓存 24 小时；可通过 `SONG_CACHE_TTL_HOURS` 调整。

完整测试入口与旧测试迁移清单见 [qqbot/tests/README.md](qqbot/tests/README.md)。

NapCat 使用现成镜像，单独拉取：

```powershell
docker compose -f compose.yaml pull napcat
```

`.env.example`、Compose 默认值和 Dockerfile 构建参数已经固定 NapCat、Python 3.11 bookworm 与 Docker CLI 29 的不可变摘要。bot、WebUI 和测试镜像分别使用 `requirements/bot.lock`、`webui.lock`、`dev.lock`，安装时强制校验发行包哈希；`.txt` 文件仅作为维护输入。

## 3. 创建并启动容器

```powershell
docker compose -f compose.yaml up -d --no-build
docker compose -f compose.yaml ps
```

`build` 构建镜像，`up` 创建并启动容器。上述 `up` 会启动 `napcat`、`qq-bot`、`webui`，自动建立项目网络并加载 Compose 中的目录挂载。

主要持久化目录如下：

| 宿主机目录 | 容器目录 | 权限 |
| --- | --- | --- |
| `napcat/config` | napcat `/app/napcat/config` | 可写 |
| `napcat/qq` | napcat `/app/.config/QQ` | 可写 |
| `napcat/plugins` | napcat `/app/napcat/plugins` | 可写 |
| `qqbot/config` | qq-bot `/app/config` | 只读 |
| `qqbot/storage` | qq-bot `/app/storage` | 可写 |
| `qqbot/config` | webui `/app/config` | 可写 |
| `qqbot/storage/webui` | webui `/app/storage/webui` | 可写 |

源码复制进镜像，配置与运行数据使用目录挂载；修改源码或依赖后需要重新构建对应镜像。

## 4. 登录和连接

默认本机访问地址：

- NapCat 管理页面：`http://127.0.0.1:6099`。
- 机器人 WebUI：`http://127.0.0.1:8765`，使用 `qqbot/config/webui.env` 的管理员密码。

在 NapCat 管理页面登录 QQ，确认 OneBot v11 反向 WebSocket 客户端启用，目标地址为：

```text
ws://qq-bot:8080/onebot/v11/ws
```

客户端 token 与 `.env` 的 `NAPCAT_ACCESS_TOKEN` 一致。`NAPCAT_WEBUI_TOKEN` 用于管理页面登录，与 OneBot token 分别设置。NapCat 的环境变量只初始化缺失配置，因此应在页面核对实际连接地址和 token。

需要查看机器人日志或确认目录挂载时执行：

```powershell
docker compose -f compose.yaml logs --tail 100 qq-bot
docker inspect my-bot --format '{{json .Mounts}}'
```

按 2026-10-26 最终验收记录，机器人锁定 NoneBot2 2.5.0、OneBot Adapter 2.4.6，并通过官方机制加载 nonebot-plugin-apscheduler 0.5.0。仅重建 qq-bot 前后，正式课表库均为 schema v1、WAL、完整性 `ok`、1 条导入／102 条课程实例且脱敏逻辑哈希一致；song 缓存均为 3 个文件、86,885,498 字节。验证过程没有复制或清空数据。NapCat 重新登录后 OneBot 已恢复连接，用户已在授权群确认 `/课ing`、`/今日课程` 可查询既有课表。

qq-bot 会继续接收全部 OneBot 事件，保证 `auto_emoji` 等被动插件正常工作；它的控制台日志只显示真正 `@bot` 的消息事件，并隐藏普通消息、通知事件和自动贴表情 Matcher 的开始／完成日志。NapCat 容器的自身日志不受此过滤影响。
