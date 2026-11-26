"""Composition root shared by service hosts and offline tests."""
import asyncio
from pathlib import Path

from src.accounts.manager import AccountConnection
from src.service.accounts import AccountService
from src.service.events import EventHub
from src.service.execution import MessageService, ScriptRegistry
from src.service.log_writer import BatchLogWriter
from src.service.store import Store
from src.service.runtime import SessionService
from src.service.tasks import TaskService
from src.service.instance_lock import InstanceLock
from src.service.workers import WorkerPool
from src.service.egress import EgressService
from src.service.mihomo import Mihomo


class Application:
    def __init__(self, data_dir: Path, *, connection_factory=AccountConnection, executor=None, max_online=100,
                 worker_module="src.service.worker_host", login_concurrency=None,
                 mihomo_config=None, mihomo_binary=None):
        self.store = Store(Path(data_dir))
        self.events = EventHub()
        self.log_writer = BatchLogWriter(self.store, self.events)
        self.messages = MessageService()
        self.scripts = ScriptRegistry(self.messages)
        self.mihomo = Mihomo(mihomo_config, self.store.directory, mihomo_binary) if mihomo_config else None
        self.workers = WorkerPool(self.store, self.events, module=worker_module, local_factory=connection_factory,
                                  mihomo=self.mihomo)
        self.sessions = SessionService(self.store, self.events, connection_factory=self.workers.connection,
                                       max_online=max_online, login_concurrency=login_concurrency)
        self.egress = EgressService(self.store, self.sessions, self.workers, self.events)
        self.sessions.egress = self.egress
        self.workers.on_lost = self.egress.failed_async
        if self.mihomo:
            self.mihomo.on_lost = self.proxy_lost
        self.accounts = AccountService(self.store, self.sessions, self.events, self.egress)
        self.tasks = TaskService(self.store, self.sessions, self.scripts, self.messages, self.events,
                                 executor=executor, log_writer=self.log_writer)
        self._started = False
        self._lock = InstanceLock(self.store.directory / "service.lock")

    async def start(self):
        if not self._started:
            self._lock.acquire()
            try:
                self.events.bind()
                await asyncio.to_thread(self.store.interrupt_pending)
                await self.egress.load()
                if self.mihomo:
                    await self.mihomo.start()
                    await self.egress.import_nodes()
                self.log_writer.start()
            except BaseException:
                if self.mihomo:
                    await self.mihomo.close()
                self._lock.release()
                self.store.close()
                raise
            self._started = True

    async def proxy_lost(self):
        for group in self.egress.list():
            if group['clash_policy'] != 'DIRECT':
                await self.egress.failed_async(group['group_id'])
                worker = self.workers.workers.get(group['group_id'])
                if worker:
                    await worker.lost('Mihomo 已退出，代理连接已停止')

    async def close(self):
        try:
            try:
                await self.tasks.close()
            finally:
                try:
                    await self.sessions.shutdown()
                finally:
                    try:
                        await self.workers.close()
                    finally:
                        if self.mihomo:
                            await self.mihomo.close()
        finally:
            try:
                await asyncio.to_thread(self.log_writer.close)
            finally:
                self.store.close()
                self._lock.release()
