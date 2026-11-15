"""道具库存查询；复用脚本的当前账号会话，不购买或缓存库存。"""
from __future__ import annotations

RESPONSE_TIMEOUT_SECONDS = 15.0


async def get_item_count(context, item_id: int) -> int:
    """查询 14_2 的 userms，返回指定道具的 qt；列表中不存在时返回 0。"""
    if type(item_id) is not int or item_id <= 0:
        raise ValueError("道具 ID 必须是正整数")

    await context.wait_for_player_initialization()
    context.assert_automation_allowed()
    subscription = context.messages.subscribe()
    try:
        await context.socket.send_xt_message(21, "14_2", {})
        response = await subscription.wait_for(
            lambda message: message.get("_cmd") == "14_2",
            timeout=RESPONSE_TIMEOUT_SECONDS,
        )
    finally:
        subscription.close()

    if response.get("msg"):
        raise RuntimeError(f"道具查询失败：{response['msg']}")
    items = response.get("userms")
    if not isinstance(items, list):
        raise RuntimeError("道具查询回包缺少有效的 userms 列表")

    count = 0
    for item in items:
        if (not isinstance(item, dict) or type(item.get("mi")) is not int
                or type(item.get("qt")) is not int or item["qt"] < 0):
            raise RuntimeError("道具查询回包包含无效的 mi/qt 记录")
        if item["mi"] == item_id:
            count = item["qt"]
    return count
