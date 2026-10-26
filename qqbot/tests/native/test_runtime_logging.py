from infrastructure.logging import should_display_log


def test_runtime_log_filter_only_shows_messages_that_mention_this_bot():
    prefix = "OneBot V11 3814615844 | [message.group.normal]: Message 1 from 2@[群:3] "
    assert not should_display_log("SUCCESS", prefix + "'普通消息'")
    assert not should_display_log("SUCCESS", prefix + "'[at:qq=123] 其他人'")
    assert should_display_log("SUCCESS", prefix + "'[at:qq=3814615844] /novel 无职转生'")
    assert not should_display_log(
        "SUCCESS",
        "OneBot V11 3814615844 | [notice.group_msg_emoji_like]: {'message_id': 1}",
    )


def test_runtime_log_filter_keeps_operations_and_hides_passive_matcher_noise():
    start = "Event will be handled by Matcher(type='message', module=plugins.auto_emoji, lineno=48)"
    complete = "Matcher(type='message', module=plugins.auto_emoji, lineno=48) running complete"
    assert not should_display_log("INFO", start)
    assert not should_display_log("INFO", complete)
    assert should_display_log(
        "INFO",
        "Event will be handled by Matcher(type='message', module=plugins.novel, lineno=38)",
    )
    assert should_display_log("SUCCESS", "OneBot V11 | Bot 3814615844 connected")
    assert should_display_log("ERROR", complete)

