"""Offline regressions for the batched service persistence and IPC paths."""
import asyncio
from contextlib import closing
from hashlib import md5
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import quote, quote_plus

from src.interfaces.http import SessionInput
from src.service.application import Application
from src.service.contracts import TaskSpec
from src.service.runtime import Redactor
from src.service.worker_host import WorkerHost
from src.service.workers import RemoteContext, Worker


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.app = Application(self.path)
        await self.app.start()
        self.addAsyncCleanup(self.app.close)

    async def test_immediate_shutdown_commits_all_5000_accepted_logs(self):
        for number in range(5000):
            self.app.tasks._log("flush-check", None, str(number))
        await self.app.close()
        self.assertEqual(self.app.log_writer.dropped_count, 0)
        with closing(sqlite3.connect(self.path / "service.sqlite3")) as connection:
            rows = connection.execute(
                "SELECT message FROM logs WHERE task_id=? ORDER BY id", ("flush-check",)).fetchall()
        self.assertEqual(rows, [(str(number),) for number in range(5000)])

    async def test_log_retention_keeps_newest_rows_per_task(self):
        self.app.log_writer.max_logs = 3
        for number in range(8):
            for task_id in ("first", "second"):
                self.app.tasks._log(task_id, None, str(number))
        await self.app.close()
        with closing(sqlite3.connect(self.path / "service.sqlite3")) as connection:
            for task_id in ("first", "second"):
                rows = connection.execute(
                    "SELECT message FROM logs WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
                self.assertEqual(rows, [("5",), ("6",), ("7",)])

    async def test_worker_and_parent_redact_once_before_sqlite_and_events(self):
        account, password = "fixture+account@example.invalid", "fixture /p+ass"
        credentials = {"account": account, "password": password}
        secrets = [account, password, md5(password.encode()).hexdigest(), quote_plus(password),
                   quote(password, safe=""), "unknown-sid", "unknown-token", "unknown-session", "unknown-key"]
        text = " ".join(secrets[:5]) + (
            " sid=unknown-sid token:unknown-token session_id=unknown-session <loginkey>unknown-key</loginkey>")
        for value in credentials.values():
            self.app.store.add_secret(value)

        worker = Worker(self.app.workers, "fixture")
        worker.lost = AsyncMock()
        reader = worker.control_reader = asyncio.StreamReader()
        host = WorkerHost()
        host.redactor = Redactor(None, credentials)
        draining = False

        async def drain():
            nonlocal draining
            self.assertFalse(draining)
            draining = True
            await asyncio.sleep(0)
            draining = False

        host.control_writer = SimpleNamespace(
            write=reader.feed_data, is_closing=lambda: False, drain=drain)
        session = SimpleNamespace(account_id="fixture-id", owner_task_id="redaction-check")
        remote = RemoteContext(SimpleNamespace(account_id=session.account_id))
        worker.connections[session.account_id] = SimpleNamespace(context=remote)
        remote.log_callback = lambda message, **options: self.app.sessions._log(session, message, **options)
        queue = self.app.events.subscribe(task_id="redaction-check", batch_logs=True)
        self.addCleanup(self.app.events.unsubscribe, queue)

        with patch.object(self.app.store, "redact_log", wraps=self.app.store.redact_log) as redact:
            host.log(text, session.account_id)
            await asyncio.gather(host._flush_logs(), host.send({"id": "response", "ok": True}))
            await host.send({"event": "log", "account_id": session.account_id,
                             "message": host.redactor.clean(text)})
            reader.feed_eof()
            await worker.read()
            self.app.tasks._log(session.owner_task_id, session.account_id, text)
            await asyncio.to_thread(self.app.log_writer.flush)
            self.assertEqual(redact.call_count, 1)

        entries = self.app.store.get_logs(session.owner_task_id)
        published = []
        while not queue.empty():
            published.extend(event["data"] for event in queue.get_nowait())
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries, published)
        for entry in entries:
            for secret in secrets:
                self.assertNotIn(secret, entry["message"])

    async def test_egress_list_uses_one_query_and_active_get_does_not_save(self):
        for index in range(12):
            self.app.store.save_egress_group(f"group-{index}", {"name": f"Fixture {index}",
                "clash_policy": "DIRECT", "routing_confirmed": False, "enabled": False})
        with patch.object(self.app.store, "get_egress_group", side_effect=AssertionError("N+1 lookup")), \
             patch.object(self.app.store, "list_egress_groups", wraps=self.app.store.list_egress_groups) as groups:
            self.assertEqual(len(await self.app.egress.list_async()), 12)
            groups.assert_called_once()

        account = self.app.accounts.create({"label": "Fixture", "account": "fixture-account",
                                            "password": "fixture-password"})
        record = await self.app.tasks.submit(TaskSpec(kind="connect", account_ids=[account["account_id"]]))
        run = self.app.tasks._runs[record["task_id"]]
        await asyncio.sleep(0)
        with patch.object(self.app.store, "save_task", side_effect=AssertionError("read wrote a task")):
            current = await self.app.tasks.get_async(record["task_id"])
            self.assertEqual(current["state"], "running")
            current["account_results"].clear()
            self.assertEqual(len((await self.app.tasks.get_async(record["task_id"]))["account_results"]), 1)
            run.record["spec"]["probe_note"] = "newly-registered-secret"
            self.app.tasks._publish(run)
            rendered = await self.app.tasks.get_async(record["task_id"], render=json.dumps)
            self.assertIn("newly-registered-secret", rendered)
            self.app.store.add_secret("newly-registered-secret")
            rendered = await self.app.tasks.get_async(record["task_id"], render=json.dumps)
            self.assertNotIn("newly-registered-secret", rendered)
        await self.app.tasks.cancel(record["task_id"])
        await run.task

    async def test_initialization_does_not_hold_login_slot(self):
        started, release = asyncio.Event(), asyncio.Event()
        initialized = 0

        async def initialize():
            nonlocal initialized
            initialized += 1
            if initialized == 2:
                started.set()
            await release.wait()

        class Connection:
            context = None
            online = False

            async def connect(self, retries):
                self.context = SimpleNamespace(wait_for_player_initialization=initialize)
                self.online = True
                return True

            async def disconnect(self):
                self.online = False

        self.app.sessions._login_slots = asyncio.Semaphore(1)
        self.app.sessions.connection_factory = lambda spec: Connection()
        self.app.store.save_egress_group("fixture", {"name": "Fixture", "clash_policy": "DIRECT",
                                                       "enabled": True, "routing_confirmed": True})
        self.app.egress._verified.add("fixture")
        self.app.workers.worker("fixture").exit_ip = "192.0.2.1"
        ids = [self.app.accounts.create({"label": f"Fixture {i}", "account": f"fixture-{i}",
                                         "password": f"fixture-password-{i}"})["account_id"] for i in range(2)]
        task = await self.app.tasks.submit(TaskSpec(kind="connect", concurrency=2, account_ids=ids))
        run = self.app.tasks._runs[task["task_id"]]
        try:
            await asyncio.wait_for(started.wait(), 3)
        finally:
            release.set()
        await run.task
        self.assertEqual(self.app.tasks.get(task["task_id"])["state"], "succeeded")
        self.assertEqual(SessionInput(account_ids=ids, concurrency=500).concurrency, 500)
