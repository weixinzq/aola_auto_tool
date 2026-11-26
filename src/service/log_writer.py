"""Serialize task persistence and batch high-frequency writes off the event loop."""
import asyncio
from concurrent.futures import Future
import logging
import os
from queue import Empty, Full, Queue
from threading import Lock, Thread
from time import monotonic

from src.service.events import utc_now


logger = logging.getLogger(__name__)


class BatchLogWriter:
    def __init__(self, store, events):
        self.store, self.events = store, events
        self.max_logs = max(0, int(os.environ.get("AOLA_MAX_LOGS_PER_TASK", "50000")))
        self.queue = Queue(maxsize=30000)
        self.thread = None
        self.loop = None
        self.failure = None
        self.dropped_count = 0
        self._reported_drops = 0
        self._drop_lock = Lock()

    def start(self):
        if self.thread is None:
            self.loop = asyncio.get_running_loop()
            self.thread = Thread(target=self._run, name="aola-log-writer", daemon=True)
            self.thread.start()

    def enqueue(self, task_id, account_id, message, *, redacted=False, timestamp=None):
        row = {"task_id": task_id, "account_id": account_id, "message": message,
               "timestamp": timestamp or utc_now()}
        try:
            self.queue.put_nowait(("log", (row, redacted)))
        except Full:
            with self._drop_lock:
                self.dropped_count += 1

    async def _put(self, item):
        try:
            self.queue.put_nowait(item)
        except Full:
            await asyncio.to_thread(self.queue.put, item)

    async def enqueue_steps(self, task_id, account_id, records):
        await self._put(("steps", (task_id, account_id, list(records))))

    async def save_task(self, record):
        result = Future()
        await self._put(("save", (record, result)))
        return await asyncio.shield(asyncio.wrap_future(result))

    def flush(self):
        if self.thread is not None:
            result = Future()
            self.queue.put(("flush", result))
            result.result()

    def close(self):
        if self.thread is not None:
            self.queue.put(("stop", None))
            self.thread.join()
            self.thread = None
        if self.failure is not None:
            raise self.failure

    def _report_drops(self):
        with self._drop_lock:
            dropped = self.dropped_count - self._reported_drops
            self._reported_drops = self.dropped_count
        if dropped:
            logger.warning("日志队列已满，丢弃 %d 条日志", dropped)

    def _run(self):
        rows, steps, dirty_tasks = [], [], set()
        deadline = monotonic() + .2
        next_warning = monotonic() + 5
        batches = 0
        while True:
            try:
                kind, data = self.queue.get(timeout=max(0, deadline - monotonic()))
            except Empty:
                kind, data = "tick", None
            if kind == "log":
                rows.append(data)
            elif kind == "steps":
                steps.append(data)
            if (kind not in {"log", "steps"} or len(rows) + len(steps) >= 500 or monotonic() >= deadline):
                try:
                    if self.failure is None:
                        if rows:
                            for row, redacted in rows:
                                if not redacted:
                                    row["message"] = self.store.redact_log(row["message"])
                            saved = self.store.append_logs_batch([row for row, _ in rows], redacted=True)
                            if self.events.has_subscribers:
                                self.loop.call_soon_threadsafe(self.events.publish_logs, saved)
                            if self.max_logs:
                                dirty_tasks.update(row["task_id"] for row in saved)
                                batches += 1
                        if steps:
                            self.store.append_task_steps_batch(steps)
                        if dirty_tasks and (batches >= 50 or kind == "stop"):
                            self.store.prune_logs(dirty_tasks, self.max_logs)
                            dirty_tasks.clear()
                            batches = 0
                except Exception as exc:
                    self.failure = exc
                    logger.error("任务日志或步骤批量写入失败 (%s)", type(exc).__name__)
                rows.clear()
                steps.clear()
                deadline = monotonic() + .2
            if kind == "save":
                record, result = data
                try:
                    if self.failure is not None:
                        raise self.failure
                    self.store.save_task(record)
                    result.set_result(self.store.get_task_summary(record["task_id"]))
                except Exception as exc:
                    result.set_exception(exc)
            elif kind == "flush":
                if self.failure is None:
                    data.set_result(None)
                else:
                    data.set_exception(self.failure)
            elif kind == "stop":
                self._report_drops()
                return
            if monotonic() >= next_warning:
                self._report_drops()
                next_warning = monotonic() + 5
