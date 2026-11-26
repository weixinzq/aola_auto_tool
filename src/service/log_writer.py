"""Prioritize task state and step persistence over batched service logs."""
import asyncio
from concurrent.futures import Future
import logging
import os
from queue import Empty, Full, Queue
import sqlite3
from threading import Event, Lock, Thread
from time import monotonic, sleep

from sqlalchemy.exc import OperationalError

from src.service.events import utc_now


logger = logging.getLogger(__name__)
_BATCH_SIZE = 500
_BATCH_INTERVAL = .2
_RETRY_DELAYS = (.05, .15, .3)


class BatchLogWriter:
    def __init__(self, store, events):
        self.store, self.events = store, events
        self.max_logs = max(0, int(os.environ.get("AOLA_MAX_LOGS_PER_TASK", "50000")))
        self.control_queue = Queue()
        self.log_queue = Queue(maxsize=30000)
        self.thread = None
        self.loop = None
        self.dropped_count = 0
        self.failed_count = 0
        self._reported_drops = 0
        self._accepted_logs = 0
        self._closing = False
        self._lock = Lock()
        self._wake = Event()

    def start(self):
        if self.thread is None:
            self.loop = asyncio.get_running_loop()
            self.thread = Thread(target=self._run, name="aola-log-writer", daemon=True)
            self.thread.start()

    def enqueue(self, task_id, account_id, message, *, redacted=False, timestamp=None):
        row = {"task_id": task_id, "account_id": account_id, "message": message,
               "timestamp": timestamp or utc_now()}
        with self._lock:
            if self._closing:
                return
            try:
                self.log_queue.put_nowait((row, redacted))
            except Full:
                self.dropped_count += 1
                return
            self._accepted_logs += 1
            if self.log_queue.qsize() in (1, _BATCH_SIZE):
                self._wake.set()

    def _control(self, kind, data):
        with self._lock:
            if self._closing:
                raise RuntimeError("Task writer is closing")
            self.control_queue.put_nowait((kind, data))
            self._wake.set()

    def enqueue_steps(self, task_id, account_id, records):
        self._control("steps", (task_id, account_id, list(records)))

    async def save_task(self, record):
        result = Future()
        self._control("save", (record, result))
        return await asyncio.shield(asyncio.wrap_future(result))

    def flush(self, *, logs=True):
        with self._lock:
            thread = self.thread
            if thread is None:
                return
            result = None
            if not self._closing:
                result = Future()
                target = self._accepted_logs if logs else None
                self.control_queue.put_nowait(("flush", (target, result)))
                self._wake.set()
        if result is None:
            thread.join()
        else:
            result.result()

    def close(self):
        with self._lock:
            thread = self.thread
            if thread is None:
                return
            if not self._closing:
                self._closing = True
                self.control_queue.put_nowait(("stop", None))
                self._wake.set()
        thread.join()
        with self._lock:
            self.thread = None

    def _report_drops(self):
        with self._lock:
            dropped = self.dropped_count - self._reported_drops
            self._reported_drops = self.dropped_count
        if dropped:
            logger.warning("日志队列已满，丢弃 %d 条日志", dropped)

    @staticmethod
    def _write(operation, *args, **kwargs):
        for delay in (*_RETRY_DELAYS, None):
            try:
                return operation(*args, **kwargs)
            except (sqlite3.OperationalError, OperationalError) as exc:
                cause = exc.orig if isinstance(exc, OperationalError) else exc
                code = getattr(cause, "sqlite_errorcode", 0) & 255
                locked = code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or (
                    not code and "locked" in str(cause).lower())
                if delay is None or not locked:
                    raise
                sleep(delay)

    def _failed(self, kind, exc, result=None):
        self.failed_count += 1
        logger.error("持久化 %s 写入失败 (%s)", kind, type(exc).__name__)
        if result is not None:
            result.set_exception(exc)

    def _write_steps(self, steps):
        try:
            if steps:
                self._write(self.store.append_task_steps_batch, steps)
        except Exception as exc:
            self._failed("steps", exc)
            return exc
        finally:
            steps.clear()

    def _write_logs(self, rows):
        batch = []
        for row, redacted in rows:
            if not redacted:
                row["message"] = self.store.redact_log(row["message"])
            batch.append(row)
        saved = self._write(self.store.append_logs_batch, batch, redacted=True)
        if self.events.has_subscribers:
            self.loop.call_soon_threadsafe(self.events.publish_logs, saved)
        return saved

    def _run(self):
        processed = 0
        flushes = []
        steps, dirty_tasks = [], set()
        stopping = False
        deadline = None
        next_warning = monotonic() + 5
        batches = 0
        while True:
            self._wake.clear()
            step_error = None
            while True:
                try:
                    kind, data = self.control_queue.get_nowait()
                except Empty:
                    self._write_steps(steps)
                    break
                if kind == "steps":
                    steps.append(data)
                    if len(steps) >= _BATCH_SIZE:
                        step_error = self._write_steps(steps) or step_error
                    continue
                step_error = self._write_steps(steps) or step_error
                if kind == "save":
                    record, result = data
                    try:
                        self._write(self.store.save_task, record)
                        result.set_result(self._write(self.store.get_task_summary, record["task_id"]))
                    except Exception as exc:
                        self._failed("save", exc, result)
                elif kind == "flush":
                    target, result = data
                    if step_error is not None:
                        result.set_exception(step_error)
                    elif target is None or target <= processed:
                        result.set_result(None)
                    else:
                        flushes.append((target, result))
                else:
                    stopping = True
                step_error = None

            now = monotonic()
            if now >= next_warning:
                self._report_drops()
                next_warning = now + 5
            size = self.log_queue.qsize()
            if size and deadline is None:
                deadline = now + _BATCH_INTERVAL
            if size and (size >= _BATCH_SIZE or now >= deadline or flushes or stopping):
                rows = [self.log_queue.get_nowait() for _ in range(min(size, _BATCH_SIZE))]
                processed += len(rows)
                try:
                    saved = self._write_logs(rows)
                    if self.max_logs:
                        dirty_tasks.update(row["task_id"] for row in saved)
                        batches += 1
                except Exception as exc:
                    self._failed("logs", exc)
                    for _, result in flushes:
                        result.set_exception(exc)
                    flushes.clear()
                for target, result in flushes[:]:
                    if target <= processed:
                        result.set_result(None)
                        flushes.remove((target, result))
                deadline = None
                if batches < 50:
                    continue

            if dirty_tasks and (batches >= 50 or stopping):
                try:
                    self._write(self.store.prune_logs, dirty_tasks, self.max_logs)
                    dirty_tasks.clear()
                except Exception as exc:
                    self._failed("prune", exc)
                batches = 0
                if size:
                    continue
            if stopping:
                self._report_drops()
                return
            if not size:
                deadline = None
            timeout = min(deadline or next_warning, next_warning) - monotonic()
            self._wake.wait(max(0, timeout))
