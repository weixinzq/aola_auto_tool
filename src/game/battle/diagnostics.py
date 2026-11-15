"""Opt-in diagnostics for one account during a script invocation."""
import asyncio
from contextlib import contextmanager


def emit(context, event, **fields):
    callback = getattr(context.battle, 'diagnostic_callback', None)
    if callback is not None:
        callback(event, **fields)


@contextmanager
def capture(context, callback):
    socket, router, battle = context.socket, context.messages, context.battle
    original_send = socket.send_xt_message
    previous = battle.diagnostic_callback
    active = True
    received_entry = set()

    def record(event, **fields):
        if active:
            callback(event, **fields)

    async def send(ext_id, cmd, params, *args, **kwargs):
        traced = cmd in {'AHM230616_1', 'SA231229_match', 'SA231229_dpmr',
                         '1404', '1409', 'SA231229_cancle'}
        fields = {'ext_id': ext_id, 'cmd': cmd,
                  'params': {key: value for key, value in params.items()
                             if key in {'tid', 'mt', 'turn', 'reqPSId', 'useAiType'}}}
        if traced:
            record('send_start', **fields)
        try:
            result = await original_send(ext_id, cmd, params, *args, **kwargs)
        except asyncio.CancelledError:
            if traced:
                record('send_cancelled', **fields, outcome='unknown')
            raise
        except Exception as error:
            if traced:
                record('send_failed', **fields, reason=f'{type(error).__name__}: {error}')
            raise
        if traced:
            record('send_returned', **fields)
        return result

    def receive(message):
        cmd = str(message.get('_cmd', ''))
        if cmd in {'2303', '2401', '2426', '2402'}:
            if cmd in received_entry and not message.get('msg'):
                return
            received_entry.add(cmd)
        elif cmd not in {'SA231229_dpmr', 'SA231229_cancle', '2403'}:
            return
        record('received', cmd=cmd, reply={key: message[key] for key in
               ('r', 'msg', 'battleId', 'battleUniqueId', 'pt') if key in message})

    def disconnected(reason):
        record('disconnected', reason=reason)

    battle.diagnostic_callback = record
    socket.send_xt_message = send
    router.add_observer(receive)
    router.add_disconnect_observer(disconnected)
    try:
        yield
    finally:
        active = False
        router.remove_disconnect_observer(disconnected)
        router.remove_observer(receive)
        socket.send_xt_message = original_send
        battle.diagnostic_callback = previous
