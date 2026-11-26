"""Run the offline 300-session throughput check against a real loopback HTTP server."""
import argparse
import asyncio
from collections import Counter
from contextlib import closing
import ctypes
import json
import logging
import math
import os
from pathlib import Path
import socket
import sqlite3
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace

import httpx
import uvicorn

from src.interfaces.http import create_app
from src.service.application import Application


ACCOUNT_COUNT = 300
INTERVAL = .2


class MockConnection:
    def __init__(self, spec):
        self.context = None
        self.online = False

    async def connect(self, retries):
        async def initialize():
            await asyncio.sleep(0)
        self.context = SimpleNamespace(wait_for_player_initialization=initialize)
        self.online = True
        return True

    async def disconnect(self):
        self.online = False


class CheckErrors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.errors = []

    def emit(self, record):
        self.errors.append(self.format(record))


def working_set():
    if os.name == "nt":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("faults", wintypes.DWORD),
                        ("peak", ctypes.c_size_t), ("working_set", ctypes.c_size_t),
                        ("other", ctypes.c_size_t * 6)]

        process = ctypes.windll.kernel32.GetCurrentProcess
        process.restype = wintypes.HANDLE
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
        counters = Counters()
        if get_memory(process(), ctypes.byref(counters), ctypes.sizeof(counters)):
            return counters.working_set
    elif Path("/proc/self/statm").exists():
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")


def p99(samples):
    return round(sorted(samples)[math.ceil(len(samples) * .99) - 1], 2)


async def wait_until(predicate, timeout=30):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.01)


async def simulate(directory, duration):
    ticks = round(duration / INTERVAL)
    expected = ticks * ACCOUNT_COUNT
    start, done = asyncio.Event(), asyncio.Event()
    arrived = 0
    counts = Counter()
    loop = asyncio.get_running_loop()
    start_at = 0

    async def execute(account_id, context, steps, **options):
        nonlocal arrived
        arrived += 1
        await start.wait()
        for tick in range(ticks):
            await asyncio.sleep(max(0, start_at + tick * INTERVAL - loop.time()))
            context.log_callback(f"offline benchmark log {tick}")
        await asyncio.sleep(max(0, start_at + duration - loop.time()))
        return SimpleNamespace(success=True, completed_repetitions=1, send_results=(), step_results=())

    app = Application(directory, executor=execute)
    app.sessions.connection_factory = MockConnection
    for index in range(12):
        group_id = f"fixture-{index}"
        app.store.save_egress_group(group_id, {"name": group_id, "clash_policy": "DIRECT",
                                               "enabled": True, "routing_confirmed": True})
        app.egress._verified.add(group_id)
        app.workers.worker(group_id).exit_ip = f"192.0.2.{index + 1}"
    ids = [app.accounts.create({"label": f"Fixture {i}", "account": f"fixture-{i}@example.invalid",
                                "password": f"fixture-password-{i}"})["account_id"]
           for i in range(ACCOUNT_COUNT)]
    http = create_app(application=app)
    service_samples = {"health": [], "task": []}

    async def timed_http(scope, receive, send):
        path = scope.get("path", "").split("/")
        endpoint = ("health" if path[-1] == "health" else
                    "task" if len(path) == 5 and path[-2] == "tasks" else None)
        if endpoint and scope.get("method") == "GET":
            before = perf_counter()
            await http(scope, receive, send)
            service_samples[endpoint].append((perf_counter() - before) * 1000)
        else:
            await http(scope, receive, send)

    server = uvicorn.Server(uvicorn.Config(timed_http, log_level="warning",
                                           access_log=False, ws="none", timeout_graceful_shutdown=5))
    handler = CheckErrors()
    logging.getLogger().addHandler(handler)
    logging.getLogger("uvicorn").addHandler(handler)
    probes, streams = [], []

    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                await wait_until(lambda: server.started or serving.done())
                assert server.started
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                                             timeout=httpx.Timeout(15, read=None)) as client:
                    response = await client.post("/api/v1/sessions/connect",
                                                 json={"account_ids": ids, "concurrency": ACCOUNT_COUNT})
                    response.raise_for_status()
                    connect_id = response.json()["task_id"]
                    await wait_until(lambda: connect_id not in app.tasks._runs, timeout=60)
                    assert app.tasks.get(connect_id)["state"] == "succeeded"
                    assert Counter(session.exit_ip for session in app.sessions._sessions.values()
                                   if session.state == "ready") == {
                                       f"192.0.2.{index + 1}": 25 for index in range(12)}

                    response = await client.post("/api/v1/tasks", json={
                        "account_ids": ids,
                        "steps": [{"kind": "messages", "messages": [
                            {"id": -1, "cmd": "#time", "param": {"seconds": 0}}]}]})
                    response.raise_for_status()
                    task_id = response.json()["task_id"]
                    task_path = f"/api/v1/tasks/{task_id}"
                    run = app.tasks._runs[task_id]
                    connected = [asyncio.Event(), asyncio.Event()]

                    async def stream(index):
                        path = "/api/v1/events" + (f"?task_id={task_id}" if index else "")
                        async with client.stream("GET", path) as response:
                            response.raise_for_status()
                            async for line in response.aiter_lines():
                                if line == ": connected":
                                    connected[index].set()
                                elif line.startswith("data: "):
                                    event = json.loads(line[6:])
                                    counts[index, event["event"]] += 1
                                    if index:
                                        assert event["data"]["task_id"] == task_id

                    streams = [asyncio.create_task(stream(index)) for index in range(2)]
                    await wait_until(lambda: arrived == ACCOUNT_COUNT and all(event.is_set() for event in connected))

                    async def poll(path):
                        samples = []
                        while not done.is_set():
                            before = perf_counter()
                            response = await client.get(path)
                            response.raise_for_status()
                            samples.append((perf_counter() - before) * 1000)
                            await asyncio.sleep(.025)
                        return samples

                    async def sample_resources():
                        memory, queues = [], []
                        while not done.is_set():
                            memory.append(working_set() or 0)
                            queues.append(app.log_writer.queue.qsize())
                            await asyncio.sleep(.05)
                        return memory, queues

                    probes = [asyncio.create_task(poll(path)) for path in ("/api/v1/health", task_path)]
                    resources = asyncio.create_task(sample_resources())
                    probes.append(resources)
                    start_at = loop.time() + .02
                    start.set()
                    await asyncio.wait_for(asyncio.shield(run.task), duration + 15)
                    done.set()
                    health, detail, (memory, queues) = await asyncio.gather(*probes)
                    await wait_until(lambda: counts[1, "log"] == expected or any(t.done() for t in streams))
                    for stream_task in streams:
                        if stream_task.done():
                            stream_task.result()
                    assert app.tasks.get(task_id)["state"] == "succeeded"
            finally:
                done.set()
                for task in probes + streams:
                    task.cancel()
                await asyncio.gather(*probes, *streams, return_exceptions=True)
                server.should_exit = True
                await serving
    finally:
        logging.getLogger().removeHandler(handler)
        logging.getLogger("uvicorn").removeHandler(handler)

    with closing(sqlite3.connect(directory / "service.sqlite3")) as db:
        persisted = db.execute("SELECT COUNT(*) FROM logs WHERE task_id=?", (task_id,)).fetchone()[0]
    report = {"sessions": ACCOUNT_COUNT, "groups": 12, "seconds": duration,
              "expected_logs": expected, "persisted_logs": persisted, "detail_sse_logs": counts[1, "log"],
              "global_sse_logs": counts[0, "log"], "global_sse_task_events": counts[0, "task"],
              "dropped_logs": app.log_writer.dropped_count, "max_sampled_queue": max(queues),
              "health_p99_ms": p99(health), "task_p99_ms": p99(detail),
              "asgi_health_p99_ms": p99(service_samples["health"]), "asgi_task_p99_ms": p99(service_samples["task"]),
              "health_samples": len(health), "task_samples": len(detail), "error_count": len(handler.errors)}
    if memory and memory[0]:
        report["working_set_mib"] = {key: round(value / 2**20, 1) for key, value in
                                     (("start", memory[0]), ("peak", max(memory)), ("end", memory[-1]))}
    print(json.dumps(report, indent=2))
    assert persisted == counts[1, "log"] == expected
    assert counts[0, "log"] == app.log_writer.dropped_count == 0
    assert counts[0, "task"] > 0
    assert p99(health) < 50 and p99(detail) < 50
    assert not handler.errors, handler.errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, choices=range(1, 31), default=30, metavar="1-30")
    args = parser.parse_args()
    with TemporaryDirectory() as directory:
        asyncio.run(simulate(Path(directory), args.seconds))
