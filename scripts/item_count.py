"""Query one item's current quantity using frontend script parameters."""
from src.game.items import get_item_count

SCRIPT_NAME = "道具数量查询"
SCRIPT_DESCRIPTION = "参数 item_id,"


async def run(context) -> None:
    item_id = context.params.get("item_id")
    count = await get_item_count(context, item_id)
    context.log(f"[查询结果] 道具 ID={item_id}，当前数量={count}")
