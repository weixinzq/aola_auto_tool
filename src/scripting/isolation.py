"""Bounded calls to synchronous third-party functions in disposable processes."""
import asyncio
import importlib
import multiprocessing
from multiprocessing.connection import wait
import os
import pickle
import threading


def _stop_with_parent():
    parent = multiprocessing.parent_process()
    if parent is not None:
        wait([parent.sentinel])
        os._exit(1)


def _quiet_stdio():
    descriptor = os.open(os.devnull, os.O_RDWR)
    try:
        for target in (0, 1, 2):
            os.dup2(descriptor, target)
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.SetStdHandle.argtypes = (wintypes.DWORD, wintypes.HANDLE)
            api.SetStdHandle.restype = wintypes.BOOL
            for target, standard in ((0, -10), (1, -11), (2, -12)):
                if not api.SetStdHandle(standard, msvcrt.get_osfhandle(target)):
                    raise ctypes.WinError(ctypes.get_last_error())
    finally:
        os.close(descriptor)


def _invoke(sender, module_name, function_name, payload):
    try:
        _quiet_stdio()
        threading.Thread(target=_stop_with_parent, daemon=True).start()
        args, kwargs = pickle.loads(payload)
        function = getattr(importlib.import_module(module_name), function_name)
        result = function(*args, **kwargs)
        sender.send((True, result))
    except BaseException as error:
        sender.send((False, f"{type(error).__name__}: {error}"))
    finally:
        sender.close()


async def _reap(process, receiver_task):
    if process.is_alive():
        process.kill()
    await asyncio.to_thread(process.join, 5)
    if process.is_alive():
        raise RuntimeError("Isolated function process did not exit after termination")
    # Killing the only sender releases a pending recv, including partial messages.
    await asyncio.gather(receiver_task, return_exceptions=True)
    process.close()


async def call_in_process(module_name, function_name, *args, timeout=30.0, **kwargs):
    """Import and call a synchronous function outside the task's event loop.

    Pass pickleable plain data (including bytes), not contexts, sockets or model
    instances. Return plain data as well to keep third-party imports in the child.
    The module is imported afresh per call. Child stdout/stderr are discarded;
    failures become RuntimeError. Timeout or cancellation terminates and reaps
    the child before returning. The timeout bounds result waiting after process
    startup; argument serialization, startup and cleanup also take time.
    """
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    # Fail before spawning if an argument cannot cross a process boundary.
    payload = pickle.dumps((args, kwargs), protocol=pickle.HIGHEST_PROTOCOL)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_invoke, args=(sender, module_name, function_name, payload))
    receiver_task = None
    try:
        process.start()
        sender.close()
        receiver_task = asyncio.create_task(asyncio.to_thread(receiver.recv))
        try:
            async with asyncio.timeout(timeout):
                success, value = await asyncio.shield(receiver_task)
        except EOFError as error:
            raise RuntimeError(f"{module_name}.{function_name}: child exited without a result") from error
        if not success:
            raise RuntimeError(f"{module_name}.{function_name}: {value}")
        return value
    finally:
        sender.close()
        if receiver_task is not None:
            cleanup = asyncio.create_task(_reap(process, receiver_task))
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            try:
                cleanup.result()
            finally:
                receiver.close()
            if cancelled:
                raise asyncio.CancelledError
        else:
            receiver.close()
            if process.pid is not None:
                process.kill()
                process.join(5)
            process.close()
