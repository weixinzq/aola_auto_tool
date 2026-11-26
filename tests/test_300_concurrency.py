"""Offline regressions for the batched service persistence and IPC paths."""
import asyncio
from collections import Counter
from contextlib import closing
from hashlib import md5
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event, get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import quote, quote_plus

from sqlalchemy import event
from sqlalchemy.exc import OperationalError

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
        await self.app.egress.load()
        with patch.object(self.app.store, "get_egress_group", side_effect=AssertionError("N+1 lookup")), \
             patch.object(self.app.store, "list_egress_groups", wraps=self.app.store.list_egress_groups) as groups:
            self.assertEqual(len(await self.app.egress.list_async()), 12)
            groups.assert_not_called()

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
        await self.app.egress.load()
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

    async def test_save_and_submit_overtake_log_backlog(self):
        writer, store = self.app.log_writer, self.app.store
        original_append, original_save = store.append_logs_batch, store.save_task
        writing_logs, release = Event(), Event()
        queues_at_save = {}
        account_id = self.app.accounts.create({"label": "Priority", "account": "priority-login",
                                               "password": "fixture-secret"})["account_id"]

        def hold_first_batch(*args, **kwargs):
            if not writing_logs.is_set():
                writing_logs.set()
                if not release.wait(10):
                    raise TimeoutError("test did not release log batch")
            return original_append(*args, **kwargs)

        def save(record):
            queues_at_save[record["task_id"]] = writer.log_queue.qsize()
            original_save(record)

        with patch.object(store, "append_logs_batch", side_effect=hold_first_batch), \
             patch.object(store, "save_task", side_effect=save):
            try:
                for index in range(500):
                    writer.enqueue("priority-save", None, str(index))
                self.assertTrue(await asyncio.to_thread(writing_logs.wait, 3))
                for index in range(10500):
                    writer.enqueue("priority-save", None, str(index))
                direct = asyncio.create_task(writer.save_task({"task_id": "priority-save", "state": "succeeded"}))
                submitted = asyncio.create_task(self.app.tasks.submit(TaskSpec(kind="connect", account_ids=[account_id])))
                async with asyncio.timeout(3):
                    while writer.control_queue.qsize() < 2:
                        await asyncio.sleep(.005)
            finally:
                release.set()
            summary, record = await asyncio.wait_for(asyncio.gather(direct, submitted), 3)
            self.assertEqual(summary["state"], "succeeded")
            self.assertGreaterEqual(queues_at_save[summary["task_id"]], 10000)
            self.assertGreaterEqual(queues_at_save[record["task_id"]], 10000)
            cleared = await asyncio.to_thread(self.app.tasks.clear_finished)
            self.assertEqual(cleared["task_ids"], ["priority-save"])
            self.assertEqual(store.get_logs("priority-save"), [])

    async def test_sqlite_lock_retries_and_failed_batches_do_not_poison_writer(self):
        writer, store = self.app.log_writer, self.app.store
        original = store.append_logs_batch
        locked = OperationalError("INSERT", {}, sqlite3.OperationalError("database is locked"))
        attempts = 0

        def lock_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise locked
            return original(*args, **kwargs)

        with patch.object(store, "append_logs_batch", side_effect=lock_once):
            writer.enqueue("recovery", None, "retried")
            await asyncio.to_thread(writer.flush)
        self.assertEqual(attempts, 2)
        self.assertEqual(writer.failed_count, 0)

        with patch.object(store, "append_logs_batch", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertLogs("src.service.log_writer", "ERROR"):
                writer.enqueue("recovery", None, "failed")
                with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"):
                    await asyncio.to_thread(writer.flush)
        self.assertEqual(writer.failed_count, 1)
        writer.enqueue("recovery", None, "later")
        saved = await writer.save_task({"task_id": "recovery", "state": "succeeded"})
        await asyncio.to_thread(writer.flush)
        self.assertEqual(saved["state"], "succeeded")
        self.assertEqual([row["message"] for row in store.get_logs("recovery")], ["retried", "later"])

    async def test_step_persistence_does_not_block_other_worker_frames(self):
        writer, store = self.app.log_writer, self.app.store
        store.add_secret("private-step-value")
        original = store.append_task_steps_batch
        writing_steps, release = Event(), Event()
        write_threads = []

        def hold_step_batch(items):
            write_threads.append(get_ident())
            writing_steps.set()
            if not release.wait(10):
                raise TimeoutError("test did not release step batch")
            return original(items)

        worker = Worker(self.app.workers, "step-fixture")
        worker.lost = AsyncMock()
        reader = worker.control_reader = asyncio.StreamReader()
        other_reply = worker.pending["other"] = asyncio.get_running_loop().create_future()
        disconnected = Event()
        worker.connections["another-account"] = SimpleNamespace(lost=lambda reason: disconnected.set())
        worker.step_callbacks["execution"] = lambda records: writer.enqueue_steps("steps", "first", records)
        reader.feed_data(b'{"event":"steps","request_id":"execution","steps":[{"error":"private-step-value"}]}\n')
        with patch.object(store, "append_task_steps_batch", side_effect=hold_step_batch):
            reading = asyncio.create_task(worker.read())
            try:
                self.assertTrue(await asyncio.to_thread(writing_steps.wait, 3))
                reader.feed_data(b'{"id":"other","ok":true}\n'
                                 b'{"event":"disconnected","account_id":"another-account"}\n')
                reader.feed_eof()
                self.assertTrue((await asyncio.wait_for(other_reply, 2))["ok"])
                await asyncio.wait_for(reading, 2)
                self.assertTrue(disconnected.is_set())
            finally:
                release.set()
                reader.feed_eof()
            await writer.save_task({"task_id": "steps", "state": "succeeded",
                                    "account_results": [{"account_id": "first", "_step_results_streamed": True}]})
        self.assertNotEqual(write_threads, [get_ident()])
        persisted = store.get_task("steps")["account_results"][0]["step_results"]
        self.assertEqual(persisted, [{"error": "<redacted>"}])

    async def test_waking_300_sessions_uses_cache_and_one_ordered_account_query(self):
        for index in range(12):
            group_id = f"cached-{index}"
            self.app.store.save_egress_group(group_id, {"name": group_id, "clash_policy": "DIRECT",
                                                       "enabled": True, "routing_confirmed": True})
            self.app.egress._verified.add(group_id)
            self.app.workers.worker(group_id).exit_ip = f"192.0.2.{index + 1}"
        await self.app.egress.load()

        async def initialize():
            pass

        class Connection:
            online = False

            async def connect(self, retries):
                self.context = SimpleNamespace(wait_for_player_initialization=initialize)
                self.online = True
                return True

            async def disconnect(self):
                self.online = False

        self.app.sessions.connection_factory = lambda spec: Connection()
        ids = [self.app.accounts.create({"label": f"Cached {i}", "account": f"cached-login-{i}",
                                        "password": f"cached-password-{i}"})["account_id"] for i in range(300)]
        queries, loop_thread = [], get_ident()

        def count_query(_conn, _cursor, statement, _parameters, _context, _many):
            if "FROM accounts" in statement:
                queries.append((statement, get_ident()))

        event.listen(self.app.store.engine, "before_cursor_execute", count_query)
        try:
            with patch.object(self.app.store, "list_egress_groups", side_effect=AssertionError("wake queried SQLite")):
                task = await self.app.tasks.submit(TaskSpec(kind="connect", concurrency=300, account_ids=ids[::-1]))
                await asyncio.wait_for(self.app.tasks._runs[task["task_id"]].task, 10)
        finally:
            event.remove(self.app.store.engine, "before_cursor_execute", count_query)
        self.assertEqual(len(queries), 1)
        self.assertIn(" IN (", queries[0][0])
        self.assertNotEqual(queries[0][1], loop_thread)
        record = self.app.tasks.get(task["task_id"])
        self.assertEqual(record["state"], "succeeded")
        self.assertEqual([row["account_id"] for row in record["account_results"]], ids[::-1])
        self.assertEqual(Counter(session.exit_ip for session in self.app.sessions._sessions.values()),
                         {f"192.0.2.{index + 1}": 25 for index in range(12)})

    async def test_active_log_polling_only_flushes_on_request(self):
        account_id = self.app.accounts.create({"label": "Polling", "account": "polling-login",
                                               "password": "fixture-secret"})["account_id"]
        record = await self.app.tasks.submit(TaskSpec(kind="connect", account_ids=[account_id]))
        task_id = record["task_id"]
        run = self.app.tasks._runs[task_id]
        writer = self.app.log_writer
        writer.enqueue(task_id, account_id, "queued log")
        with patch.object(writer, "flush", wraps=writer.flush) as flush:
            await asyncio.to_thread(self.app.tasks.logs, task_id)
            flush.assert_not_called()
            logs = await asyncio.to_thread(self.app.tasks.logs, task_id, flush=True)
            self.assertEqual([row["message"] for row in logs], ["queued log"])
            flush.assert_called_once_with()
            await self.app.tasks.cancel(task_id)
            await run.task
            flush.reset_mock()
            await asyncio.to_thread(self.app.tasks.logs, task_id)
            flush.assert_called_once_with()
