# NapCat QQ Bot

一个通过 NapCat OneBot v11 WebSocket 客户端连接的 QQ bot。

程序只依赖 Python 标准库，Docker 构建时不需要下载 Python 包。

## 功能

- 只有消息中 `@bot` 后面的指令会触发响应。
- `@bot /help`：返回可用指令列表。
- `@bot /hello`：回复 `啦啦啦`。
- `@bot /猜拳 @成员`：发起者与被 @ 成员随机猜拳，输家禁言 5 分钟，平局不禁言。
- `@bot /今日群友`：在当前群聊中随机抽取一位群成员作为今日群友老婆，并发送头像和 @ 结果。
- `@bot /结芬`：向今日抽到的群友老婆发起结芬请求，对方 2 分钟内回复 `@bot /愿意` 或 `@bot /不愿意`。
- `@bot /求偶 @成员`：向被 @ 成员发起求偶请求，对方回复 `@bot /接受求偶` 或 `@bot /拒绝求偶`。
- `@bot /song 歌曲名`：搜索歌曲并列出候选。
- `@bot /song 序号`：下载所选歌曲并转换为 MP3 发送。
- `@bot /novel 小说名`：在 linovelib 查询小说目录链接。
- `@bot /第3章` 或 `@bot /第2卷`：返回上次查询小说对应章节或卷的阅读链接。
- 支持正向 WebSocket 客户端模式，也支持 NapCat 当前常见的反向 WebSocket 模式。

## 插件结构

`bot.py` 只负责读取配置、连接 NapCat、分发消息和发送 OneBot 动作。具体功能拆分在 `plugins/` 下：

- `basic.py`：`/help`、`/hello`
- `rps.py`：`/猜拳`
- `daily_wife.py`：`/今日群友`、`/结芬`
- `courtship.py`：`/求偶`
- `song.py`：`/song` 与夸克分享下载
- `novel.py`：`/novel`、`/第N章`、`/第N卷`

## 配置

复制 `.env.example` 为 `.env`，按需修改：

```env
WEBSOCKET_MODE=server
NAPCAT_WS_URL=ws://host.docker.internal:3001
NAPCAT_ACCESS_TOKEN=
BOT_QQ=123456789
LISTEN_HOST=0.0.0.0
LISTEN_PORT=8080
LOG_LEVEL=INFO
RECONNECT_SECONDS=5
SONG_MAX_DOWNLOAD_MB=80
SONG_SESSION_MINUTES=10
QUARK_COOKIE=
```

如果 NapCat 和 bot 在同一个 Docker Compose 网络中，可以把 `NAPCAT_WS_URL` 改成类似：

```env
NAPCAT_WS_URL=ws://napcat:3001
```

`BOT_QQ` 建议填写 bot 的 QQ 号，用于准确识别 `@bot`。如果不填写，程序会优先使用 NapCat 消息事件里的 `self_id`。

## 运行

```powershell
docker compose up -d --build
```

查看日志：

```powershell
docker compose logs -f qq-bot
```

## NapCat 侧设置

当前 Compose 默认使用反向 WebSocket 模式，容器名和网络别名为 `my-bot`，监听端口为 `8080`。在 NapCat 中配置反向 WebSocket 地址：

```text
ws://my-bot:8080
```

如果你想改成 bot 主动连接 NapCat，把 `.env` 改为：

```env
WEBSOCKET_MODE=client
NAPCAT_WS_URL=ws://host.docker.internal:3001
```

## 权限说明

`/猜拳` 会调用 OneBot v11 的 `set_group_ban` 接口。要让禁言生效，需要 bot 在群里拥有管理员或群主权限，并且目标成员不能是权限高于 bot 的成员。

## 今日群友老婆

输入 `@bot /今日群友` 后，bot 会通过 OneBot v11 的 `get_group_member_list` 获取当前群成员列表，随机抽取一位成员并排除抽取者自己，然后发送该成员头像并回复 `@成员 xxx是你的今日群友老婆~`。

同一位成员在同一个群内每天只能抽取一次。当天再次输入该指令时，bot 会提示已经抽取过，并 @ 当天已抽到的成员。记录保存在 bot 内存中，按中国时区日期判断，每天 24:00 后自动重置；容器重启后当天记录也会清空。

抽取后可以继续输入 `@bot /结芬`，bot 会 @ 今日抽到的群友并询问是否愿意。对方需要在 2 分钟内回复 `@bot /愿意` 或 `@bot /不愿意`；不愿意或超时会回复 `雑魚~雑魚~，被甩了捏~`，愿意则回复 `正在制作结婚证（其实是生图ai没钱弄）`。同一位成员在同一个群内每天只能发起一次结芬，当天再次输入 `@bot /结芬` 会直接返回当次结芬结果。

## 求偶

输入 `@bot /求偶 @成员` 后，bot 会 @ 求偶对象并询问是否接受求偶。对方回复 `@bot /接受求偶` 会 @ 发起者并回复 `接下来的内容需要付给观看`；回复 `@bot /拒绝求偶` 会 @ 发起者并回复 `啊，哦，看来你只能自己解决了呢~`。

## 小说链接

`/novel` 只返回目录、章节、卷的阅读链接，不抓取正文。查询成功后，bot 会把该小说目录保存在当前 QQ 成员的会话里，随后可以继续输入 `@bot /第1章` 或 `@bot /第1卷` 获取对应链接。若站内搜索被防护拦截，bot 会从首页公开链接里兜底匹配；也可以直接输入 linovelib 小说页链接，例如：

```text
@bot /novel https://www.linovelib.com/novel/2139.html
```

## 歌曲搜索

输入 `@bot /song 歌曲名称` 后，bot 会从 `https://xiageba.liumingye.cn/` 搜索并列出最多 10 首候选歌曲，包含歌名、歌手和专辑。当前成员随后输入 `@bot /song 2` 或 `@bot /song 选择 2`，bot 才会读取歌曲详情、选择可直接访问的音频、转换为 MP3 并发送。输入 `@bot /song 取消` 可取消候选；候选默认保存 10 分钟。未找到时回复 `私密马赛，没有找到喵~`。

镜像内通过 FFmpeg 完成 MP3 转换。`SONG_MAX_DOWNLOAD_MB` 控制单个源文件允许下载的最大体积，默认是 `80` MB；`SONG_SESSION_MINUTES` 控制候选保存时长，默认是 `10` 分钟。歌曲文件通过 NapCat 的 `upload_group_file` 或 `upload_private_file` 动作以 base64 文件上传，发送后会删除 bot 容器内的临时文件。

夸克分享链接需要在 `.env` 中配置 `QUARK_COOKIE`。bot 会把分享中的 MP3 临时转存到账号根目录下的 `qqbot-song-*` 目录，获取下载直链后删除该临时目录。Cookie 只通过容器环境读取，不会出现在 QQ 消息或正常日志中。若未配置 Cookie，或遇到暂不支持的百度网盘分享页，bot 会返回原始链接。
