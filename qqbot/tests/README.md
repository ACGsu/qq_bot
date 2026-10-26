# 回归测试入口

在项目根目录执行隔离测试；不需要宿主机 Python 或虚拟环境：

```powershell
docker compose -f compose.test.yaml build tests
docker compose -f compose.test.yaml run --rm tests
```

`run_checks.py` 是完整入口，分进程执行基础与课表 pytest、`native/` 下的 NoneBug 原生 Matcher/API 回归、WebUI Python 回归和两个 `.mjs` 前端回归。WebUI 测试关闭 NoneBug 插件，原生测试使用自己的框架初始化配置，因此不要用一次 `pytest tests` 替代完整入口。`pyproject.toml` 的默认 pytest 清单也不包含 `native/` 和 JavaScript 回归。

测试镜像不挂载正式配置、数据库、QQ 登录目录或 Docker socket；临时数据由测试自行创建。Node.js 由 `Dockerfile.test` 安装，Python 测试依赖通过 `requirements/dev.lock` 的精确版本与哈希安装，`dev.txt` 仅是维护输入。

## 阶段 9 历史测试处置完成记录

2026-10-25 已完成 8 个历史文件、340 个旧用例的审查。处置以行为类别映射到当前测试为准；不再为测试恢复 `NapCatBot`、私有分发器、旧插件工厂、summary 或旧塞壬模块。

| 历史文件 | 处置结果 | 当前覆盖 |
| --- | --- | --- |
| `test_bot.py`（91） | summary、旧 dispatcher／factory 和同步上传编排按新设计废弃；歌曲、自动表情、daily_wife、courtship、配置与严格 @ 行为已有等价覆盖 | `native/test_phase3_plugins.py`、`native/test_phase4_plugins.py`、`native/test_phase5_song.py`、`test_phase2_native.py` |
| `test_daily_wife_settings.py`（15） | 设置优先级、帮助过滤、别名、缓存和任务清理由原生阶段四覆盖；旧工厂断言废弃 | `native/test_phase4_plugins.py`、`test_webui_settings.py` |
| `test_help_menu.py`（37） | 帮助内容、卡片结构、回执失败、超时、取消和并发隔离由 basic 原生及前端结构测试覆盖 | `native/test_phase3_plugins.py`、`test_webui_frontend.mjs` |
| `test_rps_settings.py`（4） | 配置、安全回退、禁言 payload 与限制由原生阶段三覆盖；旧工厂断言废弃 | `native/test_phase3_plugins.py` |
| `test_timetable.py`（121） | 文件规范与 HTTP 边界保留为 `test_timetable_files.py`；解析队列、事务、查询和 Matcher 行为映射到隔离解析、存储及原生课表回归；同步下载／旧 dispatcher 断言废弃 | `test_timetable_files.py`、`test_timetable_ics.py`、`test_timetable_store.py`、`native/test_phase6_timetable.py` |
| `test_timetable_images.py`（35） | 图片计划、多页、回执分类、文字回退与关闭边界保留；旧 echo 分发器断言废弃 | `test_timetable_image_workflow.py`、`test_timetable_image_service.py`、`native/test_phase6_timetable.py` |
| `test_timetable_settings.py`（6） | 隔离设置、配置往返与帮助展示保留；旧 bot factory 断言废弃 | `test_timetable_configuration.py`、`native/test_phase6_timetable.py` |
| `test_timetable_shared.py`（31） | 共享选择、并发更新、成员白名单和图片归属保留为存储／图片测试；旧同步工作流映射到原生 Matcher 回归 | `test_timetable_shared_store.py`、`native/test_phase6_timetable.py` |

上述 8 个旧文件已删除，统一入口不再导入已退出的生产类型。当前发布结果为基础与课表 223 项、原生回归 44 项、WebUI Python 58 项及全部前端回归通过，且没有历史跳过项。
