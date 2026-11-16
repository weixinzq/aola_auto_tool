"""Arena matching and verification with battle or escape actions."""
from __future__ import annotations

import asyncio
import math
from copy import copy
from datetime import datetime, timedelta, timezone
from time import monotonic
from weakref import WeakKeyDictionary

from src.messaging.parser import SendMessage
from src.messaging.dispatcher import SendResult
from src.game.battle import control as battle_control
from src.network.context import AppContext
from src.scripting.composer import MessageBatchStep, ScriptStep, execute_steps
from src.scripting.loader import InteractionScript, ScriptExecutionError
from aola_accounts import recognize_bytes
SCRIPT_NAME = "自动竞技场"
SCRIPT_DESCRIPTION = '参数: mt , mode'
BEIJING = timezone(timedelta(hours=8))
VERIFY_TIMEOUT_SECONDS = 20.0
VERIFY_REFRESH_INTERVAL_SECONDS = 1.0
SETTLEMENT_TIMEOUT_SECONDS = 20.0
MATCH_PAUSE_SECONDS = 1.0
MATCH_TIMEOUT_SECONDS = 60.0
# 同一待验证过程跨轮保留；验证成功后清理，连接释放时自动清理。
_verification_refresh_remaining = WeakKeyDictionary()
_verification_rejected_codes = WeakKeyDictionary()


async def run(context: AppContext) -> tuple[SendResult, ...]:
    params = getattr(context, 'params', {})
    mt, mode = params.get('mt'), params.get('mode')
    if type(mt) is not int or mt not in (0, 1):
        raise ValueError('mt 必须为数字 0 或 1')
    if mode not in ('battle', 'escape'):
        raise ValueError('mode 必须为文本 battle 或 escape')
    log = context.log
    # 只静音本次脚本传给通用执行器的中间日志，不修改原账号上下文。
    context = copy(context)
    context.log = lambda _message: None
    return await _run(context, log, mt, mode)


async def _run(context: AppContext, log, mt: int, mode: str, *, select_mode=None) -> tuple[SendResult, ...]:
    match_request = SendMessage(id=42, cmd='SA231229_match', param={'mt': mt})
    before_steps = (MessageBatchStep((
        SendMessage(id=42, cmd='AHM230616_1', param={'tid': 302}),
        match_request,
    )),)

    await context.wait_for_player_initialization()
    context.assert_automation_allowed()
    if not context.socket.connected or context.messages.disconnected.is_set():
        raise ConnectionError("账号连接已断开")
    await battle_control.require_idle(context)

    battle_started = None
    battle_finished = None
    battle_unique_id = None
    opponent_id = None
    previous_epoch = context.battle.battle_epoch
    previous_settlement_serial = context.battle.settlement_serial
    # 在前置消息发送之前订阅，保留即时回包；不另开底层接收循环。
    subscription = context.messages.subscribe(max_queue_size=500)
    failures = {}
    limited_reply = None
    verification_in_progress = False
    opponent_fled = False
    accept_match_notifications = False
    failure_reasons = {
        'join_battle_cd': '服务器提示战斗速度过快，要求重新登录；停止任务，不自动重新匹配',
        'SA231229_te': '服务器通知进入战斗失败；停止任务，不自动重新匹配',
    }
    terminal_commands = {*failure_reasons, 'SA231229_CmdReMatch', 'SA231229_fe'}

    def observe_terminal(message):
        nonlocal opponent_fled, limited_reply, battle_started, battle_finished, battle_unique_id
        cmd = str(message.get('_cmd', ''))
        if (cmd == '2303' and battle_started is None and not message.get('msg')
                and type(message.get('battleId')) is int and message['battleId'] != 0):
            battle_started = monotonic()
        elif cmd == '2401' and battle_started is not None and battle_unique_id is None:
            battle_unique_id = message.get('battleUniqueId')
        elif (cmd == '2403' and battle_started is not None and battle_finished is None
                and (message.get('battleUniqueId') is None
                     or (battle_unique_id is not None and message['battleUniqueId'] == battle_unique_id))):
            battle_finished = monotonic()
        if cmd == 'SA231229_CmdMatch':
            _verification_refresh_remaining.pop(context.battle, None)
            _verification_rejected_codes.pop(context.battle, None)
        if cmd in failure_reasons:
            failures[cmd] = failure_reasons[cmd]
        elif cmd in ('SA231229_match', 'SA231229_verify', 'SA231229_verifyRefresh') and message.get('r') in (-3, -11):
            limited_reply = dict(message)
        elif cmd == 'SA231229_CmdReMatch' and accept_match_notifications:
            opponent_fled = True

    def stop_response(message, reason):
        cd = message.get('cd')
        if (not isinstance(cd, bool) and isinstance(cd, (int, float))
                and math.isfinite(cd) and cd > 0):
            reason = f'服务端返回冷却时间 {cd:g} 秒；{reason}'
        raise battle_control.BattleStateError(
            f"{reason}（r={message.get('r')}，cd={message.get('cd')!r}）")

    def check_terminal():
        nonlocal opponent_fled
        if failures:
            raise battle_control.BattleStateError('；'.join(failures.values()))
        if limited_reply is not None and (
                not verification_in_progress or limited_reply.get('_cmd') != 'SA231229_verify'
                or context.battle.match_reply is not None
                or context.battle.battle_epoch != previous_epoch):
            stop_response(limited_reply, '服务端限制请求，停止流程，不重新匹配')
        battle = context.battle
        if battle.settlement_ambiguous:
            raise battle_control.BattleStateError('存在多场未确认的结算，无法判断回包归属，停止任务')
        if (battle.settlement_serial > previous_settlement_serial
                and battle.settlement_reply is not None
                and battle.battle_epoch == previous_epoch and battle.phase == 'idle'
                and battle.match_reply is not None):
            battle.clear_match()
            reason = '尚未入场已收到本轮结算，结束本轮，进入下一轮匹配'
            raise battle_control.BattleRoundSkipped(reason)
        if opponent_fled:
            opponent_fled = False
            if context.battle.battle_epoch != previous_epoch or context.battle.phase != 'idle':
                return
            context.battle.discard_match_settlement()
            context.battle.clear_match()
            reason = '对手在入场前逃跑，结束本轮，进入下一轮匹配'
            raise battle_control.BattleRoundSkipped(reason)

    async def pause_before_match(wait_context: AppContext) -> bool:
        check_terminal()
        battle_control.assert_connected(wait_context)
        if MATCH_PAUSE_SECONDS > 0:
            await asyncio.sleep(MATCH_PAUSE_SECONDS)
        check_terminal()
        battle_control.assert_connected(wait_context)
        if (wait_context.battle.match_reply is not None
                or wait_context.battle.battle_epoch != previous_epoch):
            return False
        return True

    async def prepare_for_battle(wait_context: AppContext) -> tuple[SendResult, ...]:
        check_terminal()
        if wait_context.battle.battle_epoch != previous_epoch:
            return ()
        result = await execute_steps(wait_context.user_id, wait_context, (
            MessageBatchStep((SendMessage(id=42, cmd="SA231229_dpmr", param={}),)),
        ), preserve_current_battle=True, check=check_terminal)
        return result.send_results

    async def wait_for_entry(wait_context: AppContext) -> None:
        await battle_control.wait_for_entry(wait_context, subscription, previous_epoch, check=check_terminal)
        subscription.close()

    async def run_battle(wait_context: AppContext) -> tuple[SendResult, ...]:
        selected_mode = select_mode(opponent_id) if select_mode is not None else mode
        action = (SendMessage(id=13, cmd='1409', param={'useAiType': 1}) if selected_mode == 'battle'
                  else SendMessage(id=13, cmd='1404', param={'turn': 0}))
        result = await execute_steps(wait_context.user_id, wait_context, (
            MessageBatchStep((action, SendMessage(id=-1, cmd='#wait', param={}))),
        ), preserve_current_battle=True, check=check_terminal)
        return result.send_results

    async def wait_for_match(wait_context: AppContext) -> tuple[SendResult, ...]:
        nonlocal opponent_id
        retries = []

        def refresh_remaining(message, *, consumed=False):
            remaining = _verification_refresh_remaining.get(wait_context.battle)
            if 'rr' in message:
                value = message['rr']
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value < 0 or int(value) != value):
                    raise battle_control.BattleStateError('服务端返回的刷新剩余次数 rr 无效，停止流程')
                remaining = int(value)
            elif consumed and remaining is not None:
                remaining = max(0, remaining - 1)
            if remaining is not None:
                _verification_refresh_remaining[wait_context.battle] = remaining
            return remaining

        async def next_verification_reply(subscriber, command, timeout):
            nonlocal limited_reply
            deadline = monotonic() + timeout
            while True:
                check_terminal()
                battle_control.assert_connected(wait_context)
                if (wait_context.battle.match_reply is not None
                        or wait_context.battle.battle_epoch != previous_epoch):
                    return None
                if command == 'SA231229_verify' and limited_reply is not None:
                    reply, limited_reply = limited_reply, None
                    return reply
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    reply = await subscriber.wait_for(lambda item: True, timeout=min(remaining, 1.0))
                except TimeoutError:
                    continue
                check_terminal()
                if (wait_context.battle.match_reply is not None
                        or wait_context.battle.battle_epoch != previous_epoch):
                    return None
                if command == 'SA231229_verify' and limited_reply is not None:
                    reply, limited_reply = limited_reply, None
                    return reply
                if reply.get('_cmd') == command:
                    return reply

        def verification_cooldown(reply):
            result = reply.get('r')
            if (isinstance(result, bool) or not isinstance(result, (int, float))
                    or not math.isfinite(result) or int(result) != result):
                stop_response(reply, '验证回包的 r 无效，停止流程')
            cd = reply.get('cd', 0)
            if result != 1 and cd is None:
                cd = 0
            if (isinstance(cd, bool) or not isinstance(cd, (int, float))
                    or not math.isfinite(cd) or cd < 0):
                stop_response(reply, '验证回包的 cd 无效，停止流程')
            return cd

        async def on_reply(message):
            nonlocal verification_in_progress
            check_terminal()
            if message.get('r') == -10:
                if 'vi' not in message:
                    return False
                battle_control.assert_connected(wait_context)
                pngbyte = message['vi']
                if not isinstance(pngbyte, (bytes, bytearray, memoryview)) or not pngbyte:
                    raise battle_control.BattleStateError('匹配回包未提供有效验证图片，停止流程')
                remaining_refreshes = refresh_remaining(message)
                refresh_count = 0
                while True:
                    check_terminal()
                    battle_control.assert_connected(wait_context)
                    if (wait_context.battle.match_reply is not None
                            or wait_context.battle.battle_epoch != previous_epoch):
                        return False
                    code = recognize_bytes(bytes(pngbyte), mode="alphanumeric", beta=True)
                    check_terminal()
                    if (wait_context.battle.match_reply is not None
                            or wait_context.battle.battle_epoch != previous_epoch):
                        return False
                    if len(code) != 4:
                        wait_context.battle.clear_match()
                        raise battle_control.BattleRoundSkipped('验证内容长度不是 4 位，跳过本轮')
                    if code in _verification_rejected_codes.get(wait_context.battle, ()):
                        raise battle_control.BattleStateError('识别结果已被服务端拒绝，停止重复提交')
                    verification = wait_context.messages.subscribe(max_queue_size=500)
                    verification_in_progress = True
                    try:
                        await wait_context.socket.send_xt_message(42, 'SA231229_verify', {'mt': mt, 'code': code})
                        try:
                            reply = await next_verification_reply(verification, 'SA231229_verify',
                                                                  VERIFY_TIMEOUT_SECONDS)
                        except TimeoutError:
                            raise battle_control.BattleStateError(
                                f'{VERIFY_TIMEOUT_SECONDS:g} 秒内未收到验证回包，停止流程，不重新匹配')
                        if reply is None:
                            return False
                        cd = verification_cooldown(reply)
                        verification_result = reply['r']
                        until = monotonic() + cd
                        while monotonic() < until:
                            try:
                                late = await next_verification_reply(
                                    verification, 'SA231229_verify', min(1.0, max(0, until - monotonic())))
                            except TimeoutError:
                                continue
                            if late is None:
                                return False
                            late_cd = verification_cooldown(late)
                            if late['r'] != 1:
                                verification_result = late['r']
                                until = max(until, monotonic() + late_cd)
                    finally:
                        verification.close()
                        verification_in_progress = False
                    check_terminal()
                    battle_control.assert_connected(wait_context)
                    if verification_result == 1:
                        _verification_refresh_remaining.pop(wait_context.battle, None)
                        _verification_rejected_codes.pop(wait_context.battle, None)
                        break
                    if verification_result in (0, -3):
                        _verification_rejected_codes.setdefault(wait_context.battle, set()).add(code)
                    if remaining_refreshes == 0 or refresh_count >= 9:
                        raise battle_control.BattleStateError('验证未通过，已无可用刷新次数，停止流程')
                    if VERIFY_REFRESH_INTERVAL_SECONDS > 0:
                        await asyncio.sleep(VERIFY_REFRESH_INTERVAL_SECONDS)
                    check_terminal()
                    battle_control.assert_connected(wait_context)
                    if (wait_context.battle.match_reply is not None
                            or wait_context.battle.battle_epoch != previous_epoch):
                        return False
                    refresh = wait_context.messages.subscribe(max_queue_size=500)
                    try:
                        refresh_remaining({}, consumed=True)
                        await wait_context.socket.send_xt_message(42, 'SA231229_verifyRefresh', {'mt': mt})
                        try:
                            reply = await next_verification_reply(refresh, 'SA231229_verifyRefresh',
                                                                  VERIFY_TIMEOUT_SECONDS)
                        except TimeoutError:
                            raise battle_control.BattleStateError('刷新验证图片超时，停止流程')
                        if reply is None:
                            return False
                        if reply.get('r') != 1:
                            stop_response(reply, '刷新验证图片失败，停止流程')
                        remaining_refreshes = refresh_remaining(reply)
                        pngbyte = reply.get('vi')
                        if not isinstance(pngbyte, (bytes, bytearray, memoryview)) or not pngbyte:
                            raise battle_control.BattleStateError('刷新未返回有效验证图片，停止流程')
                        refresh_count += 1
                    finally:
                        refresh.close()
                if not await pause_before_match(wait_context):
                    return False
                battle_control.before_send(wait_context, 'SA231229_match')
                await wait_context.socket.send_xt_message(42, 'SA231229_match', {'mt': mt})
                return True
            if message.get('r') in (-3, -11):
                stop_response(message, '匹配请求被限制，停止流程，不重新匹配')
            if message.get('r') == -8:
                raise battle_control.BattleStateError('匹配请求返回 r=-8，需确认服务端状态')
            if message.get('r') != -9:
                return False
            cd = message.get('cd')
            if isinstance(cd, bool) or not isinstance(cd, (int, float)) or not math.isfinite(cd):
                raise battle_control.BattleRoundSkipped(f"匹配拥堵回包的 cd 无效：{cd!r}")

            # A zero cooldown must not become a tight request loop.
            seconds = cd if cd > 0 else 1.0
            until = monotonic() + seconds
            while (monotonic() < until and wait_context.battle.match_reply is None
                   and wait_context.battle.battle_epoch == previous_epoch):
                check_terminal()
                battle_control.assert_connected(wait_context)
                await asyncio.sleep(min(1.0, max(0, until - monotonic())))
            check_terminal()
            battle_control.assert_connected(wait_context)
            if (wait_context.battle.match_reply is not None
                    or wait_context.battle.battle_epoch != previous_epoch):
                return False
            if not await pause_before_match(wait_context):
                return False
            result = await execute_steps(wait_context.user_id, wait_context, (
                MessageBatchStep((match_request,)),
            ), preserve_current_battle=True, check=check_terminal)
            retries.extend(result.send_results)
            return True

        try:
            reply = await battle_control.wait_for_match(
                wait_context, subscription, previous_epoch,
                allow_direct_entry=False, on_reply=on_reply, check=check_terminal,
                timeout=MATCH_TIMEOUT_SECONDS)
        except battle_control.BattleRoundSkipped as error:
            error.prepend(retries)
            raise
        except battle_control.BattleStateError as error:
            raise ScriptExecutionError(str(error), tuple(retries)) from error
        except ScriptExecutionError as error:
            raise ScriptExecutionError(str(error), (*retries, *error.send_results)) from error
        opponent_id = next((int(value) for value in reply.get('ids', [])
                            if int(value) != int(wait_context.user_id)), None)
        scope = battle_control.current_round(wait_context)
        number = scope.repetition if scope else 1
        stamp = datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M:%S')
        log(f'[匹配] {stamp} 轮数={number} 对手id={opponent_id}')
        return tuple(retries)

    try:
        context.messages.add_observer(observe_terminal)
        context.battle.require_match_settlement = True
        check_terminal()
        if context.battle.settlement_pending:
            deadline = monotonic() + SETTLEMENT_TIMEOUT_SECONDS
            while context.battle.settlement_pending:
                check_terminal()
                battle_control.assert_connected(context)
                remaining = deadline - monotonic()
                if remaining <= 0:
                    reason = f'{SETTLEMENT_TIMEOUT_SECONDS:g} 秒内未确认上一场结算，停止任务，不开始新的匹配'
                    raise battle_control.BattleStateError(reason)
                try:
                    await subscription.wait_for(lambda _message: True, timeout=min(remaining, 1.0))
                except TimeoutError:
                    continue
            check_terminal()
        accept_match_notifications = True
        before_steps = before_steps if await pause_before_match(context) else ()
        steps = (
            *before_steps,
            ScriptStep(
                InteractionScript(
                    module_name=f"{__name__}.wait_for_match",
                    name="等待本次匹配成功",
                    description="订阅并等待 SA231229_CmdMatch 匹配成功回包",
                    run=wait_for_match,
                )
            ),
            ScriptStep(
                InteractionScript(
                    module_name=f"{__name__}.prepare_for_battle",
                    name="准备本次战斗",
                    description="尚未收到本次入场包时发送 SA231229_dpmr",
                    run=prepare_for_battle,
                )
            ),
            ScriptStep(
                InteractionScript(
                    module_name=f"{__name__}.wait_for_entry",
                    name="等待本次战斗就绪",
                    description="等待 2303/2401/2426/2402",
                    run=wait_for_entry,
                )
            ),
            ScriptStep(
                InteractionScript(
                    module_name=f"{__name__}.run_battle",
                    name="执行本次战斗",
                    description="发送战斗动作并等待结束",
                    run=run_battle,
                )
            ),
        )
        try:
            result = await execute_steps(
                context.user_id, context, steps, preserve_current_battle=True, check=check_terminal
            )
        except ScriptExecutionError as error:
            failure = next(step for step in error.step_results if not step.success)
            raise ScriptExecutionError(
                f"步骤 {failure.step}（{failure.description}）失败：{failure.error}",
                error.send_results,
            )
        if context.battle.battle_epoch == previous_epoch + 1 and context.battle.phase == 'idle':
            if battle_started is not None and battle_finished is not None:
                log(f'[战斗结束] 耗时={battle_finished - battle_started:.3f}秒')
        return result.send_results
    finally:
        context.messages.remove_observer(observe_terminal)
        subscription.close()
