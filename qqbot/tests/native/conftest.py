from pathlib import Path

import nonebot
import pytest
from nonebug import NONEBOT_INIT_KWARGS, NONEBOT_START_LIFESPAN


def pytest_configure(config):
    config.stash[NONEBOT_INIT_KWARGS] = {
        "driver": "~none",
        "command_start": {"/"},
        "rps_ban_seconds": 300,
        "auto_emoji_target_qq": "111",
        "auto_emoji_ids": "12951,127852",
        "daily_wife_marriage_enabled": True,
        "song_download_dir": Path("/tmp/qqbot-song-tests"),
        "timetable_db_path": Path("/tmp/qqbot-timetable-tests.sqlite3"),
        "timetable_group_isolation_enabled": True,
    }
    config.stash[NONEBOT_START_LIFESPAN] = False


@pytest.fixture(scope="session", autouse=True)
def load_native_plugins(_nonebot_init):
    from infrastructure.runtime import runtime

    runtime.configure(
        type("TestConfig", (), {
            "assets_root": Path("/nonexistent"),
            "project_file": Path("pyproject.toml").resolve(),
        })(),
        {
            "enabled_plugins": ["auto_emoji", "rps", "basic"],
            "plugin_settings": {
                "rps": {"ban_seconds": "300"},
                "auto_emoji": {"target_qq": "111", "emoji_ids": "12951,127852"},
            },
        },
        "phase3-test",
    )
    for module in (
        "plugins.core", "plugins.basic", "plugins.rps", "plugins.auto_emoji",
        "plugins.daily_wife", "plugins.courtship", "plugins.novel", "plugins.song",
        "plugins.timetable",
    ):
        assert nonebot.load_plugin(module) is not None

