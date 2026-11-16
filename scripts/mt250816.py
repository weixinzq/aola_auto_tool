"""MT250816: keep the larger score for the requested stage."""
from __future__ import annotations

import asyncio

from src.messaging.dispatcher import DEFAULT_MESSAGE_DELAY, end_previous_battle

SCRIPT_NAME = "四象"
SCRIPT_DESCRIPTION = "读取历史最佳与本次值，并发送最终选择；参数stage（数字，1～4）选择关卡。"

EXT_ID = 42
PANEL_COMMAND = "MT250816_panel"
PANEL_TIMEOUT_SECONDS = 30.0


async def run(context) -> None:
    stage = context.params.get("stage")
    if type(stage) is not int or not 1 <= stage <= 4:
        raise ValueError("stage 必须是 1～4 的整数（关卡编号）")

    subscription = context.messages.subscribe()
    try:
        await context.socket.send_xt_message(EXT_ID, PANEL_COMMAND, {})
        panel = await subscription.wait_for(
            lambda message: message.get("_cmd") == PANEL_COMMAND,
            timeout=PANEL_TIMEOUT_SECONDS,
        )
    finally:
        subscription.close()

    historical_best, current, keep_type = await keep_mt250816_best(
        context, panel, index=stage - 1
    )
    keep_name = "本次值" if keep_type == 1 else "历史最佳"
    stage_name = "一二三四"[stage - 1]
    context.log(
        f"四象第{stage_name}关:历史最佳={historical_best}，本次值={current},"
        f"选择保留{keep_name}"
    )
    if stage == 1 and context.battle.phase in {"active", "ending"}:
        if not context.battle.end_request_sent:
            await asyncio.sleep(DEFAULT_MESSAGE_DELAY)
        await end_previous_battle(context, 0)


def parse_mt250816_scores(panel: dict, index: int = 0) -> tuple[int, int]:
    """Return ``(historical_best, current)`` from an MT250816 panel push."""
    if panel.get("_cmd") != "MT250816_panel":
        raise ValueError("not an MT250816_panel response")

    entries = panel.get("mt2i")
    pair_start = index * 2
    if not isinstance(entries, list) or pair_start + 1 >= len(entries):
        raise ValueError(f"MT250816 score pair for index={index} is missing")

    def score(entry: object) -> int:
        if not isinstance(entry, str) or "=" not in entry:
            raise ValueError(f"invalid MT250816 score entry: {entry!r}")
        try:
            return int(entry.rsplit("=", 1)[1])
        except ValueError as exc:
            raise ValueError(f"invalid MT250816 score entry: {entry!r}") from exc

    return score(entries[pair_start]), score(entries[pair_start + 1])


def choose_mt250816_type(panel: dict, index: int = 0) -> int:
    """Return 1 to keep the current score, otherwise 0 for historical best."""
    historical_best, current = parse_mt250816_scores(panel, index)
    return 1 if current > historical_best else 0


async def keep_mt250816_best(
    context, panel: dict, index: int = 0
) -> tuple[int, int, int]:
    """Compare the panel scores and send the corresponding keep command."""
    historical_best, current = parse_mt250816_scores(panel, index)
    keep_type = 1 if current > historical_best else 0
    await context.socket.send_xt_message(
        42,
        "MT250816_t2c",
        {"index": index, "type": keep_type},
    )
    return historical_best, current, keep_type
