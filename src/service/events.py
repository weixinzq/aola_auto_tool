"""Bounded, best-effort notifications; persisted queries remain authoritative."""
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from threading import get_ident


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class EventHub:
    def __init__(self, queue_size=1024):
        self.queue_size = queue_size
        self._subscribers = {}
        self._sequence = 0
        self._loop = None
        self._loop_thread = None

    def bind(self):
        self._loop = asyncio.get_running_loop()
        self._loop_thread = get_ident()

    @property
    def has_subscribers(self):
        return bool(self._subscribers)

    def subscribe(self, *, include_logs=True, task_id=None, batch_logs=False):
        queue = asyncio.Queue(maxsize=self.queue_size)
        self._subscribers[queue] = (include_logs, task_id, batch_logs)
        return queue

    def unsubscribe(self, queue):
        self._subscribers.pop(queue, None)

    def _event(self, name, data):
        self._sequence += 1
        return {"id": str(self._sequence), "event": name, "timestamp": utc_now(), "data": data}

    @staticmethod
    def _offer(queue, event, *, log=False):
        if queue.full():
            if log:
                return
            queue.get_nowait()
        queue.put_nowait(event)

    def publish(self, name, data):
        if self._loop is not None and get_ident() != self._loop_thread:
            self._loop.call_soon_threadsafe(self.publish, name, data)
            return
        if name == "log":
            self.publish_logs([data])
            return
        event = self._event(name, data)
        for queue, (_, task_id, _) in self._subscribers.items():
            if task_id is None or data.get("task_id") == task_id:
                self._offer(queue, event)
        return event

    def publish_logs(self, rows):
        subscribers = [(queue, task_id, batch) for queue, (include, task_id, batch)
                       in self._subscribers.items() if include]
        if not subscribers:
            return
        all_tasks = any(task_id is None for _, task_id, _ in subscribers)
        selected = {task_id for _, task_id, _ in subscribers}
        events, by_task = [], defaultdict(list)
        for row in rows:
            task_id = row["task_id"]
            if all_tasks or task_id in selected:
                event = self._event("log", row)
                events.append(event)
                by_task[task_id].append(event)
        for queue, task_id, batch in subscribers:
            matching = events if task_id is None else by_task.get(task_id, [])
            if batch and matching:
                self._offer(queue, matching, log=True)
            elif not batch:
                for event in matching:
                    self._offer(queue, event, log=True)
