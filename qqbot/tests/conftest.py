"""Initialize NoneBot only for non-NoneBug regression groups."""

import nonebot


def pytest_configure(config):
    if any("tests/native" in str(argument).replace("\\", "/") for argument in config.args):
        return
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init(driver="~none", command_start={"/"})
