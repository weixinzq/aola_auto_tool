"""
protocol.py — 奥拉星 H5 客户端高层协议
对照 H5 源码: BootLoader.ts, SocketClient.ts, MemberClient.ts
"""
from __future__ import annotations
import asyncio
import random
from src.network.socket_client import GameSocket

# Extension IDs — 对照 ExtMap.ts
EXT_USER       = 1     # UserExtension: getStartInfo, getCurrentTime
EXT_TEAM       = 2     # TeamExtension: 宠物相关
EXT_BUDDY      = 3     # BuddyManagerExtension: 战斗/好友
EXT_MEMBER     = 9     # MemberExtension: 11_1 (会员权限)
EXT_POCKET     = 13    # PocketMonsterExtension
EXT_MATERIAL   = 21    # MaterialExtension: 物品
EXT_ROB_MEDAL  = 25    # RobMedalArenaExtension: 亲密度
EXT_DAILY_TASK = 30    # DailyTaskExtension: 每日任务
EXT_ACTIVITY   = 1008  # 活动

async def init_game(
    sock: GameSocket, last_login_time: int = 0, delay_ms: int = 100
) -> None:
    """
    游戏初始化 — 对照 BootLoader.ts:77-96
    
    H5 在 logOK 后并行请求服务器时间、会员权限和启动信息。
    $ev (ext=0) 是内部事件，跳过。
    """
    print("  发送初始化命令...")

    await sock.send_xt_message(EXT_USER, "getCurrentTime", {})
    await asyncio.sleep(delay_ms / 1000.0)

    await sock.send_xt_message(EXT_MEMBER, "11_1", {})
    await asyncio.sleep(delay_ms / 1000.0)

    await sock.send_xt_message(
        EXT_USER,
        "getStartInfo",
        {
            "firstLogin": last_login_time < 0,
            "lastLoginTime": last_login_time,
        },
    )
    await asyncio.sleep(delay_ms / 1000.0)
    print("  已发送最小玩家启动初始化序列")


async def send_activity_cmd(
    sock: GameSocket,
    cmd_head: str,
    suffix: str,
    params: dict | None = None,
) -> None:
    """发送活动命令 — ext_id = 42 (HolidayExtension)"""
    cmd = f"{cmd_head}{suffix}"
    await sock.send_xt_message(42, cmd, params or {})
