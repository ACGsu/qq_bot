# NapCat QQ Bot

基于原生 NoneBot2 和 OneBot v11 Adapter 的 QQ bot，通过 NapCat 的反向 WebSocket 连接收发消息，使用 Docker Compose 部署 NapCat、机器人和独立 WebUI。

宿主机直接运行需要 Python 3.11 或更新版本，镜像使用摘要锁定的 Python 3.11。`qqbot/requirements/*.txt` 是维护输入，Dockerfile 实际通过带精确版本和 SHA-256 的 `*.lock` 安装依赖。图片课表使用 Pillow 和 Noto CJK 字体，不需要浏览器；歌曲功能直接下载并转发源文件，不进行本地转码。basic、rps、auto_emoji、daily_wife、courtship、novel、song、timetable 均已原生化。

## 文档导航与当前状态

- **日常使用**：本文的“插件配置与控制台”及功能说明——配置插件、保存应用和常见问题。
- **Docker 全新部署**：[Docker部署说明.md](Docker部署说明.md)——三服务镜像构建、容器创建、启动与目录挂载。
- **技术参考**：本文记录当前原生 NoneBot 运行骨架、业务迁移边界与部署方式；详细计划见 [NoneBot重构计划.md](NoneBot重构计划.md)。
- **依赖与环境要求**：[qqbot/requirements/README.md](qqbot/requirements/README.md)——Python、系统依赖、安装方式和配置文件位置。

WebUI 是当前唯一的管理界面入口，可独立运行于宿主机或 Docker。2026-10-26 已完成阶段 0～12：340 个历史测试已完成映射和清理，配置、帮助、OneBot 公共工具以及课表工作流／解析／渲染／存储已按 `services/`、`infrastructure/` 边界拆分，三个环境均使用哈希锁文件；标准 `nonebot-plugin-apscheduler 0.5.0` 通过官方 `nonebot.load_plugin` 加载并出现在 management 状态中。

阶段 12 已完成自动核验和人工群聊验收：仅重建 `qq-bot` 前后，正式课表库均为 schema 1、完整性 `ok`、1 条导入和 102 条课程实例，脱敏逻辑 SHA-256 保持一致；歌曲缓存保持 3 个文件、86,885,498 字节。NapCat 重新登录后 OneBot 已恢复连接，用户已在授权群确认既有课表可通过 `/课ing`、`/今日课程` 查询；整个过程没有清空、复制或公开正式数据。

## 功能与指令

普通指令必须放在真正的 `@bot` 消息段后面，直接输入文字 `@bot` 不等同于 QQ 的 @ 操作。程序同时支持 OneBot 消息段数组和 CQ 码字符串；私聊也会检查 @，不会直接响应未带 @ 的裸指令。

下表列出项目支持的功能，实际可用项取决于插件配置。`basic` 插件启用时，群内 `@bot /help` 以可点击的分类卡片展示当前已启用插件的指令；`@bot /help 文本` 保留完整文字列表。

| 插件 ID | 指令或触发方式 | 行为 |
| --- | --- | --- |
| `basic` | `@bot /help`、`@bot /help 文本`、`@bot /help 分类名`、`@bot /hello` | 群内分层卡片导航、文字帮助；回复 `啦啦啦` |
| `rps` | `@bot /猜拳 @成员` | 群内随机猜拳，输家按配置时长禁言（默认 60 秒），平局不禁言 |
| `daily_wife` | `@bot /今日群友`（或 `/今日老婆`）、`@bot /结芬`、`@bot /愿意`、`@bot /不愿意` | 群内每日抽取群友，结芬子功能可独立开关 |
| `courtship` | `@bot /求偶 @成员`、`@bot /接受求偶`、`@bot /拒绝求偶` | 群内求偶及回应 |
| `song` | `@bot /song 歌曲名`、`@bot /song 序号`、`@bot /song 取消` | 搜索候选、下载并发送 MP3、FLAC 或 WAV 源文件 |
| `novel` | `@bot /novel 小说名`、`@bot /第3章`、`@bot /第2卷` | 查询轻小说目录及阅读链接 |
| `timetable` | `@bot /课表`、`@bot /导入课表`、`@bot /已导入`、`@bot /更新课表`、`@bot /删除课表`、`@bot /课ing`、`@bot /今日课程`、`@bot /明日课程`、`@bot /取消导入` | 导入、更新或删除本人的 ICS 课表并持久保存；以图片展示本群当前课程或本人今日课表，失败时文字回退；不支持 Excel |
| `auto_emoji` | 指定 QQ 号在群内发言 | 自动贴配置的表情，属于被动监听，**不需要 @bot** |

猜拳目标不能是发起者或机器人。若随机输家是群主／管理员，或机器人没有足够权限，机器人仍会公布猜拳结果并说明未执行禁言，不会因 OneBot 拒绝禁言而中断。

当前正式原生插件覆盖 basic、rps、auto_emoji、daily_wife、courtship、novel、song、timetable。当前配置未启用 courtship，其余已配置插件均已加载。

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
│  ├─ /删除课表
│  ├─ /课ing
│  ├─ /今日课程
│  ├─ /明日课程
│  └─ /取消导入
└─ 基础指令等其他已启用分类（点击进入）
```

- 点击只是在 QQ 客户端中浏览帮助，**不会自动执行或发送业务指令**。执行指令仍需回到聊天中真正 `@bot` 后输入；实际课表导入指令仍为 `/导入课表`，没有重命名。
- `@bot /help 文本` 或 `@bot /help 全部`：查看全部文字帮助；也接受 `text` / `all`。
- `@bot /help 课表`：只查看课表分类的文字帮助。也支持「今日老婆 / 今日群友」「基础 / 基础指令」「歌曲 / 点歌 / 歌曲下载」「小说 / 小说链接」、其他分类标题及插件 ID（例如 `timetable`）。未知或未启用分类会提示当前可用分类。
- 私聊中的 `/help` 保留文字帮助，不发送群导航卡片；仍遵守真正 `@bot` 的要求。
- 卡片、分类文字和完整文字列表共用帮助数据：隐藏关闭的插件；关闭 `daily_wife.marriage_enabled` 时，也隐藏结芬及其回应指令。被动的自动贴表情不作为指令分类。
- 每次请求重新生成卡片，不缓存资源 ID。已有卡片是生成时的快照，不能随配置改变；在 WebUI 保存并应用配置或完成容器重新创建后，请重新发送 `@bot /help` 获取新卡片。
- 通过现有 OneBot WebSocket 调用 `send_group_forward_msg`，不需要新增 WebUI 凭据、端口或外部链接。所有帮助节点以 bot 自己的 QQ 号生成，不引用群成员的聊天记录或身份。
- 接口失败、30 秒内没有确认，或回执缺少有效 `message_id` 时，回退一次文字帮助，不重试发送卡片。超时并不证明卡片没有发出，因此偶尔可能同时看到卡片和回退文字；如果客户端无法打开卡片，也可主动使用 `/help 文本`。

发送成功以 OneBot 发送回执为准，不额外回读 `get_forward_msg`。本环境的客户端验证表明，嵌套节点在该接口中回读为空，并不代表客户端无法显示或点击。

## 项目结构

```text
D:/qq_bot/
├── compose.yaml / docker-compose.yml  # 主入口及旧文件名兼容入口
├── compose.test.yaml                  # 独立测试入口
├── .env / .env.example                # 本机配置及无秘密值示例
├── napcat/                            # config、qq 登录状态、NapCat 插件
└── qqbot/
    ├── bot.py / run_webui.py
    ├── Dockerfile / Dockerfile.webui / .dockerignore
    ├── Dockerfile.test / Dockerfile.test.dockerignore
    ├── pyproject.toml                 # 版本、插件声明和 pytest 配置
    ├── plugins/ / services/ / infrastructure/
    ├── webui/ / tests/ / assets/ / requirements/
    ├── config/                        # 私密配置及可提交的 *.example* 示例
    └── storage/                       # db、downloads、cache、backups、webui
```

Docker 运行不依赖宿主机虚拟环境。`services/plugin_catalog.py` 和 `infrastructure/plugin_config.py` 分别承担无 I/O 插件目录与配置持久化；帮助服务位于 `services/help.py`，OneBot 公共消息工具位于 `infrastructure/onebot.py`；课表会话／查询工作流、解析和渲染位于 `services/timetable/`，SQLite、下载和头像访问位于 `infrastructure/timetable/`。`plugins/timetable` 只保留 Config、Matcher、事件转换和 NoneBot 网络依赖注入。WebUI 不导入业务插件包，配置和运行数据继续使用宿主机绑定目录。

## 快速开始：Docker Compose

Compose 统一编排 `napcat`、`qq-bot`、`webui`，使用自动创建的项目 bridge 网络。首次克隆先按 [Docker部署说明](Docker部署说明.md) 创建 `.env`、`qqbot/config/plugin_config.json` 和 `qqbot/config/webui.env`；示例插件配置只启用 `basic`，其他功能可在 WebUI 开启。已有配置保留原值。检查配置后再构建：

```powershell
Set-Location D:\qq_bot
docker compose -f compose.yaml config --quiet
docker compose -f compose.yaml build qq-bot webui
```

构建只生成镜像。构建成功后，拉取 NapCat 镜像并创建、启动三个容器：

```powershell
docker compose -f compose.yaml pull napcat
docker compose -f compose.yaml up -d --no-build
docker compose -f compose.yaml ps
```

本机页面默认为 NapCat `http://127.0.0.1:6099`、WebUI `http://127.0.0.1:8765`。NapCat 反向客户端目标为 `ws://qq-bot:8080/onebot/v11/ws`，token 与 `.env` 的 `NAPCAT_ACCESS_TOKEN` 一致；已有 NapCat 配置需手动核实。

配置目录挂载到 `/app/config`，机器人只读、WebUI 可写；机器人数据挂载到 `/app/storage`。源码复制进镜像，修改代码后需要重新构建。详细配置、全新部署步骤与 Mounts 检查见 [Docker部署说明.md](Docker部署说明.md)。

2026-10-25 已使用摘要锁定基础镜像和哈希锁定 Python 依赖无缓存构建 `qqbot-app:local`、`qqbot-webui:local` 与 `qqbot-tests:local`。正式 qq-bot 已重建并加载标准第三方插件；恢复只需从选定 Git 版本重建镜像并继续挂载现有 `config` 与 `storage`。

## 其他连接方式

### NapCat 运行在宿主机

当前 Compose **没有发布 qq-bot 的宿主机端口**。宿主机上的 NapCat 无法直接使用 Docker 网络服务名 `qq-bot`；按默认监听端口，可在现有 `services.qq-bot` 下增加以下配置：

```yaml
services:
  qq-bot:
    ports:
      - "127.0.0.1:8080:8080"
```

这是要合并进原 Compose 的片段，不是完整替代文件。重新创建容器后，同一宿主机上的 NapCat 使用 `ws://127.0.0.1:8080/onebot/v11/ws`。若修改 Compose 中机器人环境变量 `PORT`，还需同步调整端口映射、NapCat 的 `ONEBOT_URL` 和 WebUI 的 `BOT_MANAGEMENT_URL`；只在 `.env` 中修改 `LISTEN_PORT` 不会改变当前容器配置。

跨机器连接还需调整绑定地址和防火墙；只允许可信来源访问，并配置 token，不要将未鉴权的 OneBot 接口暴露到公网。

### bot 主动连接 NapCat

当前发布版只支持 Compose 默认的反向连接；`WEBSOCKET_MODE=client` 和 `NAPCAT_WS_URL` 不再作为生产入口配置。若要使用正向连接，需要在独立分支重新设计并验收对应的 NoneBot Driver 配置，不应恢复已移除的旧 WebSocket 实现。

默认三服务方案使用反向连接，地址见部署说明。正向连接不属于当前发布版。

## 环境变量

首次部署时在根目录创建 `.env`，按下表填写所需变量（Token、QQ 号和 API Key 使用自己的值，不提交 Git）。下表列出**当前 Compose 的默认值**，不是所有源码直接运行时的默认值。

| 变量 | Compose 默认值 | 说明 |
| --- | --- | --- |
| `DRIVER` | 固定 `~fastapi+~httpx+~websockets` | 反向 WebSocket 服务及业务 HTTP Driver；在 Compose 中声明 |
| `NAPCAT_ACCESS_TOKEN` | 空 | Compose 映射为机器人 `ONEBOT_ACCESS_TOKEN`；NapCat 客户端需设置相同 token，留空不校验 |
| `BOT_QQ` | 空 | 供 NapCat 的 `ACCOUNT` 使用；原生机器人身份和严格 @ 匹配使用 Adapter 的 `self_id` |
| `HOST` | 固定 `0.0.0.0` | NoneBot 容器监听地址，在 Compose 中声明 |
| `PORT` | 固定 `8080` | NoneBot 容器监听端口，在 Compose 中声明 |
| `LOG_LEVEL` | `INFO` | Python 日志级别 |
| `CORE_SELFTEST_ENABLED` | `false` | 是否启用 core 自检指令，仍受超级用户限制 |
| `SUPERUSERS` | `[]` | NoneBot 超级用户 QQ 列表，使用 JSON 字符串数组 |
| `MANAGEMENT_ACCESS_TOKEN` | 空 | 独立管理令牌；手动指定时至少 32 字符且不同于 OneBot token；留空自动生成，写入 `qqbot/storage/webui/management.token` |
| `AUTO_CANDY_QQ` | 空 | 自动贴表情的目标 QQ；留空且无插件设置时不触发 |
| `AUTO_EMOJI_IDS` | `127852,12951` | 表情 ID 列表，默认 🍬 和 ㊗ |
| `AUTO_CANDY_EMOJI_ID` | `127852` | 旧版单表情兼容项；当前 Compose 的 `AUTO_EMOJI_IDS` 默认值会优先于它 |
| `SONG_MAX_DOWNLOAD_MB` | `100` | 单个歌曲源文件下载上限，按 1024 × 1024 字节计算 MB；覆盖塞壬唱片 WAV 最大曲目（约 85MB） |
| `SONG_BASE64_UPLOAD_LIMIT_MB` | `35` | 文件大小分界线：不超过该值走 base64 单帧上传；超过则走 NapCat Stream API 分片上传（为 NapCat 反向 WebSocket 的 50 MiB payload 上限预留 Base64 和协议开销），同时在下载前向用户预告耗时 |
| `SONG_STREAM_CHUNK_KB` | `256` | Stream API 分片大小；以 85MB 文件为例约 340 次分片往返（单帧约 350KB），过慢可调大、被服务端拒绝可调小 |
| `SONG_CACHE_TTL_HOURS` | `24` | 歌曲源文件缓存有效期，缓存位于 `storage/downloads/song-cache`；同来源、同歌曲、同音质命中后直接复用 |
| `SONG_SESSION_MINUTES` | `10` | 歌曲候选会话有效期，单位分钟 |
| `SONG_MUSIC_SOURCE` | `netease` | 音乐源：`netease`/`tencent`/`kuwo`/`tidal`/`qobuz`/`joox`/`bilibili`/`apple`/`ytmusic`/`spotify`；稳定源为 `netease`、`joox`、`bilibili`，部分源暂不开放 |
| `SONG_BITRATE` | `320` | 不带音质参数时的默认音质；聊天内可选 `192`/`320`/`740`/`999`，740/999 为无损档（实际码率以上游返回为准） |
| `TIMETABLE_DB_PATH` | `/app/storage/db/timetable.sqlite3` | 对应宿主机 `qqbot/storage/db/` 绑定目录 |

旧 `LISTEN_HOST`、`LISTEN_PORT` 和 `NAPCAT_ACCESS_TOKEN` 仅在直接运行源码且未设置对应原生变量时作为兼容项；当前 Compose 使用原生 `HOST`、`PORT` 和 `ONEBOT_ACCESS_TOKEN`。`WEBSOCKET_MODE=client` 会被拒绝，`NAPCAT_WS_URL`、`RECONNECT_SECONDS` 当前不参与运行配置。

`BOT_PLUGIN_CONFIG` 默认读取 `qqbot/config/plugin_config.json`，Compose 固定为 `/app/config/plugin_config.json`。整个配置目录挂载，机器人只读、WebUI 可写，支持原子替换保存。

`TIMETABLE_DB_PATH` 在源码直接运行时默认指向 `qqbot/storage/db/timetable.sqlite3`，首次课表数据库操作时自动创建。当前 Compose 将此变量固定为上表路径；自定义路径必须同步调整数据卷挂载，不能只在 `.env` 中改值。

源码另外支持 `GD_MUSIC_API_URL`（GD 音乐台 API 地址，默认 `https://music-api.gdstudio.xyz/api.php`）以及旧版 `AUTO_EMOJI_QQ`、`AUTO_EMOJI_ID` 环境变量；这些变量当前没有通过 Compose 透传。需要在容器中使用时，应自行添加到服务的 `environment` 中。

## 插件配置与控制台

### 配置文件

可手动编辑 `qqbot/config/plugin_config.json`，例如：

```json
{
  "enabled_plugins": [
    "auto_emoji", "rps", "daily_wife", "courtship",
    "song", "novel", "timetable", "basic"
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
    }
  }
}
```

- 从 `enabled_plugins` 中移除对应 ID 即可关闭业务插件；`[]` 会关闭全部业务插件，包括 `/help`、`/hello`，常驻的 `core` 和 `management` 仍会加载。未知 ID 不会作为业务插件加载，management 将其列为 `unknown_configuration`；WebUI 保留已有未知项，不允许凭空添加。第三方插件需安装镜像依赖并加入 `pyproject.toml` 的 `[tool.nonebot].plugins`，不通过业务开关注册。
- `plugin_settings.rps.ban_seconds` 为 **1～86400 的整数秒字符串**，例如 `"120"`；缺省为 60 秒，不读取另一个猜拳环境变量。原生机器人会拒绝使用无效配置启动，WebUI 也会拒绝保存无效值。推荐用页面的秒／分钟输入，无需手工换算或编辑 JSON。
- `plugin_settings.daily_wife.marriage_enabled` 控制结芬子功能：`"true"` 开启，`"false"` 关闭；未配置时默认开启，兼容旧配置。关闭它不影响每日抽取，但关闭整个 `daily_wife` 插件会同时关闭抽取和结芬。
- `plugin_settings.timetable.group_isolation_enabled` 控制课表群聊隔离：`"true"`（默认）按群独立，`"false"` 按 QQ 跨群共享。旧配置缺省或无效值仍保持隔离；不会自动开放原有课表。
- 自动表情设置按“JSON 中显式字段 → 对应环境变量 → 内置默认值”读取。显式 `target_qq: ""` 会关闭目标，不会回退到环境变量；要继承环境值需省略该字段。`emoji_ids` 优先于旧 `emoji_id`，仅在两者均缺失时读取环境值。
- 文件应保存为 UTF-8（无 BOM）。**原生入口要求配置文件存在并通过严格校验**：文件缺失、不可读、JSON 无效或字段结构错误都会阻止启动，不会自动启用全部插件。WebUI 在文件不存在时可展示并保存默认配置；已有文件损坏则报错并禁止覆盖。旧文件若只有 `{}`，需补齐合法的 `enabled_plugins` 列表；修复时保留其他私人设置。
- 旧控制台写入的 `updated_at` 和 `plugins` 元数据会被 WebUI 保留；这些字段不用于动态发现或加载新插件。

### 独立本地 WebUI（新管理入口）

WebUI 使用 FastAPI + Uvicorn，前端无需 Node.js，可在 Windows 宿主机运行，也可使用独立 `webui` 镜像。容器版连接宿主机 Docker socket，保持本机端口绑定；部署见 [Docker部署说明.md](Docker部署说明.md)。停止机器人不会关闭管理页面。

#### 安装与启动

Docker 部署不需要宿主机 Python 环境。若只运行宿主机 WebUI，才需要单独创建 `.venv-webui`：

```powershell
if (-not (Test-Path -LiteralPath ".venv-webui\Scripts\python.exe")) {
    python -m venv .venv-webui
}
.\.venv-webui\Scripts\python.exe -m pip install -r qqbot/requirements/webui.txt
```

手动创建 UTF-8（无 BOM）编码的 `qqbot/config/webui.env`，按下面的模板填写：

```dotenv
# 必须自行填写 16～512 字符的独立随机密码；留空将拒绝启动
WEBUI_PASSWORD=
WEBUI_PORT=8765
```

上面的密码值特意留空，没有默认密码；必须自行填写且不能全为空白。**不要使用机器人 API Key 或 NapCat Token 作为登录密码**；私密文件已被 Git 和 Docker 构建上下文排除。服务不会自动创建该文件，也不会读写认证历史或生成备份。

Docker 部署会自动读取 `qqbot/config/webui.env`；当前不再依赖根目录快捷方式或 `启动WebUI.cmd`。详细密码设置见 [Docker部署说明](Docker部署说明.md)。

统一启动入口（也可手动运行）：

```powershell
.\.venv-webui\Scripts\python.exe qqbot/run_webui.py
```

浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，输入自己设置的管理员密码。宿主机入口默认仅监听 `127.0.0.1`；容器入口监听 `0.0.0.0`，由 Compose 将端口绑定到宿主机回环地址。会话 1 小时到期，HttpOnly + SameSite=Strict，注销立即失效；每分钟最多 5 次登录尝试。使用本地 HTTP，因此 Cookie 不设置 Secure；未来远程访问必须另行设计 HTTPS，不能直接暴露当前服务。Host、来源和 CSRF 均校验，不支持代理转发。

注意：同名**进程环境变量优先于 `qqbot/config/webui.env`**。Docker 中容器端口固定为 `8765`；修改对外端口应设置根目录 `.env` 的 `WEBUI_PORT`，Compose 会同步端口映射和来源校验，修改 `webui.env` 的端口不会生效。宿主机直接运行时才使用 `webui.env` 中的端口。修改密码或端口后重新创建 WebUI 容器（宿主机运行则重启进程），已有会话失效；端口范围为 1024～65535。

宿主机 WebUI 默认无法解析 Docker 内部的 `qq-bot` 管理地址。需要在线插件状态时，按上文发布机器人回环端口，再在启动 WebUI 前执行 `$env:BOT_MANAGEMENT_URL='http://127.0.0.1:8080/internal/management/status'`；该管理地址只读取进程环境变量。

#### 页面和设置

- **概览**：Docker 可用性、可验证的容器状态、启用插件数、最多 20 项内存任务记录。**容器运行不代表 QQ 已连接**。
- **插件管理**：当前显示 8 个业务配置项，搜索、状态筛选、单独和批量开关、侧边设置面板。主插件关闭后控件置灰，子功能和原有配置保持不变。
- **猜拳**：秒／分钟输入，保存 `plugin_settings.rps.ban_seconds` 字符串，范围 1～86400 整数秒。缺省 60 秒。无效值会阻止原生机器人启动，WebUI 也会拒绝保存。机器人仍需群管理权限。
- **自动贴表情**：直接输入或粘贴 😀、😂、👍、❤️、🔥、🍬、㊗️ 等单码点 Unicode 表情，解析后预览、去重、删除；不限于快捷按钮，内部继续保存兼容的十进制 `emoji_ids`。仅转换符合 Emoji 属性且不会与 QQ 短 ID 混淆的字符，不承诺所有字符或 QQ 专属表情均可用。肤色、旗帜、ZWJ 等组合按完整字素簇拒绝，不拆成错误回应。无法反向映射的旧 ID 显示「旧版表情（保留）」，其他设置修改不会丢弃它。继承环境时只作预览，不主动写入 JSON；实际 NapCat 版本接受情况须授权群验收。
- **今日群友／课表**：分别配置结芬子功能和群聊隔离；关闭隔离会提示跨群共享的隐私影响。
- **群聊总结**：该功能已暂时移除，不提供设置项，也不读取群聊历史或调用 DeepSeek；`/总结` 进入未知指令兜底。
- **日志**：认证后请求最近 200 行，并按输出上限截取展示；支持搜索、暂停自动刷新与滚动，日志量和长行有上限，凭据脱敏。qq-bot 仍接收全部 OneBot 事件以支持 `auto_emoji` 等被动插件，但原始事件日志只显示真正包含机器人 QQ `@` 消息段的消息；普通群消息、通知事件和自动贴表情 Matcher 的开始／完成噪声不显示，警告、错误、启动和连接日志保留。NapCat 自身日志仍会显示它接收的全部群消息。

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

初次部署猜拳新增运行代码必须构建镜像；之后仅调整配置，使用保存并应用，无需构建。配置已改为目录挂载，原子替换不会困在旧文件挂载中；WebUI 仍使用重新创建流程验证配置。仅 `restart` 不会加载更新后的 Compose 环境变量。重新创建会丢失部分内存会话和每日记录，但不删除持久化课表。

配置损坏或不可读时，WebUI 明确报错，不把它显示为「全部启用」并覆盖；需在保留原设置的前提下手动修复原文件。并发保存返回 409，重新加载后再编辑。不要同时在多个页面或外部程序中写配置，外部程序不参加 WebUI 文件锁。

#### 隔离测试与迁移状态

阶段二测试使用根目录的 `compose.test.yaml`，测试镜像不挂载正式配置、数据库、NapCat 登录目录，也不连接真实 QQ：

```powershell
docker compose -f compose.test.yaml build tests
docker compose -f compose.test.yaml run --rm tests
```

如已安装 Node.js，可单独运行两个前端回归；它们无需 npm 或浏览器：

```powershell
node qqbot/tests/test_webui_duration.mjs
node qqbot/tests/test_webui_frontend.mjs
```

2026-10-26 已完成历史测试清理、目录职责拆分、可复现发布、第三方插件加载、正式数据重建前后自动核对以及恢复登录后的 OneBot／授权群查询验收；重构计划阶段 0～12 已全部收尾。

#### 常见问题入口

- 页面打不开：确认 WebUI 终端未退出、依赖已安装、密码及端口有效；只改端口时重启 WebUI，不重建 bot。
- Docker 不可用：仍可使用页面与配置功能；等准备好后再启动 Docker Desktop，不用为了查看页面先启动容器。
- “配置损坏”：检查 `enabled_plugins` 是否为字符串数组、设置值是否符合格式；旧 `{}` 不通过 WebUI 校验。不要把文件删掉来消除提示。
- 409 冲突／已有任务：等待当前操作结束；若为版本冲突，确认是否丢弃当前草稿后重新加载，不盲目重试或删除锁文件。
- 403／会话问题：使用配置端口的本机直接地址，不走代理；重新登录，不关闭 Host／来源／CSRF 防护。

更多诊断及离线／正式入口区分见 [Docker部署说明](Docker部署说明.md)。

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
- `@bot /删除课表`：删除本人在当前生效范围内已入库的课表。默认群聊隔离模式只删除当前群记录；共享模式删除该 QQ 的共享课表记录。删除会清除对应课程，其他成员的课表不受影响；没有课表时重复执行不会报错。
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
- 默认 Docker 镜像安装 `Pillow==12.3.0` 和 Debian `fonts-noto-cjk`。Windows 本地开发可使用系统微软雅黑；其他 Linux 本地环境请安装 Noto Sans CJK。也可通过进程环境变量 `TIMETABLE_FONT_REGULAR` / `TIMETABLE_FONT_BOLD` 指定可用的字体文件；缺字用占位字符，缺少整套中文字体则安全回退文字。不要将 Windows 字体复制进发布镜像。

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
- **Compose 绑定宿主机 `qqbot/storage` 到 `/app/storage`**，数据库位于 `/app/storage/db/timetable.sqlite3`。重建容器保留目录数据，上传中的内存会话会失效。
- `qqbot/storage/`、业务配置和 NapCat 登录数据均从 Git 或构建上下文排除。正式数据保存在宿主机绑定目录；删除该目录会丢失数据。
- 同时适配群上传通知和群文件消息段（含 CQ 字符串）；文件 ID 去重加 SHA-256 内容去重。不同 ID 可能需要各下载一次，不能只按“同名、同大小”判重。
- 下载通过 NapCat 的 `get_group_file_url` 获取 HTTP(S) 地址，不使用文件消息中的任意 URL；不执行或访问 ICS 内部的链接、附件或提醒。下载使用 NoneBot HTTP Driver 的异步流式请求，并检查重定向目标、内容和大小。
- 下载失败可在有效会话内重新上传；取消、到期、重开或停止后，旧下载/解析结果不会写入新会话或触发新的入库。停止时已提交到数据库工作队列的事务允许完成，但不会再发送迟到回执。

已有显式 `enabled_plugins` 列表的配置不会自动启用新插件，需要加入 `timetable` 或在控制台勾选后重新创建 bot 容器。真实群上传还要求 NapCat 正常上报群文件事件，且 `get_group_file_url` 可用；接收流程按锁定的 `mlikiowa/napcat-docker@sha256:b101…f1b7e` 验证。**仅从阶段二预览版升级**的用户需重新 `/导入课表`、上传 ICS 并 `/已导入` 一次；阶段三及以后已经正式入库的课表可直接继续使用，本次升级无需重新导入，也不改变数据库结构。

#### 课表在线备份（Docker / PowerShell）

本节是原有课表功能的**可选手工运维参考**，不属于本次 WebUI 安装或验收流程；本次不执行备份，也未新增自动备份功能，现有备份保留。若以后另行决定执行手工备份，下面使用 SQLite `backup()` 生成包含已提交 WAL 数据的一致性快照，保存到绑定目录 `qqbot/storage/backups/`；不会停止 bot、改写正式课表或读取群文件。每次生成不同文件名，并拒绝覆盖同名快照。

在项目目录运行：

```powershell
$backupName = "timetable-" + (Get-Date -Format "yyyyMMdd-HHmmss-fff") + ".sqlite3"
$backupScript = @'
import sqlite3, sys
from contextlib import closing
from pathlib import Path
source = Path("/app/storage/db/timetable.sqlite3")
target = Path("/app/storage/backups") / sys.argv[1]
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
# /app/storage/backups 已绑定到 qqbot/storage/backups，无需 docker compose cp。
```

- 备份含课程安排和 QQ 关联信息，和正式库一样需要限制访问；不要上传到公共仓库或聊天中。上述目录已被 Git / Docker 忽略；只保留在同一磁盘的副本不能防范磁盘故障，应按自己的策略留存。
- 这是课表数据库备份，不包含 `.env` 或插件配置；配置文件也应单独安全保管。恢复必须先停止 `qq-bot`、保留当前数据库及 WAL/SHM 文件，再离线恢复，不能在线覆盖数据库或以清空数据卷代替恢复。
- 仅更新 bot 可执行 `docker compose up -d --no-deps --build --force-recreate qq-bot`；不要使用 `down -v`。换目录或改项目名时应先核对实际绑定目录。

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
| 容器重建后看不到课表 | 检查 `/app/storage` 是否绑定到 `qqbot/storage`，以及 `db/timetable.sqlite3` 是否存在。 |

群内验收由操作者自行发送真实 @指令：检查 `/help` / `/课表`、本人导入与更新、`/课ing` 的群名片及无课状态、`/今日课程` 只 @本人；更新失败或取消后确认旧课表仍可查询。若测试昵称变更或退群过滤，只使用自愿参与的测试成员。图片升级后还应检查：两条查询均能显示图片和真实 @；14:30/15:55 等边界后的新查询更新状态；长课名、无课、重叠课程及多页没有遗漏；头像失败仍能展示；连续查询不会在 bot 目录生成图片。隔离开关可由自愿测试成员验收：在 A 群导入后，关闭隔离应能在 B 群查询；B 群共享更新后 A 群读到新内容；重新开启隔离后按原归属群查询，非 B 群成员始终不应出现在 B 群的 `/课ing` 中。群内消息与 NapCat 自身缓存可能保留图片，不属于 bot 输出目录。自动化测试不会代发群消息，也不会读取真实群文件。

### 自动贴表情

启用 `auto_emoji` 并设置目标 QQ 后，bot 会在能接收到该成员发言的各群中，对其带 `message_id` 的消息依次调用 NapCat 的 `set_msg_emoji_like`。不处理私聊，也不需要指令或 @。

日常使用推荐在 WebUI 中直接粘贴 😀、😂、👍、❤️、🔥、🍬、㊗️ 等表情，点击“解析并添加”，不需要查询编码。支持单码点 Unicode Emoji（允许末尾的 emoji 样式选择符），不再限定两种表情；是否被当前 QQ／NapCat 实际接受仍需人工验收。以下 ID 说明用于理解旧配置和环境变量，不是页面输入要求。

表情 ID 支持用英文/中文逗号、分号或空白分隔，并自动去重。新配置建议使用 `AUTO_EMOJI_IDS` 或插件设置中的 `emoji_ids`；插件设置同时兼容旧的 `emoji_id` 字段。能否成功贴表情取决于 NapCat 接口和具体表情 ID 的支持情况。

### 群聊总结

群聊总结功能已暂时移除，当前不加载对应插件、不读取群聊历史，也不调用 DeepSeek；`/总结` 会进入未知指令兜底。

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

小说会话仅按 QQ 号保存，不区分群聊；同一成员在其他群重新搜索也会覆盖上次查询，重启后清空。站内搜索返回空页面时会使用公开索引结果定位小说，再以首页链接作最后回退；简体目录被拦截或无法解析时会自动切换繁体目录，后续卷／章链接保持同一可用域名。常见简称和“关于我转生成史莱姆”一类书名变体会进行放宽匹配。也可以直接提供小说页链接：

```text
@bot /novel https://www.linovelib.com/novel/2139.html
```

### 歌曲搜索与下载

`@bot /song 歌曲名称` 从 [GD 音乐台](https://music.gdstudio.xyz/)（API：`music-api.gdstudio.xyz`）搜索并列出最多 10 首候选，包含歌名、歌手和专辑。当前成员随后输入：

- `@bot /song 2` 或 `@bot /song 选择 2`：按默认音质（`SONG_BITRATE`，默认 `320`）下载；
- `@bot /song 2+320`（也支持 `选择 2+999`、`2 740`）：按**指定音质**下载，音质可选 `192` / `320` / `740` / `999`（740/999 为无损档，实际码率以上游返回为准）。

bot 会在**同一条消息**中返回歌曲详情（歌名、歌手、专辑、音质、文件大小）和专辑封面图，随后获取直链并**直接发送源文件**（MP3 或 FLAC，不做二次转码，与网页下载的文件一致）。全程无需任何网盘 Cookie。

- `@bot /song 取消` 或 `@bot /song cancel` 可取消候选；默认有效期 10 分钟，选择后消耗该候选会话。
- 候选按群聊 / 私聊范围和用户区分，仅保存在内存中，重启后清空。
- 封面通过 `types=pic` 接口获取，以 base64 图片段内嵌在同一条消息中；封面获取失败时只发送文字详情，不影响下载。
- 源文件格式按文件头自动识别（MP3/FLAC/M4A/WAV/OGG），文件名保留原始扩展名。
- `SONG_MUSIC_SOURCE` 选择音乐源，默认 `netease`。若当前音源不支持所选音质，bot 会回复 `当前音源不支持所选音质（xxxkbps），请降低音质后重试。`
- `SONG_MAX_DOWNLOAD_MB` 限制源文件大小，默认 100 MB。无损档单曲可能达 70 MB 以上，下载 999 音质失败时可调大此上限。
- **大文件上传**：文件超过 `SONG_BASE64_UPLOAD_LIMIT_MB`（默认 35 MiB）时，bot 会在下载前预告耗时，并使用锁定 NapCat 镜像验证过的 Stream API 分片上传，避免 Base64 单帧超过 NapCat 反向 WebSocket 的 50 MiB payload 上限；分片失败会明确提示上传失败。分片大小由 `SONG_STREAM_CHUNK_KB`（默认 256KB）控制。
- 小文件通过 NapCat 的 `upload_group_file` 或 `upload_private_file` 以 `base64://` 上传；大文件先通过 `upload_file_stream` 写入 NapCat 文件系统，再调用对应文件上传 API。发送后释放上传占用；成功下载的音频保留为缓存，失败或取消产生的临时文件会清理。
- 上游接口限制 5 分钟内不超过 50 次请求，触发限流时回复 `请求过于频繁，请稍后再试。`；歌曲无版权或已下架时回复 `该歌曲暂时无法获取下载链接，可能无版权或已下架。`；未找到时回复 `私密马赛，没有找到喵~`。
- **下载失败兜底**：下载阶段失败或超时（含超过 `SONG_MAX_DOWNLOAD_MB` 上限）时，bot 会回复 `下载失败，请点击直链自行下载：<直链>`，GD 与塞壬源均适用；上传阶段失败仍提示重新搜索或前往原站下载。
- **歌曲缓存**：下载成功的音频会保存在 `storage/downloads/song-cache`，默认保留 24 小时；相同来源、歌曲 ID 和音质会直接复用缓存，容器重建后只要 `storage` 挂载仍在即可继续复用。缓存按文件修改时间（下载完成时记录）计算有效期，命中不会续期；启动时清理并每小时巡检；首次下载遇到临时网络或 DNS 异常会自动重试一次。

> API 出处：[GD音乐台(music.gdstudio.xyz)](https://music.gdstudio.xyz/)，该接口声明仅限学习参考、严禁商用；音乐源可用性由上游动态调整。

### 塞壬唱片（Monster Siren）音乐源

在搜索关键词后加 `+塞壬唱片` 即可改从 [塞壬唱片](https://monster-siren.hypergryph.com/music) 搜索和下载：

- `@bot /song 大地之盐+塞壬唱片`：在塞壬唱片站内搜索候选；**下载时不需要加后缀**，直接 `@bot /song 1` 即可。
- 会话来源在搜索时一次锁定：塞壬会话的裸序号选择下载 WAV；**搜索时没加后缀的会话（默认源）当次不支持塞壬下载**，此时用 `+塞壬唱片` 选择会被拒绝并提示重新搜索。
- 塞壬源固定提供 **WAV 无损**，没有音质选项；在塞壬会话中输入 `+192/320/740/999` 会被拒绝并提示改用裸序号；详情消息不显示文件大小（单曲通常 35 MB 左右）。
- 曲库来自站点公开接口（`/api/songs` + `/api/albums`，约 900 首），按曲名 / 专辑名本地匹配；目录带 10 分钟缓存（`SIREN_CATALOG_TTL_MINUTES` 可调；Docker 部署需要自定义时可将该变量和 `SIREN_API_BASE_URL` 加入 Compose 的 `environment`）。
- 封面为专辑封面（`/api/albums` 的 `coverUrl`），同样内嵌在详情同一条消息中；获取失败只发文字，不影响下载。
- 塞壬唱片 WAV 单曲约 35~85MB，超过 `SONG_BASE64_UPLOAD_LIMIT_MB`（默认 35 MiB）时 bot 会预告耗时并走 Stream API 分片上传（见上文「歌曲搜索与下载」的大文件上传说明）；默认 `SONG_MAX_DOWNLOAD_MB`（100 MB）可覆盖已知最大曲目，超限会走下载失败兜底（回复直链）。

## 本地开发与测试

项目使用 Docker 作为运行和测试环境，宿主机不需要恢复 `.venv`、`.venv-webui` 或安装 NoneBot。生产镜像启动入口是 `qqbot/bot.py`；当前原生代码包含 `plugins/core`、`plugins/management`、`plugins/basic`、`plugins/rps`、`plugins/auto_emoji`、`plugins/daily_wife`、`plugins/courtship`、`plugins/novel`、`plugins/song` 和 `plugins/timetable`。业务插件按 `enabled_plugins` 加载；`core`、`management` 常驻，额外插件按 `pyproject.toml` 声明加载。

隔离回归不挂载正式配置、数据库、NapCat 登录目录，也不连接真实 QQ：

```powershell
Set-Location D:\qq_bot
docker compose -f compose.test.yaml build tests
docker compose -f compose.test.yaml run --rm tests
```

测试入口 `qqbot/tests/run_checks.py` 会运行配置与插件加载边界、严格 @、summary 移除、旧协议清理、课表纯业务、阶段三至六 NoneBug Matcher/API、WebUI Python 及前端回归。2026-10-25 使用锁定依赖重建测试镜像后，基础与课表 pytest 223 项、原生插件回归 44 项、WebUI Python 回归 58 项和全部前端回归通过，历史套件无跳过项。

完整入口和历史测试处置记录见 [qqbot/tests/README.md](qqbot/tests/README.md)。直接运行 `pytest` 不包含原生与前端全部分组，不能替代 `run_checks.py`。

## 配置安全与排查提示

- `.env` 和 `plugin_config.json` 均被 Git 忽略，但这不等于加密。不要分享包含 token、Cookie 或 API Key 的文件、截图和日志。
- 新构建镜像不再复制 `plugin_config.json`，由 Compose 在运行时只读挂载，`.env`、课表库和备份也从构建上下文排除。旧镜像/旧构建缓存可能仍包含以前复制的私密配置，不要对外分享。单独 `docker run` 时需自行挂载配置并传入环境变量；未提供合法插件配置会拒绝启动，不会自动继承宿主机配置。
- 当前 Compose 自动创建项目网络；若仍提示外部网络不存在，检查是否误用旧 Compose 文件。NapCat 连接失败时核对实际反向地址与双方 token。
- 已连接但不响应时，检查 Adapter 显示的机器人账号、真正的 @ 消息段、插件是否启用，以及配置修改后是否重新创建了容器。关闭 `basic` 会同时关闭 `/help` 和 `/hello`。
- 第三方网站搜索失败或 OneBot 动作失败时，先查看 `docker compose logs -f qq-bot`，分别检查网络、接口支持和群权限；避免公开未经脱敏的错误内容。
