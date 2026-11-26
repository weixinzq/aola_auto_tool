"""Isolated worker with a dedicated control channel and account contexts."""
import argparse
import asyncio
from collections import deque
from contextvars import ContextVar
from datetime import datetime, timezone
import ipaddress
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from pydantic import TypeAdapter

from src.accounts.manager import AccountConnection, AccountSpec
from src.service.contracts import StepSpec, ServiceError
from src.service.execution import MessageService, ScriptRegistry
from src.service.execution_runner import run_execution
from src.service.events import utc_now
from src.service.runtime import Redactor
from src.service.worker_process import actual_executable, bind_linux_parent
from src.service.worker_transport import MESSAGE_LIMIT, open_worker_connection
from src.network.server_probe import probe_server, transport_result
from src.network.proxy import process_proxy, proxy_connector


logger = logging.getLogger(__name__)


async def detect_exit_ip():
    # Independent services avoid treating a blocked IP-check endpoint as a broken exit.
    proxy_url = process_proxy()
    options = {'connector': proxy_connector(proxy_url)} if proxy_url else {}
    async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=12), **options) as client:
        for url, json_response in (("https://api.ipify.org?format=json", True),
                                   ("https://checkip.amazonaws.com", False)):
            try:
                async with client.get(url) as response:
                    response.raise_for_status()
                    value = (await response.json())["ip"] if json_response else (await response.text()).strip()
                    return str(ipaddress.IPv4Address(value))
            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError):
                continue
    raise ServiceError("egress_probe_failed", "出口检测失败，请检查代理节点和公网 IP 服务可用性")


def connection_config(config):
    proxy_url = process_proxy()
    return dict(config, network_mode='proxy' if proxy_url else 'system', proxy_url=proxy_url,
                browser_verification=False)


class WorkerHost:
    def __init__(self, connection_factory=AccountConnection):
        self.factory = connection_factory
        self.connections = {}
        self.probing = set()
        self.commands = {}
        self.registry = ScriptRegistry(MessageService(), lazy=True)
        self.redactor = Redactor(None)
        self.redactor.add(process_proxy())
        if process_proxy():
            self.redactor.add(urlsplit(process_proxy()).password)
        self.account_scope = ContextVar("worker_account", default=None)
        self.control_writer = None
        self.write_lock = asyncio.Lock()
        self._log_lock = asyncio.Lock()
        self._log_ready = asyncio.Event()
        self._log_buffer = deque()
        self._log_stopping = False
        self._log_drops = 0

    async def send(self, message):
        payload = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) > MESSAGE_LIMIT and message.get("event") == "log_batch":
            items = message["items"]
            if len(items) == 1:
                self._log_drops += 1
                return
            middle = len(items) // 2
            await self.send({"event": "log_batch", "items": items[:middle]})
            await self.send({"event": "log_batch", "items": items[middle:]})
            return
        async with self.write_lock:
            if self.control_writer is None or self.control_writer.is_closing():
                return
            self.control_writer.write(payload)
            try:
                await self.control_writer.drain()
            except ConnectionError:
                self.control_writer.close()

    def log(self, message, account_id=None):
        if len(self._log_buffer) >= 5000:
            self._log_drops += 1
            return
        self._log_buffer.append({"account_id": account_id or self.account_scope.get(),
                                 "message": self.redactor.clean(message), "timestamp": utc_now()})
        if len(self._log_buffer) >= 50:
            self._log_ready.set()

    async def _flush_logs(self):
        async with self._log_lock:
            # Keep this drain finite even while other accounts continue logging.
            remaining = len(self._log_buffer)
            while remaining:
                batch = [self._log_buffer.popleft() for _ in range(min(50, remaining))]
                remaining -= len(batch)
                await self.send({"event": "log_batch", "items": batch})

    async def _send_logs(self):
        next_warning = asyncio.get_running_loop().time() + 5
        while not self._log_stopping:
            try:
                await asyncio.wait_for(self._log_ready.wait(), .1)
            except TimeoutError:
                pass
            self._log_ready.clear()
            await self._flush_logs()
            if asyncio.get_running_loop().time() >= next_warning:
                self._report_log_drops()
                next_warning = asyncio.get_running_loop().time() + 5

    def _report_log_drops(self):
        if self._log_drops:
            logger.warning("执行进程日志缓冲区已满或 IPC 消息过大，丢弃 %d 条日志", self._log_drops)
            self._log_drops = 0

    async def operate(self, command, outcome):
        operation, account_id, data = command["op"], command.get("account_id"), command.get("data", {})
        if operation == "hello":
            return {"pid": os.getpid(), "executable": str(actual_executable())}
        if operation == "probe":
            return {"exit_ip": await detect_exit_ip()}
        if operation == "cancel":
            target = self.commands.get(data["request_id"])
            if target is not None and target is not asyncio.current_task():
                target.cancel()
                await asyncio.gather(target, return_exceptions=True)
            return {}
        if operation == "shutdown":
            current = asyncio.current_task()
            pending = [task for task in self.commands.values() if task is not current]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.gather(*(connection.disconnect() for connection in self.connections.values()))
            return {}
        if operation == "server_probe":
            if account_id in self.connections or account_id in self.probing:
                raise ServiceError("worker_account_busy", "账号已经存在于执行进程", 409)
            self.probing.add(account_id)
            config = connection_config(data["config"])
            self.redactor.collect(config)
            try:
                if data.get("start_at"):
                    start = datetime.fromisoformat(data["start_at"])
                    await asyncio.sleep(max(0, (start - datetime.now(timezone.utc)).total_seconds()))
                    if datetime.now(timezone.utc) >= datetime.fromisoformat(data["start_before"]):
                        outcome.update(flash=transport_result(), h5=transport_result())
                        for name in ("flash", "h5"):
                            outcome[name].update(status="missed_start" if name == data["target"]["transport"] else "not_selected",
                                                 error="已超过允许的启动时窗" if name == data["target"]["transport"] else None)
                        self.log("错峰登录已错过启动时窗，未发起游戏认证", account_id)
                        return outcome
                steps = self.registry.build_steps(TypeAdapter(list[StepSpec]).validate_python(data.get("steps", [])))
                await probe_server(config, data["target"], data["timeout"], outcome, self.redactor,
                                   steps=steps, attempts=data.get("attempts", 1), log=self.log, window=data.get("window"))
                return outcome
            finally:
                self.probing.discard(account_id)
        if operation == "connect":
            if account_id in self.connections or account_id in self.probing:
                raise ServiceError("worker_account_busy", "账号已经存在于执行进程", 409)
            config = connection_config(data["config"])
            self.redactor.collect(config)
            connection = self.factory(AccountSpec(data["label"], config))
            self.connections[account_id] = connection
            connected = await connection.connect(retries=1)
            context = connection.context
            if context is not None:
                login = getattr(context, "login_result", None)
                self.redactor.add(getattr(login, "session_id", ""))
                self.redactor.add(getattr(login, "user_id", ""))
                context.log_callback = lambda text: self.log(text, account_id)
                messages = getattr(context, "messages", None)
                if hasattr(messages, "add_disconnect_observer"):
                    messages.add_disconnect_observer(lambda reason: asyncio.create_task(self.send({
                        "event": "disconnected", "account_id": account_id, "reason": self.redactor.clean(reason)})))
            return {"connected": connected, "state": connection.state,
                    "last_error": self.redactor.clean(connection.last_error),
                    "login_block_reason": connection.login_block_reason}
        if operation == "disconnect":
            connection = self.connections.get(account_id)
            if connection is not None:
                await connection.disconnect()
            self.connections.pop(account_id, None)
            return {}
        connection = self.connections.get(account_id)
        if connection is None:
            raise ServiceError("worker_session_missing", "执行进程中的账号会话不存在", 409)
        context = connection.context
        if context is None:
            raise ServiceError("worker_session_missing", "账号尚未连接", 409)
        if operation == "initialize":
            await context.wait_for_player_initialization()
            if not connection.online:
                raise ServiceError("connection_failed", "初始化期间连接已断开")
            return {}
        if operation == "execute":
            self.redactor.collect(data["steps"])
            steps = self.registry.build_steps(TypeAdapter(list[StepSpec]).validate_python(data["steps"]))
            async def send_steps(records):
                for step in records:
                    if step["error"]:
                        step["error"] = self.redactor.clean(step["error"])
                await self.send({"event": "steps", "request_id": command["id"], "steps": records})
                await asyncio.sleep(0)

            await run_execution(account_id, context, steps, outcome,
                                on_steps=send_steps if data.get("stream_results") else None, **data["options"])
            for step in outcome.get("step_results", []):
                if step["error"]:
                    step["error"] = self.redactor.clean(step["error"])
            return outcome
        raise ServiceError("worker_operation", "未知执行操作")

    async def handle(self, command):
        request_id = command["id"]
        token = self.account_scope.set(command.get("account_id"))
        outcome = {"network_write": {"status": "unknown", "count": None}}
        try:
            try:
                try:
                    result = await self.operate(command, outcome)
                    response = {"id": request_id, "ok": True, "data": result}
                except Exception as error:
                    response = {"id": request_id, "ok": False, "data": outcome,
                                "error": {"code": getattr(error, "code", "worker_operation_failed"),
                                          "message": self.redactor.clean(error)}}
                if command["op"] in {"initialize", "execute", "disconnect", "server_probe", "shutdown"}:
                    # Deliver the logs before the parent can release the request's account.
                    await asyncio.shield(self._flush_logs())
            except asyncio.CancelledError:
                await asyncio.shield(self._flush_logs())
                response = {"id": request_id, "ok": False, "cancelled": True, "data": outcome}
            await asyncio.shield(self.send(response))
        finally:
            self.account_scope.reset(token)
            self.commands.pop(request_id, None)

    async def run(self, reader, writer):
        self.control_writer = writer
        log_sender = asyncio.create_task(self._send_logs(), name="worker-log-sender")
        try:
            while line := await reader.readline():
                command = json.loads(line)
                task = asyncio.create_task(self.handle(command))
                self.commands[command["id"]] = task
        finally:
            for task in list(self.commands.values()):
                task.cancel()
            await asyncio.gather(*list(self.commands.values()), return_exceptions=True)
            try:
                await asyncio.gather(*(connection.disconnect() for connection in self.connections.values()), return_exceptions=True)
                self._log_stopping = True
                self._log_ready.set()
                await log_sender
                await self._flush_logs()
            finally:
                self._report_log_drops()
                writer.close()
                await writer.wait_closed()


def main(connection_factory=AccountConnection, *, host_factory=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, required=True)
    arguments = parser.parse_args()
    bind_linux_parent(arguments.parent_pid)

    async def run():
        reader, writer = await open_worker_connection()
        host = host_factory() if host_factory is not None else WorkerHost(connection_factory)
        await host.run(reader, writer)

    asyncio.run(run())


if __name__ == "__main__":
    main()
