"""Temporary account-local packet evidence, retained until disconnect."""
import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from src.config import PROJECT_ROOT


LOG_DIRECTORY = PROJECT_ROOT / 'logs' / 'battle-messages'


def _json_default(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {'$bytes_hex': bytes(value).hex()}
    raise TypeError(f'无法保存消息字段类型：{type(value).__name__}')


def _related_send(ext_id, cmd):
    return (ext_id in {13, 15, 16} or cmd.startswith(('SA231229_', 'MessageBox_'))
            or cmd in {'AHM230616_1', 'joinKO', 'logKO', 'preFightLoad'}
            or (cmd.isdigit() and (1400 <= int(cmd) < 1500 or 2300 <= int(cmd) < 2500)))


class BattleMessageLog:
    def __init__(self, context, *, quiet=False):
        self.context = context
        self.quiet = quiet
        self._log = context.log
        self.closed = False
        self.script = ''
        self.match_number = 0
        self.run_id = ''
        self.sequence = 0
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        # Role IDs in filenames would be redacted by the task-log transport.
        self.path = LOG_DIRECTORY / f'battle-{stamp}-{uuid4().hex[:8]}.jsonl'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Flush each complete JSON line, including immediately before a send.
        self._stream = self.path.open('x', encoding='utf-8', buffering=1)
        context.socket.message_trace = self.sent
        context.messages.message_trace = self.received
        context.messages.add_disconnect_observer(self.close)

    def start_round(self, script, match_number):
        self.script = script
        self.match_number = match_number
        if match_number == 1 or not self.run_id:
            self.run_id = uuid4().hex
        self._write('round_start')

    def _write(self, event, **fields):
        if self.closed:
            return
        self.sequence += 1
        battle = self.context.battle
        record = {
            'at': datetime.now(timezone.utc).isoformat(), 'sequence': self.sequence,
            'user_id': str(self.context.user_id), 'run_id': self.run_id,
            'script': self.script, 'match_number': self.match_number,
            'event': event, 'battle_epoch': battle.battle_epoch,
            'battle_id': battle.battle_id, 'phase': battle.phase,
            'match_pending': battle.match_pending, **fields,
            'turn': battle.current_turn,
            'auto_enabled': self.context.auto_battle.enabled,
            'extra_action_delay_s': self.context.auto_battle.extra_action_delay_seconds,
        }
        try:
            self._stream.write(json.dumps(record, ensure_ascii=False,
                                          separators=(',', ':'), default=_json_default) + '\n')
        except (OSError, TypeError, ValueError) as error:
            self._detach()
            if self.quiet:
                logging.getLogger(__name__).warning('收发日志保存失败：%s；%s', self.path, error)
            else:
                self._log(f'[收发日志] 保存失败，已停止记录：{self.path}；{error}')

    def received(self, message, *, filtered=False):
        # Keep unknown notifications too: the opponent-disconnect command is unknown.
        self._write('received', direction='receive', filtered=filtered, message=message)

    def sent(self, event, *, ext_id, cmd, **fields):
        if _related_send(ext_id, str(cmd)):
            self._write(event, direction='send', ext_id=ext_id, cmd=cmd, **fields)

    def close(self, reason='账号连接已关闭'):
        if not self.closed:
            self._write('closed', reason=reason)
            self._detach()

    def _detach(self):
        if self.closed:
            return
        self.closed = True
        socket, router = self.context.socket, self.context.messages
        if socket.message_trace == self.sent:
            socket.message_trace = None
        if router.message_trace == self.received:
            router.message_trace = None
        router.remove_disconnect_observer(self.close)
        try:
            self._stream.close()
        except OSError:
            pass
