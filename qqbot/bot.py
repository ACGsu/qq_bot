"""Native NoneBot entrypoint. Dependencies and plugins are installed in Docker."""
import nonebot
from pathlib import Path
from nonebot.adapters.onebot.v11 import Adapter
from infrastructure.bootstrap import load_application_plugins
from infrastructure.config import load_runtime_config
from infrastructure.logging import install_bot_log_filter
from infrastructure.runtime import runtime


def create_bot():
    config, payload, version = load_runtime_config()
    # Compose passes validated variables explicitly; avoid reading an image-local dotenv file.
    nonebot.init(_env_file=Path(), **config.nonebot_options(payload))
    install_bot_log_filter()
    driver = nonebot.get_driver()
    driver.register_adapter(Adapter)
    runtime.configure(config, payload, version)
    load_application_plugins(config.project_file, payload)
    return driver


if __name__ == "__main__":
    create_bot()
    nonebot.run()
