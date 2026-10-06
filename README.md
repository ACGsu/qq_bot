# NapCat QQ Bot

一个通过 NapCat OneBot v11 WebSocket 收发消息的插件式 QQ bot，支持正向连接（bot 作为客户端）和反向连接（bot 作为服务端）。当前 Docker Compose 默认使用反向 WebSocket。

宿主机直接运行需要 Python 3.11 或更新版本，镜像使用 Python 3.11。WebSocket 等核心功能使用标准库；课表解析依赖 `requirements/bot.txt` 中的 `icalendar`、`python-dateutil` 和 `tzdata`；图片课表使用 Pillow 和中文字体，不需要浏览器。歌曲转换额外依赖 FFmpeg。Dockerfile 会安装这些依赖及可再分发的 Noto CJK 字体，构建需要能够访问基础镜像源、Alpine 软件源和 Python 包索引。

## 文档导航与当前状态

- **日常使用**：[使用说明.md](使用说明.md)——从启动 WebUI 到配置插件、保存应用和常见问题。
- **首次部署与技术参考**：本文包含 Docker／NapCat 连接、环境变量、插件行为和开发测试说明。
- **依赖与环境要求**：[requirements/README.md](requirements/README.md)——Python、系统依赖、安装方式和配置文件位置。

WebUI 是当前唯一的管理界面入口，运行于宿主机，不依赖 bot 容器启动。用户已于 2026-10-01 确认人工核验完成；旧 Tkinter 入口及其专属测试已清理。验收记录来自用户确认，本轮收尾仅整理文件与文档，不操作容器或发送真实消息。

## 功能与指令

普通指令必须放在真正的 `@bot` 消息段后面，直接输入文字 `@bot` 不等同于 QQ 的 @ 操作。程序同时支持 OneBot 消息段数组和 CQ 码字符串；私聊也会检查 @，不会直接响应未带 @ 的裸指令。

下表列出项目支持的功能，实际可用项取决于插件配置。`basic` 插件启用时，群内 `@bot /help` 以可点击的分类卡片展示当前已启用插件的指令；`@bot /help 文本` 保留完整文字列表。

| 插件 ID | 指令或触发方式 | 行为 |
| --- | --- | --- |
| `basic` | `@bot /help`、`@bot /help 文本`、`@bot /help 分类名`、`@bot /hello` | 群内分层卡片导航、文字帮助；回复 `啦啦啦` |
| `rps` | `@bot /猜拳 @成员` | 群内随机猜拳，输家按配置时长禁言（默认 60 秒），平局不禁言 |
| `daily_wife` | `@bot /今日群友`（或 `/今日老婆`）、`@bot /结芬`、`@bot /愿意`、`@bot /不愿意` | 群内每日抽取群友，结芬子功能可独立开关 |
| `courtship` | `@bot /求偶 @成员`、`@bot /接受求偶`、`@bot /拒绝求偶` | 群内求偶及回应 |
| `song` | `@bot /song 歌曲名`、`@bot /song 序号`、`@bot /song 取消` | 搜索候选、下载所选歌曲并转换为 MP3 文件发送 |
| `novel` | `@bot /novel 小说名`、`@bot /第3章`、`@bot /第2卷` | 查询轻小说目录及阅读链接 |
| `timetable` | `@bot /课表`、`@bot /导入课表`、`@bot /已导入`、`@bot /更新课表`、`@bot /课ing`、`@bot /今日课程`、`@bot /明日课程`、`@bot /取消导入` | 导入或更新本人的 ICS 课表并持久保存；以图片展示本群当前课程或本人今日课表，失败时文字回退；不支持 Excel |
| `summary` | `@bot /总结`、`@bot /总结 100` | 调用 DeepSeek 总结当前群最近的消息，默认 100 条，最多 500 条 |
| `auto_emoji` | 指定 QQ 号在群内发言 | 自动贴配置的表情，属于被动监听，**不需要 @bot** |

未知或已关闭插件的指令会回复 `没有该指令喵~`，并在素材存在时附带提示图片。

### /help 分层导航

群内发送 `@bot /help` 后，bot 只发送一张外层「功能导航」合并转发卡片。打开后可以点击分类卡片进入第二层指令说明；使用 QQ 自带的返回按钮回到上一级。例如，相关插件启用时可以浏览：

```text
功能导航
├─ 今日老婆（点击进入）
│  ├─ /今日群友 或 /今日老婆
│  ├─ /结芬
│  └─ /愿意 或 /不愿意
├─ 课表（点击进入）
│  ├─ /课表
│  ├─ /导入课表
│  ├─ /已导入
│  ├─ /更新课表
│  ├─ /课ing
│  ├─ /今日课程
│  ├─ /明日课程
│  └─ /取消导入
└─ 基础指令等其他已启用分类（点击进入）
```

- 点击只是在 QQ 客户端中浏览帮助，**不会自动执行或发送业务指令**。执行指令仍需回到聊天中真正 `@bot` 后输入；实际课表导入指令仍为 `/导入课表`，没有重命名。
- `@bot /help 文本` 或 `@bot /help 全部`：查看全部文字帮助；也接受 `text` / `all`。
- `@bot /help 课表`：只查看课表分类的文字帮助。也支持「今日老婆 / 今日群友」「基础 / 基础指令」「歌曲 / 点歌 / 歌曲下载」「小说 / 小说链接」「总结 / 群聊总结」、其他分类标题及插件 ID（例如 `timetable`）。未知或未启用分类会提示当前可用分类。
- 私聊中的 `/help` 保留文字帮助，不发送群导航卡片；仍遵守真正 `@bot` 的要求。
- 卡片、分类文字和完整文字列表共用帮助数据：隐藏关闭的插件；关闭 `daily_wife.marriage_enabled` 时，也隐藏结芬及其回应指令。被动的自动贴表情不作为指令分类。
- 每次请求重新生成卡片，不缓存资源 ID。已有卡片是生成时的快照，不能随配置改变；在 WebUI 保存并应用配置或完成容器重新创建后，请重新发送 `@bot /help` 获取新卡片。
- 通过现有 OneBot WebSocket 调用 `send_group_forward_msg`，不需要新增 WebUI 凭据、端口或外部链接。所有帮助节点以 bot 自己的 QQ 号生成，不引用群成员的聊天记录或身份。
- 接口失败、30 秒内没有确认，或回执缺少有效 `message_id` 时，回退一次文字帮助，不重试发送卡片。超时并不证明卡片没有发出，因此偶尔可能同时看到卡片和回退文字；如果客户端无法打开卡片，也可主动使用 `/help 文本`。

发送成功以 OneBot 发送回执为准，不额外回读 `get_forward_msg`。本环境的客户端验证表明，嵌套节点在该接口中回读为空，并不代表客户端无法显示或点击。

## 项目结构

```text
qq_bot/
├── bot.py / plugin_control.py       # 机器人入口与共享配置逻辑
├── run_webui.py / 启动WebUI.cmd      # 本地 WebUI 入口（保持在根目录）
├── plugins/                         # 自动表情、猜拳、课表等业务插件
├── webui/                           # 认证、配置、Docker 任务与 API
│   └── static/                      # 页面、脚本、样式及运行所需背景图
├── assets/                          # 机器人提示图与 WebUI 背景原图
├── tests/                           # 正式 Python / JavaScript 回归与隔离预览
├── README.md / 使用说明.md           # 部署参考与日常使用手册
├── Dockerfile / docker-compose.yml  # 机器人镜像、服务与数据卷
├── requirements/                    # 依赖清单与环境要求
│   ├── bot.txt                      # 机器人运行依赖
│   ├── webui.txt                    # WebUI 运行依赖
│   ├── webui-dev.txt                # WebUI 测试依赖（包含 webui.txt）
│   └── README.md                    # 环境要求与安装命令
├── .env / .webui.env                # 本机私密配置，自行创建，不纳入 Git
└── plugin_config.json              # 本机插件配置，不纳入 Git
```

依赖清单集中在 `requirements/`，但 `.env`、`.webui.env`、虚拟环境和启动入口仍保留原位置。仅移动清单不需要重新安装依赖或重建现有容器。

本机的 `.venv/`、`.venv-webui/`、`backups/`、运行锁以及所有 `__pycache__/` 均保留，不是本次清理对象。课表数据仍由原持久化目录／Docker 卷管理。旧测试输出日志已清理；`tests/webui_preview.py` 是可复用的隔离预览工具，不是临时垃圾。不要移动根目录的启动入口、Compose 或私密配置，以免相对路径失效。

## 快速开始：Docker Compose

需要 Docker Engine / Docker Desktop（Linux 容器模式）和 Docker Compose，以及已经运行、登录并启用 OneBot 的 NapCat。**本项目的 Compose 只启动 bot，不包含 NapCat 服务。**

以下 PowerShell 命令均在项目根目录执行。本节面向首次真实部署；如果目前只想使用管理页面且不准备启动容器，请先看 [使用说明](使用说明.md)，跳过本节的网络和构建操作。

### 1. 准备本地配置

仅在文件不存在时创建，避免覆盖已有配置：

```powershell
if (-not (Test-Path -LiteralPath ".env")) {
    'WEBSOCKET_MODE=server','NAPCAT_ACCESS_TOKEN=','BOT_QQ=' | Set-Content -LiteralPath ".env" -Encoding ascii
}
if (-not (Test-Path -LiteralPath "plugin_config.json")) {
    '{"enabled_plugins":["auto_emoji","rps","daily_wife","courtship","song","novel","summary","timetable","basic"],"plugin_settings":{}}' | Set-Content -LiteralPath "plugin_config.json" -Encoding ascii
}
```

- 编辑 `.env`，将 `BOT_QQ` 改为真实的机器人 QQ 号，或留空以使用 NapCat 事件中的 `self_id`；不要保留示例号码。
- 按需填写 `NAPCAT_ACCESS_TOKEN`、自动表情、歌曲下载和 DeepSeek 配置，详见下方配置表。
- 上面的初始配置显式启用当前 9 个插件，兼容机器人和 WebUI；具体设置仍按各插件规则回退。**不要再用 `{}` 初始化**：机器人兼容读取会把它当成默认配置，但 WebUI 为避免静默覆盖，要求已有文件包含字符串数组 `enabled_plugins`。文件不存在时也可以先在 WebUI 检查默认选项，再主动点击“保存配置”创建；只打开页面不会创建配置文件。
- `plugin_config.json` 被 Git 和 Docker 构建上下文忽略，不会打入新镜像。Compose 会将它作为只读文件挂载，因此**首次启动前必须先创建这个文件**，不能用同名目录代替；已有配置不要重置。

### 2. 准备 Docker 网络

Compose 使用名为 `qqbot-net` 的外部网络，不会自动创建它。首次部署执行；若网络已经存在则跳过：

```powershell
docker network create qqbot-net
```

默认反向连接方案要求 NapCat 容器也加入该网络。下面的 `napcat` 要替换为实际 NapCat 容器名，已经接入时无需重复执行：

```powershell
docker network connect qqbot-net napcat
```

如果 NapCat 也通过 Compose 管理，建议在它的 Compose 中声明并接入同一个外部网络，以免重建 NapCat 容器后丢失网络连接。

### 3. 配置 NapCat 反向 WebSocket

在 NapCat 中启用反向 WebSocket 客户端，目标地址填写：

```text
ws://my-bot:8080
```

`my-bot` 是当前 Compose 设置的容器名和网络别名，只适用于能访问该 Docker 网络的客户端。若设置了 `NAPCAT_ACCESS_TOKEN`，NapCat 侧也必须配置相同 token；bot 校验的是 `Authorization: Bearer <token>` 请求头，不支持仅把 token 放在 URL 查询参数中。

### 4. 构建、启动和查看状态

```powershell
docker compose up -d --build --force-recreate --no-deps qq-bot
docker compose ps
```

如果 Alpine 官方软件源连接超时，可**仅在构建时**指定可信 HTTPS 镜像源，例如清华镜像站；仍使用 Alpine 原有软件包签名校验，不使用 `--allow-untrusted` 或关闭证书校验：

```powershell
docker compose build --build-arg ALPINE_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/alpine qq-bot
docker compose up -d --no-deps --no-build qq-bot
```

`ALPINE_MIRROR` 默认仍为 Alpine 官方 CDN，不是运行时环境变量；后续需要重新构建时可继续传入同一参数。先完成构建和测试再重建容器，构建失败不会替换正在运行的 bot；课表数据卷也不会被删除。

构建需要支持 BuildKit 的 Docker（当前 Docker Desktop 默认支持）。Alpine 包先下载到 BuildKit 的独立缓存，再校验并安装；网络连续 30 秒无进展时超时，最多尝试 3 次，失败则停止构建，不会启动缺少依赖的新版本。下载缓存不进入最终镜像，也与机器人运行时的课表图片无关；HTTPS 证书及 Alpine 包签名校验始终开启。

查看日志：

```powershell
docker compose logs -f qq-bot
```

停止 bot（不会删除外部网络）：

```powershell
docker compose stop qq-bot
```

## 其他连接方式

### NapCat 运行在宿主机

当前 Compose **没有发布宿主机端口**，仅修改 `LISTEN_PORT` 不会自动开放端口。宿主机上的 NapCat 无法直接使用 Docker 网络别名 `my-bot`；按默认监听端口，可在现有 `services.qq-bot` 下增加以下配置：

```yaml
services:
  qq-bot:
    ports:
      - "127.0.0.1:8080:8080"
```

这是要合并进原 Compose 的片段，不是完整替代文件。重新创建容器后，同一宿主机上的 NapCat 使用 `ws://127.0.0.1:8080`。如果修改了容器内的 `LISTEN_PORT`，需要同步修改映射右侧端口。

跨机器连接还需调整绑定地址和防火墙；只允许可信来源访问，并配置 token，不要将未鉴权的 OneBot 接口暴露到公网。

### bot 主动连接 NapCat

在 NapCat 中开启正向 WebSocket 服务，将 `.env` 改为：

```env
WEBSOCKET_MODE=client
NAPCAT_WS_URL=ws://host.docker.internal:3001
```

- 上述地址适用于 Docker Desktop 中的 bot 访问宿主机 NapCat；端口应以 NapCat 实际配置为准。
- 如果两者在同一个 Docker 网络中，可以使用 NapCat 的容器名或网络别名，例如 `ws://napcat:3001`。
- Linux Docker Engine 若不能解析 `host.docker.internal`，可在服务中配置 `extra_hosts: ["host.docker.internal:host-gateway"]`，或改用实际可达的宿主机地址。
- 客户端支持 `ws://` 和 `wss://`，断线后按 `RECONNECT_SECONDS` 重连；`LISTEN_HOST`、`LISTEN_PORT` 在此模式下不使用。
- 反向服务端模式不使用 `NAPCAT_WS_URL`，由 NapCat 主动连接 bot。

修改连接配置后，按下方“应用配置”步骤重新创建容器。

## 环境变量

首次部署时在根目录创建 `.env`，按下表填写所需变量（Token、QQ 号和 API Key 使用自己的值，不提交 Git）。下表列出**当前 Compose 的默认值**，不是所有源码直接运行时的默认值。

| 变量 | Compose 默认值 | 说明 |
| --- | --- | --- |
| `WEBSOCKET_MODE` | `server` | `server`：接收 NapCat 反向连接；`client`：主动连接 NapCat |
| `NAPCAT_WS_URL` | `ws://host.docker.internal:3001` | 客户端模式连接地址 |
| `NAPCAT_ACCESS_TOKEN` | 空 | WebSocket 鉴权 token，两端需一致；留空不校验 token |
| `BOT_QQ` | 空 | 机器人 QQ 号；未设置时使用消息事件的 `self_id` |
| `LISTEN_HOST` | `0.0.0.0` | 服务端模式监听地址 |
| `LISTEN_PORT` | `8080` | 服务端模式的容器内监听端口 |
| `LOG_LEVEL` | `INFO` | Python 日志级别 |
| `RECONNECT_SECONDS` | `5` | 客户端断线重连间隔，单位秒 |
| `AUTO_CANDY_QQ` | 空 | 自动贴表情的目标 QQ；留空且无插件设置时不触发 |
| `AUTO_EMOJI_IDS` | `127852,12951` | 表情 ID 列表，默认 🍬 和 ㊗ |
| `AUTO_CANDY_EMOJI_ID` | `127852` | 旧版单表情兼容项；当前 Compose 的 `AUTO_EMOJI_IDS` 默认值会优先于它 |
| `SONG_MAX_DOWNLOAD_MB` | `80` | 单个歌曲源文件下载上限，按 1024 × 1024 字节计算 MB |
| `SONG_SESSION_MINUTES` | `10` | 歌曲候选会话有效期，单位分钟 |
| `QUARK_COOKIE` | 空 | 下载夸克分享音频时使用的登录 Cookie |
| `DEEPSEEK_API_KEY` | 空 | 群聊总结使用的 API Key |
| `DEEPSEEK_API_BASE_URL` | `https://api.deepseek.com` | API 根地址，程序会追加 `/chat/completions` |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 总结使用的模型 |
| `TIMETABLE_DB_PATH` | `/app/data/timetable.sqlite3` | Compose 中固定的课表数据库路径；`/app/data` 挂载持久化命名卷 |

`BOT_PLUGIN_CONFIG` 用于指定插件配置文件路径，源码默认读取项目根目录的 `plugin_config.json`。当前 Compose 将它固定为 `/app/plugin_config.json`，并以只读方式挂载宿主机的 `./plugin_config.json`；如需改路径，要同步修改 Compose 的环境变量和挂载，不能只在 `.env` 中改值。

`TIMETABLE_DB_PATH` 在源码直接运行时默认指向项目根目录的 `data/timetable.sqlite3`，首次课表数据库操作时自动创建。当前 Compose 将此变量固定为上表路径；自定义路径必须同步调整数据卷挂载，不能只在 `.env` 中改值。

源码另外支持 `XIAGEBA_BASE_URL`（歌曲站点地址，默认 `https://xiageba.liumingye.cn`）以及旧版 `AUTO_EMOJI_QQ`、`AUTO_EMOJI_ID` 环境变量；这些变量当前没有通过 Compose 透传。需要在容器中使用时，应自行添加到服务的 `environment` 中。

## 插件配置与控制台

### 配置文件

可手动编辑 `plugin_config.json`，例如：

```json
{
  "enabled_plugins": [
    "auto_emoji", "rps", "daily_wife", "courtship",
    "song", "novel", "summary", "timetable", "basic"
  ],
  "plugin_settings": {
    "auto_emoji": {
      "target_qq": "",
      "emoji_ids": "127852,12951"
    },
    "rps": {
      "ban_seconds": "60"
    },
    "daily_wife": {
      "marriage_enabled": "true"
    },
    "timetable": {
      "group_isolation_enabled": "true"
    },
    "summary": {
      "deepseek_api_key": "",
      "deepseek_api_base_url": "https://api.deepseek.com",
      "deepseek_model": "deepseek-chat"
    }
  }
}
```

- 从 `enabled_plugins` 中移除对应 ID 即可关闭插件；`[]` 表示全部关闭，包括 `/help`、`/hello`。未知 ID 在机器人运行时被忽略，WebUI 保留原文件中已有的未知项，不允许凭空添加未注册插件。
- `plugin_settings.rps.ban_seconds` 为 **1～86400 的整数秒字符串**，例如 `"120"`；缺省为 60 秒，不读取另一个猜拳环境变量。机器人侧无效值安全回退并警告，WebUI 则拒绝保存无效值。推荐用页面的秒／分钟输入，无需手工换算或编辑 JSON。
- `plugin_settings.daily_wife.marriage_enabled` 控制结芬子功能：`"true"` 开启，`"false"` 关闭；未配置时默认开启，兼容旧配置。关闭它不影响每日抽取，但关闭整个 `daily_wife` 插件会同时关闭抽取和结芬。
- `plugin_settings.timetable.group_isolation_enabled` 控制课表群聊隔离：`"true"`（默认）按群独立，`"false"` 按 QQ 跨群共享。旧配置缺省或无效值仍保持隔离；不会自动开放原有课表。
- 自动表情和 DeepSeek 的各项设置按“插件配置中的非空值 → 对应环境变量 → 内置默认值”读取。控制台保存的非空设置会覆盖 `.env` 对应项。
- 文件应保存为 UTF-8（无 BOM）。**机器人兼容读取与 WebUI 严格读取不同**：机器人在文件缺失、读取失败、JSON 无效或 `enabled_plugins` 不是列表时，会回退为全部启用；WebUI 只在文件不存在时展示可保存的默认配置，已有文件损坏或字段无效则报错并禁止覆盖。旧文件若只有 `{}`，需先补齐合法的 `enabled_plugins` 列表；不要删除、重置或整份覆盖已有私人设置来排错。
- 旧控制台写入的 `updated_at` 和 `plugins` 元数据会被 WebUI 保留；这些字段不用于动态发现或加载新插件。

### 独立本地 WebUI（新管理入口）

WebUI 使用 FastAPI + Uvicorn，在 **Windows 宿主机**运行，前端无需 Node.js。停止机器人不会关闭管理页面；不要把此服务加入 bot 镜像、挂载 Docker Socket 或开放到公网。

#### 安装与启动

在项目目录使用 Python 3.11+ 创建独立环境（不修改原有 `.venv`）：

```powershell
if (-not (Test-Path -LiteralPath ".venv-webui\Scripts\python.exe")) {
    python -m venv .venv-webui
}
.\.venv-webui\Scripts\python.exe -m pip install -r requirements/webui.txt
```

手动创建 UTF-8（无 BOM）编码的 `.webui.env`，按下面的模板填写：

```dotenv
# 必须自行填写 16～512 字符的独立随机密码；留空将拒绝启动
WEBUI_PASSWORD=
WEBUI_PORT=8765
```

上面的密码值特意留空，没有默认密码；必须自行填写且不能全为空白。**不要使用机器人 API Key 或 NapCat Token 作为登录密码**；私密文件已被 Git 和 Docker 构建上下文排除。服务不会自动创建该文件，也不会读写认证历史或生成备份。

完成依赖安装和密码设置后，可直接双击项目根目录的 [启动WebUI.cmd](启动WebUI.cmd)。脚本复用 `.venv-webui`，不会启动 Docker 或自动安装依赖；出错时保留窗口提示。可通过资源管理器“发送到 → 桌面快捷方式”创建桌面入口，不要移动脚本本身。详细密码设置见 [使用说明](使用说明.md)。

统一启动入口（也可手动运行）：

```powershell
.\.venv-webui\Scripts\python.exe run_webui.py
```

浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，输入自己设置的管理员密码。默认仅监听 `127.0.0.1`；可配置端口，暂不支持远程绑定。会话 1 小时到期，HttpOnly + SameSite=Strict，注销立即失效；每分钟最多 5 次登录尝试。使用本地 HTTP，因此 Cookie 不设置 Secure；未来远程访问必须另行设计 HTTPS，不能直接暴露当前服务。Host、来源和 CSRF 均校验，不支持代理转发。

注意：`WEBUI_PASSWORD`／`WEBUI_PORT` 的同名**进程环境变量优先于 `.webui.env`**。修改该文件后要结束并重启 WebUI 才生效，已有会话失效；这不需要重建机器人容器。端口范围 1024～65535，改端口后使用新的访问地址。启动入口默认只输出警告，终端保持运行且没有报错时可直接打开页面，不必等待“启动成功”文字。仅关闭浏览器不会停止服务；服务终端中 Ctrl+C 才会退出，容器任务执行中请勿关闭。

#### 页面和设置

- **概览**：Docker 可用性、可验证的容器状态、启用插件数、最多 20 项内存任务记录。**容器运行不代表 QQ 已连接**。
- **插件管理**：9 个插件、搜索、状态筛选、单独和批量开关、侧边设置面板。主插件关闭后控件置灰，子功能和原有配置保持不变。
- **猜拳**：秒／分钟输入，保存 `plugin_settings.rps.ban_seconds` 字符串，范围 1～86400 整数秒。缺省 60 秒。运行时无效值安全回退并警告；WebUI 拒绝无效配置。机器人仍需群管理权限。
- **自动贴表情**：直接输入或粘贴 😀、😂、👍、❤️、🔥、🍬、㊗️ 等单码点 Unicode 表情，解析后预览、去重、删除；不限于快捷按钮，内部继续保存兼容的十进制 `emoji_ids`。仅转换符合 Emoji 属性且不会与 QQ 短 ID 混淆的字符，不承诺所有字符或 QQ 专属表情均可用。肤色、旗帜、ZWJ 等组合按完整字素簇拒绝，不拆成错误回应。无法反向映射的旧 ID 显示「旧版表情（保留）」，其他设置修改不会丢弃它。继承环境时只作预览，不主动写入 JSON；实际 NapCat 版本接受情况须授权群验收。
- **今日群友／课表**：分别配置结芬子功能和群聊隔离；关闭隔离会提示跨群共享的隐私影响。
- **群聊总结**：配置 DeepSeek Key、API 地址和模型。Key 不从配置 API 回传；留空保留，勾选才清除 **JSON 中的 Key**。若 `.env` 仍有 Key，清除后会继续继承，WebUI 不修改 `.env`。自定义 API 地址会接收 Key，请只使用信任的服务。
- **日志**：认证后请求最近 200 行，并按输出上限截取展示；支持搜索、暂停自动刷新与滚动，日志量和长行有上限，凭据脱敏。群消息中的个人信息仍需人工检查后再转发。

#### 保存、应用和 Docker 操作

| 操作 | 实际行为 |
| --- | --- |
| 校验配置 | 校验草稿，不写文件、不操作容器 |
| 保存配置 | 版本冲突检查，同目录临时文件原子替换 JSON，无备份，不操作容器 |
| 保存并应用 | 保存后 `docker compose up -d --force-recreate --no-build --no-deps qq-bot` |
| 启动机器人 | `docker compose up -d --no-build --no-deps qq-bot`，没有镜像时提示先构建 |
| 停止机器人 | 仅 `docker compose stop qq-bot`，不删除卷和网络 |
| 重新构建并启动 | `docker compose up -d --build --force-recreate --no-deps qq-bot` |

命令固定项目目录与 Compose 文件，同时只允许一个保存／容器任务。容器任务后台执行并返回 ID、阶段和结果；普通操作限时 120 秒，构建 900 秒，状态／日志／验证 20 秒。超时会结束命令树，但 Docker daemon 可能已经执行部分操作，所以不声称回滚，需刷新实际状态。任务记录仅保存在服务进程内，重启后清空。

应用和构建会验证 **新运行容器 ID** 与容器 `BOT_PLUGIN_CONFIG` 文件的 SHA-256 摘要，不只依据命令退出码。无法验证则显示失败／未知；外部替换容器、改变配置或 Docker 不可用会使验证状态失效。此验证不保证 QQ 连接、群禁言或消息回应成功。保存成功而应用失败时，JSON 仍已保存，应排障后重新应用。

初次部署猜拳新增运行代码必须构建镜像；之后仅调整配置，使用保存并应用，无需构建。不要用 `docker compose restart` 替代重新创建：单文件原子替换挂载可能仍指向旧文件，环境变量也不会随简单重启更新。重新创建会丢失部分内存会话和每日记录，但不删除持久化课表。

配置损坏或不可读时，WebUI 明确报错，不把它显示为「全部启用」并覆盖；需在保留原设置的前提下手动修复原文件。并发保存返回 409，重新加载后再编辑。不要同时在多个页面或外部程序中写配置，外部程序不参加 WebUI 文件锁。

#### 隔离测试与迁移状态

```powershell
.\.venv-webui\Scripts\python.exe -m pip install -r requirements/webui-dev.txt -r requirements/bot.txt
.\.venv-webui\Scripts\python.exe -m unittest discover -s tests -v
```

如已安装 Node.js，可额外执行纯 JavaScript 回归（无需 npm 安装；WebUI 运行本身不需要 Node.js）：

```powershell
node tests/test_webui_duration.mjs
node tests/test_webui_frontend.mjs
```

这两项分别覆盖全部 1～86400 秒的单位往返、十进制校验，以及最小 DOM 模拟下的页面状态／操作锁／Esc 确认，不替代浏览器视觉验收。

WebUI 测试仅使用临时 JSON、临时锁文件和模拟 Docker，不操作真实容器、私人配置或课表卷。可运行 `.\.venv-webui\Scripts\python.exe tests/webui_preview.py` 启动 **18765 端口的隔离页面**；它使用临时数据与测试密码 `test-only-password-123456`，所有 Docker 调用均模拟，绝不能当作正式管理入口。

用户已于 2026-10-01 确认最终人工核验完成。后续版本仍需按变更范围核验界面、配置应用和真实消息；当前结果不代表所有 QQ／NapCat 版本均兼容。旧 GUI 已移除，正式入口统一为 `run_webui.py` 或 `启动WebUI.cmd`。离线回归不等于真实 QQ 在线；Docker 不可用时仍可登录和编辑配置。

#### 常见问题入口

- 页面打不开：确认 WebUI 终端未退出、依赖已安装、密码及端口有效；只改端口时重启 WebUI，不重建 bot。
- Docker 不可用：仍可使用页面与配置功能；等准备好后再启动 Docker Desktop，不用为了查看页面先启动容器。
- “配置损坏”：检查 `enabled_plugins` 是否为字符串数组、设置值是否符合格式；旧 `{}` 不通过 WebUI 校验。不要把文件删掉来消除提示。
- 409 冲突／已有任务：等待当前操作结束；若为版本冲突，确认是否丢弃当前草稿后重新加载，不盲目重试或删除锁文件。
- Key 显示“已设置”但输入框为空：正常的秘密保护行为；留空会保留，清除 JSON 后仍可能继承 `.env`。
- 403／会话问题：使用配置端口的本机直接地址，不走代理；重新登录，不关闭 Host／来源／CSRF 防护。

更多诊断及离线／正式入口区分见 [使用说明](使用说明.md)。

### 应用配置（命令行）

插件开关和设置在 bot 启动时加载，不支持热重载。代码／依赖变更时：

```powershell
docker compose up -d --build --force-recreate --no-deps qq-bot
```

仅配置变更且镜像已有时：

```powershell
docker compose up -d --force-recreate --no-build --no-deps qq-bot
```

## 功能说明

### 课表（ICS 导入、更新与查询）

启用 `timetable` 后，在群内操作（`@bot` 必须使用 QQ 的真正 @ 操作，不是手打文字）。**只支持 ICS，不支持 Excel。**

首次导入：

1. `@bot /导入课表`：开启当前群、当前发送者的文件接收会话。
2. **由同一成员在同一群内上传一个 `.ics` 文件**，上传文件无需 @bot。
3. bot 会 @上传者并回复“文件已接收……尚未解析或入库”。
4. `@bot /已导入`：解析并展开重复课程，**整份成功写入 SQLite 后**才回复“导入成功”，附事件数、课程次数、北京时间日期范围和前 5 次课程预览（课程名、时间、地点）。成功后会话与原始文件暂存自动清除。
5. 重复 `/已导入` 不会重复入库；已有课表再次 `/导入课表` 会提示使用 `/更新课表`，不会擅自覆盖。

更新、取消与查询：

- `@bot /更新课表`：仅已导入成员可用；未导入时提示完整导入步骤。开启后上传新 ICS，再 `@bot /已导入`，解析成功后用一个数据库事务替换本人当前生效的那份课表（隔离模式为本群记录，共享模式为该 QQ 最近更新的记录）。**确认成功前旧课表始终有效，解析/写入失败不会清空旧课表。** 更新保存时还校验版本，旧会话不能覆盖后来已经更新的数据。
- `@bot /取消导入`：清除本次上传或更新的暂存，终止正在进行的解析，**不删除已入库课表**。在开始提交数据库前可取消或重开；数据库提交期间会明确提示“正在提交数据库”，暂不允许取消、重开或重复提交。已开始的事务会原子完成，不因上传会话此时到期而中断。
- `@bot /课ing`：以图片卡片列出**当前仍在本群且已导入课表的成员**，优先显示群名片，其次 QQ 昵称；均为空时显示 `QQ 号码`，同名成员附 QQ 号区分，不逐个 @打扰。显示正在进行的课程名、开始/结束时间及可选地点；重叠课程全部显示，无课成员显示“无课程”。不显示不在当前群的成员；隔离关闭时，允许查询本群成员在其他群导入的课表。当前成员均无可用课表时给出导入指引。发送者自己未导入，也可以使用此群内查询。
- `@bot /今日课程`：真实 @命令发送者，以个人时间轴图片显示他本人的今日课表，按时间排序；隔离开启时用本群记录，关闭时用跨群共享记录。额外 @他人不会改变查询对象。未导入时提醒导入；已导入但今天无课时回复“今日无课程”。
- `@bot /明日课程`：以图片展示发送者自己明天的课表，并 @本人；按北京时间确定明天，其余权限、群聊隔离/共享、跨日课程筛选和文字回退条件与 `/今日课程` 相同。
- `@bot /课表`：查看以上操作说明。

时间与输出规则：

- `/课ing` 每次通过 `get_group_member_list` 获取当前群成员和昵称，不缓存名单、不从消息中的额外 @推断成员。成员名单在数据库查询限额之前过滤；获取失败、名单异常或超过 10,000 人时明确报错，**不回退展示数据库中的全部成员**。`/今日课程` 不依赖此接口，仍可查询本人。退群仅影响展示，不自动删除课表；重新入群后可继续使用原记录。
- 统一按 **Asia/Shanghai（北京时间）** 查询和展示。当前课程使用 `开始时间 <= 当前时间 < 结束时间`，下课时刻不再视为正在上课。
- 今日课程使用北京时间当日 `00:00` 至次日 `00:00`，列出与今天有交集的课程。跨午夜课程会标出起止日期；恰在今日零点结束、或恰在明日零点开始的课程不计入今天。
- 单次数据库查询最多 1,000 行。图片较长时自动分页，先检查完整布局，最多 8 页；超过图片限额会改用文字。文字回复按行拆分，每条正文约 2,800 字符，最多 8 条；完整文字仍超限时明确拒绝，不静默遗漏成员或课程。
- 所有回复都发到当前群，**即使只 @本人也不是私聊**。课程名、地点、文件名和成员昵称只作为图片中的文字或 OneBot 文本消息段输出；其中的 CQ 码样式文字不会生成额外 @或动作。真正的 @发送者消息段在图片外，图片内昵称不冒充 @。


#### 可选的群聊隔离

在宿主机打开 WebUI，在“课表”下设置 **“启用群聊隔离”**，再点击 **“保存并应用”**。仅“保存配置”不会改变正在运行的 bot；`/课表` 和 `/help 课表` 会显示当前已生效的模式。

- **勾选（默认）**：沿用原有行为，同一 QQ 在各群分别导入、查询和更新。旧配置没有此选项时仍默认隔离。
- **取消勾选**：同一 QQ 只需在一个群完成正式导入，就能在其他群使用 `/今日课程`，也能出现在那些群的 `/课ing` 中，无需重复导入。`/更新课表` 可在任意群发起；再次 `/导入课表` 会提示已有课表并引导更新。
- **隐私变化**：关闭后，其他共同群的成员可通过 `/课ing` 查阅该成员的课程安排。名单仍以每次从 NapCat 获取的当前群成员为准，不会展示非本群成员；个人查询仍固定为命令发送者，额外 @他人不能查询他人的今日课表。
- **已有多份课表**：同一 QQ 历史上在多群导入过不同课表时，选最近更新的一份，不拼接、复制或删除其他记录。时间精度为秒，更新时间相同则依次按导入时间、群号作固定倒序选择，保证各群查询一致。
- **共享更新与恢复隔离**：共享模式更新只替换当前选中的那份记录，保留它原本所属的群；其他群的历史记录不变。重新勾选后恢复按原归属群查询，没有本群记录的成员需在该群导入。切换本身不迁移或改写数据库，也不要求重传已有 ICS。
- **上传会话始终隔离**：无论是否共享，开始接收、上传文件、`/已导入` 和 `/取消导入` 都必须由同一 QQ 在同一群完成。多个群同时更新同一份共享课表时，旧会话不能覆盖已提交的新版本。

#### Pillow 图片课表（内存发送）

- 使用白底、青绿色标题、紫色上课状态的固定设计：圆形头像、群名片/昵称、课程名、起止时间、可选地点。`/课ing` 展示正在上课和无课成员；`/今日课程` 分别标记“已结束 / 上课中 / 未开始”，上课中显示进度条及剩余时间。重叠课程逐条展示，长名称/地点自动换行，不能完整放下时回退文字，不直接截掉课程。
- **每次指令实时查询和绘图，不复用旧课表图片。** 在排队和头像等待后重新读取北京时间和数据库；例如 14:30 开课、15:55 下课会使用当次查询的状态。各页保持同一个查询快照，页脚标注查询时间和页码。群里已发出的图片是静态消息，不会自动变化；没有定时推送，也不会修改或撤回旧 QQ 消息。
- **方案一：完全在内存中绘制和发送。** `Pillow → BytesIO → Base64 → OneBot image`；逐页生成，关闭绘图对象和编码缓冲区，发送后不保存 PNG/Base64 缓存。因此 bot 不会积累“旧课表图片”，不需要文件清理定时任务或共享图片目录。Python 可能保留已分配内存供复用，不代表仍保存旧图片。
- **这不控制 QQ/NapCat 自身的缓存和群消息。** 它们仍可能保存接收/发送图片；此实现不会越界删除它们的缓存、用户文件、原始 ICS 或课表数据库。`tmp pillow/` 仅用于人工样稿/效果验收，正式代码不会读取其中的样本，也不会向其中写图；该目录已从 Git 和 Docker 构建上下文排除，原有文件不删除。
- 头像只按合法 QQ 号从固定 HTTPS 域名 `q1.qlogo.cn` 请求，不使用昵称或 ICS 中的地址，不跟随重定向。每个响应最多 512 KiB、原图最多 200 万像素，归一化为 160×160 PNG；下载/解码失败或等待预算耗尽时使用默认头像，不影响课程显示。
- 头像仅有**内存 LRU 缓存**：最多 128 个、每个最多 96 KiB，有效期 6 小时；失败缓存 60 秒。每个查询最多等待头像 1.2 秒，最多 4 个下载线程、8 个在途下载；重启后缓存清空。昵称、成员名单、查询结果和生成图片不作跨请求缓存。
- 绘图和 Base64 编码在专用线程池中执行，不堵塞 WebSocket 事件循环。最多 2 个活动图片请求、合计 8 个活动/排队请求，同一群/发送者/命令不重复占用图片队列；队列等待 2 秒、单次绘图工作 4 秒超时。超时或取消不等于底层线程已经终止，只有线程实际结束才释放绘图槽位，避免不停叠加线程和图片内存。
- 单次布局最多 512 条课程/无课卡片；输出宽 1560 像素、高最多 2700 像素，单页最多 450 万像素、PNG 最多 2 MiB（Base64 约增大三分之一）。超限、服务繁忙、Pillow/字体缺失或绘图出错时使用现有文字回复。
- 图片调用 `send_msg` 并等待回执（包含发送过程最多 12 秒）。**明确被 OneBot 拒绝**时改用完整文字课表；已有部分页确认发送时会注明页数再给出完整文字，避免遗漏。**超时、断连、仅异步受理或缺少有效回执**不代表图片未发出：停止后续页，只尝试发送一条“结果未能确认”的提示（最多等待 3 秒），不自动重发图片或完整课表；请先查看群消息再决定是否重查。
- 默认 Docker 镜像安装 `Pillow==12.3.0` 和 Alpine `font-noto-cjk`。Windows 本地开发可使用系统微软雅黑；其他 Linux 本地环境请安装 Noto Sans CJK。也可通过进程环境变量 `TIMETABLE_FONT_REGULAR` / `TIMETABLE_FONT_BOLD` 指定可用的字体文件；缺字用占位字符，缺少整套中文字体则安全回退文字。不要将 Windows 字体复制进发布镜像。

Pillow 展示本身只改变绘图与发送流程，**不修改数据库结构，也不要求重新上传已入库的 ICS**；课表读取/更新范围由上述群聊隔离开关决定。绘图本身的耗时与头像获取、群成员接口和 QQ 图片发送耗时不同；网络慢时优先使用默认头像，不能把本地绘图耗时当成群内送达保证。

#### ICS 解析能力与限制

- 只支持 UTF-8 编码（允许 BOM）的 `.ics` / `VERSION:2.0`，**不支持 `.xls`、`.xlsx` 等 Excel 文件**；改后缀不能转换文件格式。
- 读取 `VEVENT` 的 `UID`、`SUMMARY`、`DTSTART`、`DTEND`（或 `DURATION`）和可选 `LOCATION`；处理折行和文本转义。不读取描述里的学生身份作为导入身份，也不把描述、个人元数据写入课程结果或日志。
- 支持 UTC（`Z`）和可识别的 IANA `TZID`；未标注时区的浮动时间按北京时间处理，并在导入回执中提醒。IANA 时区以时区数据库为准，不采用自定义 `VTIMEZONE` 规则；未知时区不会静默当作北京时间。
- 支持 `RRULE` 的 `DAILY`、`WEEKLY`、`MONTHLY`、`YEARLY`，以及合法组合的 `INTERVAL`、`BYDAY`、`BYMONTHDAY`、`BYMONTH`、`BYSETPOS`、`WKST`。**每条规则必须且只能有 `COUNT` 或 `UNTIL`**，不接受无限重复。带时区的 `DTSTART` 必须使用 UTC `UNTIL`；浮动 `DTSTART` 使用浮动 `UNTIL`。
- 支持 `RDATE` 补课日期、`EXDATE` 停课日期，以及同一 `UID` 下 `RECURRENCE-ID` 的**单次改期/取消**。保留改期前的原始实例标识；相同时间不同 UID 的课程不会被合并。重复 UID 主事件、多个同次改期、未匹配的改期、同次同时排除与改期/取消等冲突会明确拒绝。
- 暂不支持全天事件、`RDATE;VALUE=PERIOD`、`EXRULE`、`RECURRENCE-ID;RANGE` 批量改期、按秒/分钟/小时重复或其他未列出的重复字段。邀请/整份取消等非 `PUBLISH` 日历也会拒绝；请导出完整课表。单次课程须大于 0 且小于 24 小时，允许跨午夜。
- 单文件最多 **512 个 VEVENT**，完整展开最多 **10,000 次课程**，日期范围及重复搜索窗口最多 **730 天**；不符合或超限时整份失败，**不会静默截断或跳过错误课程**。
- ICS 在独立进程中解析，超时 **10 秒**即终止；最多 2 个并行解析、合计 8 个在途解析，已解析课程暂存总量最多 20,000 次。Linux 解析子进程另有 256 MiB 地址空间限制，并限制输入结构及输出大小，避免拖住 WebSocket 和其他指令。
- 内容解析失败时缓存安全错误提示，不反复解析同一份坏文件；按提示重新 `/导入课表` 或 `/更新课表`，上传修正文件后再确认。解析排队、暂存空间或数据库繁忙时可稍后再次确认；数据库写入失败时，在会话有效期内复用已经解析的结果，无需重传。

#### 接收、数据持久化与隐私

- 上传会话固定为事件中的 `(群号, 发送者 QQ)`，不接受替其他成员导入、确认、更新或查询其今日课表。已入库课表默认按群隔离，只有管理者关闭“启用群聊隔离”后才按 QQ 共享；未开启本群会话时忽略上传，不调用下载接口。
- 会话从开启时起有效 **10 分钟**，重复确认不会延长。每个会话只保留第一个通过基础检查的文件。更换文件需再次使用相同的 `/导入课表` 或 `/更新课表` 清空暂存后上传。
- 单文件最大 **10 MiB**，下载时检查实际字节数；最多 128 个有效会话、3 个并发下载、16 个待处理文件（每会话最多 4 个），已接收原始文件暂存总量最多 64 MiB。
- 原始 ICS 只暂存在内存，**成功、到期、取消或重启后释放**，不另存文件副本。数据库只保留群号、QQ、源文件名/哈希、导入计数/时间/版本，以及课程 UID、原始实例标识、名称、起止时间和地点。成员昵称和群成员名单不写入数据库。群文件仍由 QQ 保存，取消接收不会删除 QQ 群文件。
- SQLite 使用事务、外键、时间索引和 WAL；数据库操作在有界单线程工作队列执行，最多 32 个在途操作，不在 WebSocket 事件循环上执行 SQL。每次替换要么完整提交，要么完整回滚。
- **Docker Compose 挂载命名卷 `timetable-data` 到 `/app/data`**（实际卷名带 Compose 项目前缀），数据库位于 `/app/data/timetable.sqlite3`。重新构建镜像、重启或重新创建 `qq-bot` 容器不会清除已导入课表；上传中的临时会话会失效。源码直接运行默认使用项目内 `data/timetable.sqlite3`，可通过进程环境变量 `TIMETABLE_DB_PATH` 修改。
- `data/`、`backups/` 和 `*.sqlite3*` 已从 Git 与 Docker 构建上下文排除。数据库包含课程安排和 QQ 关联信息，应限制文件/数据卷访问权限，不要公开或提交。**删除数据卷（例如执行 `docker compose down -v`）会丢失课表**；备份请使用 SQLite 在线 `backup()`，或停止 bot 后备份完整数据卷，不要在运行时只复制主库而漏掉 WAL。
- 同时适配群上传通知和群文件消息段（含 CQ 字符串）；文件 ID 去重加 SHA-256 内容去重。不同 ID 可能需要各下载一次，不能只按“同名、同大小”判重。
- 下载通过 NapCat 的 `get_group_file_url` 获取 HTTP(S) 地址，不使用文件消息中的任意 URL；不执行或访问 ICS 内部的链接、附件或提醒。下载及基础检查在线程中进行。
- 下载失败可在有效会话内重新上传；取消、到期、重开或停止后，旧下载/解析结果不会写入新会话或触发新的入库。停止时已提交到数据库工作队列的事务允许完成，但不会再发送迟到回执。

已有显式 `enabled_plugins` 列表的配置不会自动启用新插件，需要加入 `timetable` 或在控制台勾选后重新创建 bot 容器。真实群上传还要求 NapCat 正常上报群文件事件，且 `get_group_file_url` 可用；接收流程已按 NapCat 4.18.6 的字段格式适配。**仅从阶段二预览版升级**的用户需重新 `/导入课表`、上传 ICS 并 `/已导入` 一次；阶段三及以后已经正式入库的课表可直接继续使用，本次升级无需重新导入，也不改变数据库结构。

#### 课表在线备份（Docker / PowerShell）

本节是原有课表功能的**可选手工运维参考**，不属于本次 WebUI 安装或验收流程；本次不执行备份，也未新增自动备份功能，现有备份保留。若以后另行决定执行手工备份，下面使用 SQLite `backup()` 生成包含已提交 WAL 数据的一致性快照，再复制到宿主机的 `backups/`；不会停止 bot、改写正式课表或读取群文件。每次生成不同文件名，并拒绝覆盖同名快照。

在项目目录运行：

```powershell
$backupName = "timetable-" + (Get-Date -Format "yyyyMMdd-HHmmss-fff") + ".sqlite3"
$backupScript = @'
import sqlite3, sys
from contextlib import closing
from pathlib import Path
source = Path("/app/data/timetable.sqlite3")
target = Path("/app/data/backups") / sys.argv[1]
if not source.is_file():
    raise SystemExit("No timetable database exists yet")
target.parent.mkdir(parents=True, exist_ok=True)
with target.open("xb"):
    pass
with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
    with closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise SystemExit("Backup check failed; do not use this snapshot")
print("Backup checked:", target)
'@
$backupScript | docker compose exec -T qq-bot python - $backupName
if ($LASTEXITCODE -ne 0) { throw "Backup failed; no copy was made" }
New-Item -ItemType Directory -Force -Path ".\backups" | Out-Null
$destination = Join-Path ".\backups" $backupName
if (Test-Path -LiteralPath $destination) { throw "Backup destination already exists" }
docker compose cp ("qq-bot:/app/data/backups/" + $backupName) $destination
if ($LASTEXITCODE -ne 0) { throw "Copy failed; the checked snapshot is still in the data volume" }
```

- 备份含课程安排和 QQ 关联信息，和正式库一样需要限制访问；不要上传到公共仓库或聊天中。上述目录已被 Git / Docker 忽略；只保留在同一数据卷内的副本不能防范删卷，因此应确认宿主机复制成功，并按自己的策略留存。
- 这是课表数据库备份，不包含 `.env` 或插件配置；配置文件也应单独安全保管。恢复必须先停止 `qq-bot`、保留当前数据库及 WAL/SHM 文件，再离线恢复，不能在线覆盖数据库或以清空数据卷代替恢复。
- 仅更新 bot 可执行 `docker compose up -d --no-deps --build --force-recreate qq-bot`；不要使用 `down -v`。Compose 项目名称决定命名卷前缀，换目录或改项目名时应先核对实际挂载的原数据卷。

#### 课表故障排查与群内验收

| 现象 | 检查与处理 |
|---|---|
| `/课表` 或 `/help` 中没有课表命令 | 检查 `timetable` 开关、真正的 @消息段；保存开关后重建 `qq-bot`，不要重置其他插件配置。`basic` 关闭时 `/help` 本身也不可用。 |
| 上传文件没有回执 | 先开启会话；上传者和群必须与命令一致，会话不能超过 10 分钟；检查 NapCat 群文件事件及 `get_group_file_url`。 |
| 导入/更新被拒绝 | 仅支持 UTF-8 ICS；按回执修正格式、重复规则、事件数或大小限制。Excel 改后缀不可用，失败不会覆盖旧课表。 |
| `/课ing` 提示获取成员列表失败 | 检查 NapCat 连接、`get_group_member_list` 动作及群权限；可先用 `/今日课程`。为避免泄露退群成员数据，不会用旧名单兜底。 |
| 某成员没有出现在 `/课ing` | 只有当前成员且已正式导入才显示；隔离开启时需本群记录，关闭时可用同一 QQ 在其他群的记录。退群或仅完成上传/预览不会显示。 |
| 关闭隔离后仍提示未导入 | 检查是否点击“保存并应用”且重建成功；用 `/课表` 确认已生效的模式，并核对发送者是否同一 QQ、原课表是否已完成正式入库。 |
| 显示“无课程”或“今日无课程” | 核对北京时间、ICS 有效日期、停课/改期及上课时段；“未导入”是不同状态。 |
| 图片降级为文字 | 先看提示和脱敏日志中的错误类型；确认 Pillow、中文字体已安装，未达到布局/页数限制，绘图队列不繁忙。更新 Dockerfile 后必须重新构建镜像，仅重启旧容器不会安装新依赖。 |
| 头像显示默认图标 | QQ 头像接口超时、限额、响应格式或下载失败均可触发，不影响课表数据。缓存到期后或稍后新查询可重新尝试。 |
| 提示图片发送结果未能确认 | 可能实际已经发出；先检查群消息再重查，不应立即重复发送指令。检查 NapCat 的 `send_msg` 回执和连接，bot 不盲目重发。 |
| 数据库暂时不可用或繁忙 | 检查数据目录写权限、磁盘空间和占用；保留原库，稍后重试，不要为排错清库。 |
| 容器重建后看不到旧课表 | 检查 `/app/data` 的实际命名卷以及 Compose 项目名是否改变；不要删卷或往新的空库重复导入来掩盖挂载问题。 |

群内验收由操作者自行发送真实 @指令：检查 `/help` / `/课表`、本人导入与更新、`/课ing` 的群名片及无课状态、`/今日课程` 只 @本人；更新失败或取消后确认旧课表仍可查询。若测试昵称变更或退群过滤，只使用自愿参与的测试成员。图片升级后还应检查：两条查询均能显示图片和真实 @；14:30/15:55 等边界后的新查询更新状态；长课名、无课、重叠课程及多页没有遗漏；头像失败仍能展示；连续查询不会在 bot 目录生成图片。隔离开关可由自愿测试成员验收：在 A 群导入后，关闭隔离应能在 B 群查询；B 群共享更新后 A 群读到新内容；重新开启隔离后按原归属群查询，非 B 群成员始终不应出现在 B 群的 `/课ing` 中。群内消息与 NapCat 自身缓存可能保留图片，不属于 bot 输出目录。自动化测试不会代发群消息，也不会读取真实群文件。

### 自动贴表情

启用 `auto_emoji` 并设置目标 QQ 后，bot 会在能接收到该成员发言的各群中，对其带 `message_id` 的消息依次调用 NapCat 的 `set_msg_emoji_like`。不处理私聊，也不需要指令或 @。

日常使用推荐在 WebUI 中直接粘贴 😀、😂、👍、❤️、🔥、🍬、㊗️ 等表情，点击“解析并添加”，不需要查询编码。支持单码点 Unicode Emoji（允许末尾的 emoji 样式选择符），不再限定两种表情；是否被当前 QQ／NapCat 实际接受仍需人工验收。以下 ID 说明用于理解旧配置和环境变量，不是页面输入要求。

表情 ID 支持用英文/中文逗号、分号或空白分隔，并自动去重。新配置建议使用 `AUTO_EMOJI_IDS` 或插件设置中的 `emoji_ids`；插件设置同时兼容旧的 `emoji_id` 字段。能否成功贴表情取决于 NapCat 接口和具体表情 ID 的支持情况。

### 群聊总结

```text
@bot /总结
@bot /总结 300
@bot /总结 条数100
```

省略条数时读取最近 100 条，最多读取 500 条。通过 NapCat 的 `get_group_msg_history` 分批获取并去重，实际条数可能受可获取的历史记录限制。

bot 会整理成员显示名和消息的文本表示、过滤总结指令，将每条文本截断至最多 500 个字符后发给 DeepSeek。图片、语音和视频不会被识别其内容；消息中可能保留占位符或 CQ 码。结果是模型生成的中文摘要，不保证完全准确。

需配置有效的 `DEEPSEEK_API_KEY` 或插件设置中的 `deepseek_api_key`；未配置或接口失败时会提示错误，不提供离线总结兜底。**调用会将群聊内容发送到配置的 API 服务，并可能产生费用**，启用前应确认群成员知情及服务使用规则。当前没有单独的调用者权限校验或频率限制。

### 猜拳时长与权限

在 WebUI“猜拳 → 配置插件”中设置输家禁言时长，默认 60 秒，可填秒或分钟，换算后必须为 1～86400 整数秒。点击完成后仍需“保存配置”或“保存并应用”；关闭主插件会保留时长。首次部署新增猜拳代码需要构建镜像，后续只改时长无需构建。

`/猜拳` 仅限群聊，不能和自己猜拳。禁言通过 OneBot 的 `set_group_ban` 实现，需要 bot 拥有相应群管理权限，且目标不是 bot 无权禁言的成员。发送了猜拳结果不代表 NapCat 的禁言动作一定成功。

### 今日群友与结芬

`@bot /今日群友`（也支持 `@bot /今日老婆`）会通过 `get_group_member_list` 获取群成员，排除抽取者自己后随机选择一人，并发送头像和 @ 结果。

同一成员在同一群内每天只能抽取一次；再次查询会返回当天已抽到的成员。日期按中国时区（UTC+8）判断，次日可重新抽取，记录仅保存在内存中。

结芬是 `daily_wife` 的可选子功能，默认开启。可以在 WebUI“今日群友”下取消勾选“启用结芬子功能”，再点击“保存并应用”。关闭后仍可抽取或查询今日群友，但抽取结果和 `/help` 不再显示结芬提示，`/结芬` 以及 `/愿意`、`/不愿意` 等回应指令不再由此插件处理；求偶插件不受影响。

开启时，抽取后输入 `@bot /结芬`，对方需在 2 分钟内回复 `@bot /愿意` 或 `@bot /不愿意`。不愿意或超时回复 `雑魚~雑魚~，被甩了捏~`；愿意回复 `正在制作结婚证（其实是生图ai没钱弄）`，**目前不会实际生成结婚证图片**。同一成员在同一群内每天只能发起一次结芬，完成后重复请求返回当次结果。

### 求偶

`@bot /求偶 @成员` 会向对方发起请求，不能向自己或当前机器人求偶。

- 对方回复 `@bot /接受求偶`：@ 发起者并回复 `接下来的内容需要付给观看`。
- 对方回复 `@bot /拒绝求偶`：@ 发起者并回复 `啊，哦，看来你只能自己解决了呢~`。

请求按群和成员保存在内存中，当前没有自动超时或取消指令，需等待回应或重启清空。建议使用上述完整回应指令；与结芬子功能同时启用时，通用别名 `/接受`、`/拒绝` 会优先命中结芬；关闭结芬后，这些别名可正常用于求偶。

### 小说链接

`/novel` 从 [Linovelib](https://www.linovelib.com) 查询目录，只返回目录、章节或卷的阅读链接，不抓取正文。查询成功后，可以继续输入 `@bot /第1章`、`@bot /第一章` 或 `@bot /第1卷`。章节序号按全书目录顺序累计，而不是每卷重新计数。

小说会话仅按 QQ 号保存，不区分群聊；同一成员在其他群重新搜索也会覆盖上次查询，重启后清空。若站内搜索不可用，会尝试从首页公开链接匹配；也可以直接提供小说页链接：

```text
@bot /novel https://www.linovelib.com/novel/2139.html
```

### 歌曲搜索与下载

`@bot /song 歌曲名称` 从 [下歌吧](https://xiageba.liumingye.cn/) 搜索并列出最多 10 首候选，包含歌名、歌手和专辑。当前成员随后输入 `@bot /song 2` 或 `@bot /song 选择 2`，bot 才会读取详情、下载可用音频、转换为 MP3 并发送。

- `@bot /song 取消` 或 `@bot /song cancel` 可取消候选；默认有效期 10 分钟，选择后消耗该候选会话。
- 候选按群聊 / 私聊范围和用户区分，仅保存在内存中，重启后清空。
- `SONG_MAX_DOWNLOAD_MB` 限制源文件大小，默认 80 MB；使用本地 FFmpeg 转为 MP3。
- 文件通过 NapCat 的 `upload_group_file` 或 `upload_private_file` 以 `base64://` 上传，不是 QQ 音乐卡片。发送动作后清理本地临时文件，无需在 bot 和 NapCat 之间共享下载目录。
- 站点接口、分享有效期、文件大小及 NapCat 文件上传能力都可能影响可用性；未找到时回复 `私密马赛，没有找到喵~`。

如果搜索结果中的下载源是夸克分享页，需要配置 `QUARK_COOKIE`。bot 会优先选择分享顶层列表中的 MP3（也支持其他常见音频格式），临时转存到账号根目录下的 `qqbot-song-*` 文件夹，获取直链并下载，最后尝试删除该临时文件夹。当前不支持带提取码的分享或递归查找子文件夹；清理失败时日志会给出警告，可能需要手动清理。

若只有夸克分享页且未配置 Cookie，或只有百度/阿里等不支持自动下载的网盘分享页，bot 会返回原始链接。Cookie 失效或下载失败则提示失败，需要修复配置后重新搜索。

## 本地开发与测试

先创建项目内虚拟环境并安装依赖（PowerShell；无需激活环境）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements/bot.txt
```

直接运行 bot 时，程序**不会自动读取 `.env`**，需先将配置导出为进程环境变量。例如仅监听本机的反向 WebSocket：

```powershell
$env:WEBSOCKET_MODE = "server"
$env:LISTEN_HOST = "127.0.0.1"
$env:LISTEN_PORT = "8080"
.\.venv\Scripts\python.exe bot.py
```

在此示例中，本机 NapCat 可连接 `ws://127.0.0.1:8080`；跨容器或跨机器连接需调整监听地址。若没有设置任何环境变量，源码默认是 `client` 模式，连接 `ws://napcat:3001`，与 Compose 默认值不同。需要歌曲下载时，宿主机还须安装 FFmpeg 并将其加入 `PATH`。

全量回归还包含独立 WebUI，请先按上文“隔离测试与迁移状态”安装 `.venv-webui` 中的测试依赖。安装依赖和中文字体后，运行测试无需 NapCat、API Key 或第三方测试框架：

```powershell
.\.venv-webui\Scripts\python.exe -B -m unittest discover -s tests -v
```

测试使用模拟接口及本机回环 HTTP 服务，覆盖配置读写、指令解析、自动表情、群聊总结、结芬/求偶、歌曲/小说解析、分层帮助及卡片发送/超时回退，以及课表文件接收、双事件去重、格式和大小拒绝、ICS 时区/重复/改期/排除规则、解析限额和进程终止、会话隔离、过期、取消、并发、SQLite 持久化与更新回滚、课程查询时间边界、当前成员过滤与昵称刷新、成员接口失败的安全处理、SQLite 在线备份及 WebSocket 动作响应等逻辑，不访问真实群聊或外部 API，也不代替真实 NapCat、外部网站、DeepSeek 和 FFmpeg 的联调。ICS 自动化测试使用脱敏合成数据和临时数据库，不读取个人下载目录中的原始样本，也不会写入运行中的正式课表库。图片测试还覆盖内存 PNG/Base64、绘图布局/分页/大小限额、14:30/15:55 与跨午夜更新、头像异常及缓存淘汰、线程真实结束前不释放限额、取消/关闭后不迟发、图片回执确认与不盲重发、数据库更新后立即使用新内容。头像 HTTPS 完全模拟，不下载真实 QQ 头像。群聊隔离测试覆盖默认开关、WebUI 配置保存与禁用状态、按 QQ 跨群查询、历史多份选择、共享更新/并发冲突、恢复隔离和当前群成员过滤。

添加新插件时，需要在 `plugin_control.py` 注册定义、在 `bot.py` 的工厂映射中注册实现，并按需补充 `plugins/help_menu.py` 的帮助指令、分类标题/别名和测试；卡片与文字共用这些数据。仅放入 `plugins/` 目录不会被自动发现。插件的可选 `handle_event()` 只接收消息事件，`handle_notice()` 接收通知事件；需要清理暂存或定时器时可实现同步 `close()`，bot 停止时会调用。

## 配置安全与排查提示

- `.env` 和 `plugin_config.json` 均被 Git 忽略，但这不等于加密。不要分享包含 token、Cookie 或 API Key 的文件、截图和日志。
- 新构建镜像不再复制 `plugin_config.json`，由 Compose 在运行时只读挂载，`.env`、课表库和备份也从构建上下文排除。旧镜像/旧构建缓存可能仍包含以前复制的私密配置，不要对外分享。单独 `docker run` 时需自行挂载配置并传入环境变量；未提供插件配置会使用默认启用列表，不会自动继承宿主机配置。
- 遇到 `network qqbot-net declared as external, but could not be found`，先创建外部网络；NapCat 连接不到 `my-bot` 时，检查是否在同一网络，以及是否误把容器别名用于宿主机连接。
- 已连接但不响应时，检查 `BOT_QQ`、真正的 @ 消息段、插件是否启用，以及配置修改后是否重新创建了容器。关闭 `basic` 会同时关闭 `/help` 和 `/hello`。
- 第三方网站搜索失败、DeepSeek 报错或 OneBot 动作失败时，先查看 `docker compose logs -f qq-bot`，分别检查网络、凭据、接口支持和群权限；避免公开未经脱敏的错误内容。
