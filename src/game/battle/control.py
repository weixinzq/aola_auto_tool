"""Account-local battle deadlines and the signal that abandons one outer round."""
from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from dataclasses import dataclass

from src.game.battle.diagnostics import emit

MATCH_TIMEOUT_SECONDS = 20.0
ENTRY_TIMEOUT_SECONDS = 15.0
MATCH_ENTRY_TIMEOUT_SECONDS = 33.0
WAIT_TIMEOUT_SECONDS = 15.0
logger = logging.getLogger(__name__)


class BattleStateError(RuntimeError):
    """The transport is alive, but the current battle cannot continue."""


class BattleRoundSkipped(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.send_results = ()
        # Set by the enclosing step executor; no dependency on its record types.
        self.step_results = ()

    def prepend(self, results):
        self.send_results = (*results, *self.send_results)


@dataclass
class RoundScope:
    battle: object
    repetition: int
    recovering: bool = False
    match_deadline: float | None = None
    entry_epoch: int | None = None


_round: ContextVar[RoundScope | None] = ContextVar("battle_round", default=None)


def current_round(context):
    scope = _round.get()
    return scope if scope is not None and scope.battle is getattr(context, "battle", context) else None


def begin_round(context, repetition):
    return _round.set(RoundScope(getattr(context, "battle", context), repetition))


def reset_round(token):
    _round.reset(token)


def propagate_script_skip(result):
    # A directly invoked script may own its inner combination. Preserve its signal.
    if result.skipped_repetitions:
        reason = next(step.error for step in result.step_results if step.skipped)
        error = BattleRoundSkipped(reason)
        error.send_results = result.send_results
        raise error


def before_send(context, cmd):
    # Reserve before the await: the socket may deliver a reply before send returns.
    battle = context.battle
    scope = current_round(context)
    if cmd == "SA231229_match":
        if battle.require_match_settlement and (battle.settlement_pending or battle.settlement_ambiguous):
            _stop_unconfirmed(context, "上一场结算尚未确认，禁止开始下一次匹配")
        if battle.match_pending or battle.phase != "idle":
            _stop_unconfirmed(context, "上一轮匹配或战斗尚未确认结束，禁止重复发送匹配")
        battle.clear_match()
        battle.match_pending = True
        battle.match_epoch = battle.battle_epoch
        battle.match_deadline = asyncio.get_running_loop().time() + MATCH_TIMEOUT_SECONDS
        if scope is not None:
            scope.match_deadline = battle.match_deadline
            scope.entry_epoch = None
    elif cmd == "SA231229_dpmr":
        if battle.prepare_deadline is not None or battle.phase != "idle":
            _stop_unconfirmed(context, "已经准备或收到入场消息，禁止重复发送准备")
        battle.prepare_deadline = asyncio.get_running_loop().time() + MATCH_ENTRY_TIMEOUT_SECONDS
    elif cmd == "SA231229_cancle" and battle.match_pending:
        battle.match_cancel_requested = True
    elif scope is not None and cmd == "54_22":
        scope.entry_epoch = None


def _stop_unconfirmed(context, reason):
    emit(context, 'match_unresolved', reason=reason, action='stop')
    context.log(f"[匹配保护] {reason}；停止当前流程，不重新匹配")
    raise BattleStateError(reason)


def assert_connected(context):
    context.assert_automation_allowed()
    if not context.socket.connected or context.messages.disconnected.is_set():
        raise ConnectionError("账号连接已断开")


def _can_escape(battle):
    return battle.phase != "idle" and battle.battle_id is not None and {
        "2303", "2401", "2426", "2402"
    }.issubset(battle.entry_responses)


async def abort_round(context, reason):
    assert_connected(context)
    if context.battle.match_pending:
        await context.auto_battle.abort_pending()
        if context.battle.match_pending:
            _stop_unconfirmed(context, reason)
    scope = current_round(context)
    if scope is None or not scope.recovering:
        if scope is not None:
            scope.recovering = True
        auto = context.auto_battle
        await auto.abort_pending()
        # Pending-action cleanup can yield while a 2403 ends the battle.
        can_escape = _can_escape(context.battle)
        emit(context, 'recovery', reason=reason,
             action='1404_then_skip' if can_escape else 'skip')
        slots = [p.get("battleView", {}).get("slotId") for p in auto.own_players]
        slots = [slot for slot in slots if isinstance(slot, int) and not isinstance(slot, bool) and slot >= 0]
        targets = (slots[:1] or [0, 11]) if can_escape else []
        if can_escape:
            context.log(f"[战斗逃跑] battleId={context.battle.battle_id}；原因：{reason}")
        else:
            context.log(f"[战斗恢复] 战斗已结束或未确认完整入场；不发送 1404；原因：{reason}")
        for slot in targets:
            assert_connected(context)
            if not _can_escape(context.battle):
                break
            await context.socket.send_xt_message(13, "1404", {"turn": 0, "reqPSId": slot})
            context.log(f"[战斗逃跑] 1404 已提交，reqPSId={slot}；不等待回执")
        context.battle.abandon()
        auto.clear_battle()
        number = scope.repetition if scope is not None else 1
        context.log(f"第 {number} 轮恢复跳过：{reason}；本轮后续步骤不再发送")
    raise BattleRoundSkipped(reason)


async def require_idle(context):
    assert_connected(context)
    if context.battle.match_pending:
        _stop_unconfirmed(context, "上一轮匹配或战斗尚未收到结束确认")
    if context.battle.phase != "idle":
        await abort_round(context, context.battle.last_error or f"上一场战斗状态为 {context.battle.phase}")


async def _check_entry(context, previous_epoch):
    assert_connected(context)
    battle = context.battle
    if battle.match_cancelled:
        await abort_round(context, "服务器已确认取消本次匹配")
    if battle.phase == "unknown":
        await abort_round(context, battle.last_error or "战斗状态未知")
    if battle.battle_epoch not in (previous_epoch, previous_epoch + 1):
        await abort_round(context, "等待期间战斗身份已改变")
    if battle.battle_epoch == previous_epoch + 1 and battle.phase == "idle":
        await abort_round(context, "本次战斗在就绪前已经结束")


async def _next(context, subscription, deadline):
    remaining = 1.0 if deadline is None else deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError
    try:
        message = await subscription.wait_for(lambda _message: True, timeout=min(remaining, 1.0))
    except TimeoutError:
        if deadline is not None and asyncio.get_running_loop().time() >= deadline:
            raise
        return {}
    if str(message.get("_cmd", "")) == "2303" and message.get("msg"):
        await abort_round(context, str(message["msg"]))
    return message


async def wait_for_match(context, subscription, previous_epoch, *, allow_direct_entry=True,
                         on_reply=None, check=None, timeout=None):
    battle = context.battle
    timeout_seconds = MATCH_TIMEOUT_SECONDS if timeout is None else timeout
    # before_send records a deadline using the shared default; retain its send origin.
    deadline_offset = timeout_seconds - MATCH_TIMEOUT_SECONDS
    deadline = battle.match_deadline
    deadline = (deadline + deadline_offset if deadline is not None
                else asyncio.get_running_loop().time() + timeout_seconds)
    try:
        while True:
            if check is not None:
                check()
            await _check_entry(context, previous_epoch)
            if battle.match_reply is not None:
                return battle.match_reply
            if allow_direct_entry and context.battle.battle_epoch == previous_epoch + 1:
                return None
            message = await _next(context, subscription, deadline)
            if check is not None:
                check()
            if str(message.get("_cmd", "")) == "SA231229_CmdMatch":
                return message
            if on_reply is not None and str(message.get("_cmd", "")) == "SA231229_match":
                retried = await on_reply(message)
                if battle.match_deadline is not None:
                    deadline = battle.match_deadline + deadline_offset
                elif retried:
                    deadline = asyncio.get_running_loop().time() + timeout_seconds
    except TimeoutError:
        # publish() updates battle synchronously, even if the wait timed out first.
        if check is not None:
            check()
        await _check_entry(context, previous_epoch)
        if battle.match_reply is not None:
            return battle.match_reply
        if allow_direct_entry and battle.battle_epoch == previous_epoch + 1:
            return None
        reason = f"{timeout_seconds:g} 秒内未确认本次匹配成功"
        emit(context, 'match_timeout', reason=reason, action='stop', timeout_s=timeout_seconds)
        _stop_unconfirmed(context, reason)


async def wait_for_entry(context, subscription, previous_epoch, *, ready=None, check=None):
    battle = context.battle
    timeout = MATCH_ENTRY_TIMEOUT_SECONDS if battle.match_pending or battle.prepare_deadline is not None else ENTRY_TIMEOUT_SECONDS
    deadline = battle.prepare_deadline or asyncio.get_running_loop().time() + timeout

    def is_ready():
        if battle.battle_epoch == previous_epoch + 1 and battle.entry_ready and (ready is None or ready()):
            scope = current_round(context)
            if scope is not None:
                scope.entry_epoch = battle.battle_epoch
            return True
        return False

    try:
        while True:
            if check is not None:
                check()
            await _check_entry(context, previous_epoch)
            if is_ready():
                return
            await _next(context, subscription, deadline)
    except TimeoutError:
        if check is not None:
            check()
        await _check_entry(context, previous_epoch)
        if is_ready():
            return
        context.log(f"[战斗超时] {timeout:g} 秒内未确认本次战斗完整就绪")
        emit(context, 'entry_timeout', timeout_s=timeout,
             reason='未确认本次战斗完整就绪')
        await abort_round(context, f"{timeout:g} 秒内未确认本次战斗完整就绪")


async def wait_for_end(context, *, check=None):
    subscription = context.messages.subscribe(max_queue_size=500)
    battle = context.battle
    epoch = None if battle.phase == "entering" else battle.battle_epoch
    # Automatic battles have no total-duration limit. Explicit escape waits stay bounded.
    deadline = None if context.auto_battle.enabled else asyncio.get_running_loop().time() + WAIT_TIMEOUT_SECONDS
    try:
        logger.debug("等待当前战斗结束")
        while True:
            if check is not None:
                check()
            assert_connected(context)
            if battle.phase == "unknown" or battle.last_error:
                await abort_round(context, battle.last_error or "战斗状态未知")
            if epoch is None and battle.phase in ("active", "ending"):
                epoch = battle.battle_epoch
            if epoch is not None and battle.battle_epoch != epoch:
                await abort_round(context, "等待结束期间战斗身份已改变")
            if battle.phase == "idle" and not battle.match_pending:
                logger.debug("当前战斗已结束或不在战斗中，继续消息序列")
                return
            if deadline is None and not context.auto_battle.enabled:
                deadline = asyncio.get_running_loop().time() + WAIT_TIMEOUT_SECONDS
            try:
                await _next(context, subscription, deadline)
            except TimeoutError:
                if check is not None:
                    check()
                context.log(f"[战斗超时] #wait 超过 {WAIT_TIMEOUT_SECONDS:g} 秒（非自动战斗等待）")
                await abort_round(context, f"#wait 超过 {WAIT_TIMEOUT_SECONDS:g} 秒")
    finally:
        subscription.close()
