"""Only lifecycle and recovery events cross worker/session log boundaries."""
KEY_PREFIXES = ("[战斗开始]", "[战斗结束]", "[战斗逃跑]", "[战斗超时]", "[任务取消]", "[匹配计时]")


def is_key_log(message):
    return isinstance(message, str) and message.startswith(KEY_PREFIXES)
