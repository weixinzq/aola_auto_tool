"""HTTP contracts and real loopback CLI checks; no game login or outbound traffic."""
from __future__ import annotations

import asyncio
from collections import Counter
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import aiohttp
from fastapi.testclient import TestClient
from fastapi import Request
from pydantic import ValidationError
from typer.testing import CliRunner
import uvicorn

from src.interfaces.cli import app as cli
from src.interfaces.http import SessionInput, _same_origin, create_app
from src.service.application import Application


class SimulatedConnection:
    instances = []

    def __init__(self, spec):
        self.spec = spec
        self.context = None
        self.online = False
        self.last_error = ""
        self.login_block_reason = ""
        self.state = "offline"
        self.instances.append(self)

    async def connect(self, retries=1):
        async def initialize():
            await asyncio.sleep(0.01)
        self.context = SimpleNamespace(wait_for_player_initialization=initialize, log_callback=None)
        self.online = True
        return True

    async def disconnect(self):
        await asyncio.sleep(0.01)
        self.online = False
        self.context = None


async def simulated_executor(_account_id, context, _steps, **options):
    await asyncio.sleep(0.1)
    context.log_callback("simulated local write completed")
    return SimpleNamespace(success=True, send_results=(SimpleNamespace(success=True),),
                           completed_repetitions=options["repetitions"],
                           step_results=(SimpleNamespace(repetition=1, step=1, kind="messages",
                                                         success=True, error=""),))


def account_payload(label="sample"):
    return {"label": label, "account": "simulated-" + label,
            "password": "offline-test-secret", "char_id": 0, "zone_index": 1}


def task_payload(*account_ids):
    return {"account_ids": list(account_ids), "repeat_interval": 0, "message_delay": 0,
            "steps": [{"kind": "messages", "messages": [
                {"id": -1, "cmd": "#time", "param": {"seconds": 0}}]}]}


def simulated_application(directory, executor=simulated_executor):
    app = Application(directory, executor=executor)
    app.sessions.connection_factory = SimulatedConnection
    app.store.save_egress_group("fixture", {"name": "Fixture", "clash_policy": "DIRECT",
                                            "routing_confirmed": True, "enabled": True})
    app.workers.worker("fixture").exit_ip = "192.0.2.1"
    app.egress._verified.add("fixture")
    return app


def wait_task(client, task_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/tasks/{task_id}")
        response.raise_for_status()
        record = response.json()
        if record["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return record
        time.sleep(0.01)
    raise AssertionError("Simulated task did not finish")


class AdapterInvariantTests(unittest.TestCase):
    def test_origin_validation_handles_malformed_origins_and_default_ports(self):
        for origin in ("http://testserver", "http://testserver:80", "http://TESTSERVER"):
            request = Request({"type": "http", "scheme": "http", "path": "/", "query_string": b"",
                               "headers": [(b"host", b"testserver"), (b"origin", origin.encode())]})
            self.assertTrue(_same_origin(request), origin)
        for origin in ("http://[broken", "null", "http://testserver:invalid", "http://testserver:81",
                       "https://testserver", "http://other.invalid", "http://user@testserver",
                       "http://testserver/path", "http://testserver?query=1"):
            request = Request({"type": "http", "scheme": "http", "path": "/", "query_string": b"",
                               "headers": [(b"host", b"testserver"), (b"origin", origin.encode())]})
            self.assertFalse(_same_origin(request), origin)

    def test_session_inputs_reject_invalid_batches_before_application_dispatch(self):
        for body in ({"account_ids": ["same", "same"]},
                     {"account_ids": ["sample"], "concurrency": True},
                     {"account_ids": ["sample"], "concurrency": 0},
                     {"account_ids": ["sample"], "concurrency": 501}):
            with self.assertRaises(ValidationError):
                SessionInput.model_validate(body)
        batch = SessionInput.model_validate({"account_ids": [str(number) for number in range(101)],
                                             "concurrency": 500})
        self.assertEqual(len(batch.account_ids), 101)
        self.assertEqual(batch.concurrency, 500)

    def test_service_host_limits_workers_and_shutdown_wait_for_streams(self):
        with patch("src.interfaces.http.create_app") as factory, patch("uvicorn.run") as run:
            invocation = CliRunner().invoke(cli, ["serve"])
        self.assertEqual(invocation.exit_code, 0, invocation.output)
        self.assertEqual(run.call_args.kwargs["workers"], 1)
        self.assertEqual(run.call_args.kwargs["timeout_graceful_shutdown"], 10)
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(factory.call_args.kwargs["max_online"], 100)
        self.assertIsNone(factory.call_args.kwargs["login_concurrency"])
        with patch("src.interfaces.http.create_app") as factory, patch("uvicorn.run"):
            invocation = CliRunner().invoke(cli, ["serve", "--max-online", "300", "--login-concurrency", "5"])
        self.assertEqual(invocation.exit_code, 0, invocation.output)
        self.assertEqual(factory.call_args.kwargs["max_online"], 300)
        self.assertEqual(factory.call_args.kwargs["login_concurrency"], 5)
        for flag in ("--max-online", "--login-concurrency"):
            self.assertNotEqual(CliRunner().invoke(cli, ["serve", flag, "0"]).exit_code, 0)

    def test_non_utf8_message_file_reports_an_input_error(self):
        invalid = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")
        with patch("pathlib.Path.read_text", side_effect=invalid):
            result = CliRunner().invoke(cli, ["parse", "--file", __file__])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("Cannot read message file", result.output)

    def test_account_creation_requires_an_explicit_stable_label(self):
        result = CliRunner().invoke(cli, ["accounts", "create", "--account", "sample-login",
                                         "--password", "local-test-password"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--label", result.output)


class APIContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        SimulatedConnection.instances.clear()
        self.application = simulated_application(self.directory)
        self.frontend = self.directory / "frontend"
        self.frontend.mkdir()
        (self.frontend / "index.html").write_text("<html>Local application</html>", encoding="utf-8")
        (self.frontend / "app.js").write_text("console.log('application')", encoding="utf-8")
        self.web = create_app(application=self.application, frontend_dir=self.frontend)
        self.client = self.enterContext(TestClient(self.web))

    def create_account(self, label="sample"):
        response = self.client.post("/api/v1/accounts", json=account_payload(label))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("password", response.json())
        return response.json()["account_id"]

    def test_validation_and_account_responses_never_echo_passwords(self):
        secret = "do-not-reflect-password"
        value = account_payload()
        value["password"] = {"submitted": secret}
        response = self.client.post("/api/v1/accounts", json=value)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], "invalid_request")
        self.assertTrue(all("input" not in detail for detail in error["details"]))
        account_id = self.create_account()
        self.assertNotIn("offline-test-secret", self.client.get("/api/v1/accounts").text)
        task = task_payload(account_id)
        task["file"] = "C:/must-not-read.json"
        self.assertEqual(self.client.post("/api/v1/tasks/validate", json=task).status_code, 422)
        for endpoint in ("/accounts/import", "/messages/parse"):
            response = self.client.post("/api/v1" + endpoint, json={"file": "C:/must-not-read.json"})
            self.assertEqual(response.status_code, 422)
        for script in self.client.get("/api/v1/scripts").json():
            self.assertNotIn("module", script)
            self.assertNotIn("module_name", script)
        for operation in ("connect", "reconnect", "disconnect"):
            response = self.client.post("/api/v1/sessions/" + operation,
                                        json={"account_ids": [account_id, account_id]})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(SimulatedConnection.instances, [])

    def test_same_origin_browser_writes_and_headerless_cli_writes(self):
        foreign = {"Origin": "http://elsewhere.invalid"}
        same = {"Origin": "http://testserver"}
        url = "/api/v1/accounts"
        response = self.client.post(url, json=account_payload(), headers=foreign)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "origin_mismatch")
        self.assertEqual(self.client.get(url, headers=foreign).json(), [])
        created = self.client.post(url, json=account_payload(), headers=same)
        self.assertEqual(created.status_code, 201, created.text)
        account_url = url + "/" + created.json()["account_id"]
        self.assertEqual(self.client.patch(account_url, json={"label": "changed"}, headers=foreign).status_code, 403)
        self.assertEqual(self.client.delete(account_url, headers=foreign).status_code, 403)
        self.assertEqual(self.client.get(account_url).json()["label"], "sample")
        self.assertEqual(self.client.delete(account_url, headers=same).status_code, 204)
        self.assertEqual(self.client.get(account_url).status_code, 404)
        self.create_account()

    def test_retired_auth_routes_do_not_serve_frontend_or_set_cookies(self):
        for method in ("POST", "DELETE"):
            response = self.client.request(method, "/api/v1/auth/session",
                                           headers={"Origin": "http://testserver"})
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("set-cookie", response.headers)

    def test_preflight_persistent_session_ownership_and_cleanup(self):
        first = self.create_account("first")
        second = self.create_account("second")
        parsed = self.client.post("/api/v1/messages/parse", json={"text": "#time=0|"})
        self.assertEqual(parsed.status_code, 200, parsed.text)
        preflight = self.client.post("/api/v1/tasks/validate", json=task_payload(first))
        self.assertEqual(preflight.status_code, 200, preflight.text)
        self.assertEqual(SimulatedConnection.instances, [])
        connected = self.client.post("/api/v1/sessions/connect", json={"account_ids": [first]})
        self.assertEqual(connected.status_code, 202, connected.text)
        self.assertEqual(wait_task(self.client, connected.json()["task_id"])["state"], "succeeded")
        response = self.client.patch(f"/api/v1/accounts/{second}", json={"label": "edited"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.patch(f"/api/v1/accounts/{first}", json={"label": "busy"}).status_code, 409)

        executing, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)

        async def held_executor(*args, **options):
            executing.set()
            while not finish.is_set():
                await asyncio.sleep(.01)
            return await simulated_executor(*args, **options)

        self.application.tasks.executor = held_executor
        started = self.client.post("/api/v1/tasks", json=task_payload(first))
        self.assertEqual(started.status_code, 202, started.text)
        task_id = started.json()["task_id"]
        self.assertTrue(executing.wait(5))
        conflict = self.client.post("/api/v1/tasks", json=task_payload(first))
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "account_busy")
        active = self.client.get(f"/api/v1/tasks/{task_id}")
        self.assertEqual(active.status_code, 200, active.text)
        self.assertEqual(active.headers["content-type"], "application/json")
        self.assertEqual(active.json()["state"], "running")
        finish.set()
        result = wait_task(self.client, task_id)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["account_results"][0]["business_confirmation"], "unknown")
        self.assertEqual(result["account_results"][0]["network_write"]["status"], "succeeded")
        self.assertIsNone(result["account_results"][0]["step_results"][0]["error"])
        results = self.client.get(f"/api/v1/tasks/{task_id}/results")
        self.assertEqual(results.status_code, 200, results.text)
        self.assertEqual(results.json(), {key: result[key] for key in (
            "task_id", "state", "error", "account_results", "server_results")})
        summary = next(row for row in self.client.get("/api/v1/tasks").json() if row["task_id"] == task_id)
        self.assertEqual((summary["state"], summary["account_count"]), ("succeeded", 1))
        self.assertNotIn("account_results", summary)
        logs_url = f"/api/v1/tasks/{task_id}/logs"
        logs = self.client.get(logs_url).json()
        self.assertEqual([(row["account_id"], row["message"]) for row in logs],
                         [(first, "simulated local write completed")])
        self.assertEqual(self.client.get(logs_url, params={"after": logs[-1]["id"]}).json(), [])

        by_id = {row["account_id"]: row for row in self.client.get("/api/v1/sessions").json()}
        self.assertEqual(by_id[first]["state"], "ready")
        self.assertEqual(by_id[second]["state"], "offline")
        reconnected = self.client.post("/api/v1/sessions/reconnect", json={"account_ids": [first]})
        self.assertEqual(reconnected.status_code, 202, reconnected.text)
        self.assertEqual(wait_task(self.client, reconnected.json()["task_id"])["state"], "succeeded")
        self.assertEqual(len(SimulatedConnection.instances), 2)
        self.assertFalse(SimulatedConnection.instances[0].online)
        disconnected = self.client.post("/api/v1/sessions/disconnect", json={"account_ids": [first]})
        self.assertEqual(disconnected.status_code, 202, disconnected.text)
        self.assertEqual(wait_task(self.client, disconnected.json()["task_id"])["state"], "succeeded")
        self.assertTrue(all(row["state"] == "offline" for row in self.client.get("/api/v1/sessions").json()))
        self.assertEqual(self.client.patch(f"/api/v1/accounts/{first}", json={"label": "available"}).status_code, 200)

    def test_pooled_tasks_reuse_ready_sessions_and_connect_offline_accounts(self):
        first, offline = self.create_account("online"), self.create_account("offline")
        connected = self.client.post("/api/v1/sessions/connect", json={"account_ids": [first]})
        self.assertEqual(connected.status_code, 202, connected.text)
        self.assertEqual(wait_task(self.client, connected.json()["task_id"])["state"], "succeeded")
        payload = task_payload(offline, first)
        checked = self.client.post("/api/v1/tasks/validate", json=payload)
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(checked.json()["account_count"], 2)
        self.assertEqual(checked.json()["required_capacity"], 1)
        self.assertEqual(checked.json()["skipped_accounts"], [])
        self.assertEqual(checked.json()["spec"]["session_policy"], "pooled")
        self.assertEqual(len(SimulatedConnection.instances), 1)
        started = self.client.post("/api/v1/tasks", json=payload)
        self.assertEqual(started.status_code, 202, started.text)
        result = wait_task(self.client, started.json()["task_id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual([(row["account_id"], row["state"]) for row in result["account_results"]],
                         [(offline, "succeeded"), (first, "succeeded")])
        self.assertEqual(Counter(connection.spec.config["service_account_id"]
                                 for connection in SimulatedConnection.instances), {first: 1, offline: 1})

    def test_openapi_exposes_inputs_and_sanitized_output_types(self):
        schema = self.web.openapi()
        models = schema["components"]["schemas"]
        self.assertIn("TaskSpec", models)
        self.assertIn("TaskRecord", models)
        self.assertEqual(schema["paths"]["/api/v1/tasks"]["post"]["requestBody"]["content"]["application/json"][
            "schema"]["$ref"], "#/components/schemas/ScriptTaskInput")
        self.assertEqual(schema["paths"]["/api/v1/tasks/{task_id}"]["get"]["responses"]["200"]["content"][
            "application/json"]["schema"]["$ref"], "#/components/schemas/TaskRecord")
        self.assertEqual(models["SessionInput"]["properties"]["concurrency"]["maximum"], 500)
        self.assertNotIn("password", models["AccountRecord"]["properties"])
        self.assertNotIn("securitySchemes", schema["components"])
        self.assertNotIn("/{path}", schema["paths"])
        self.assertNotIn("/api/v1/auth/session", schema["paths"])
        event_types = schema["paths"]["/api/v1/events"]["get"]["responses"]["200"]["content"]
        self.assertEqual(set(event_types), {"text/event-stream"})

    def test_online_capacity_is_checked_by_service_instead_of_batch_length(self):
        imported = self.client.post("/api/v1/accounts/import", json={"config": {
            "accounts": [account_payload(f"capacity-{number}") for number in range(101)],
        }})
        self.assertEqual(imported.status_code, 200, imported.text)
        account_ids = [row["account_id"] for row in imported.json()["accounts"]]
        checked = self.client.post("/api/v1/tasks/validate", json=task_payload(*account_ids))
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(checked.json()["required_capacity"], 101)
        self.assertEqual(checked.json()["max_online"], 25)
        self.assertEqual(SimulatedConnection.instances, [])
        self.assertEqual(self.client.get("/api/v1/tasks").json(), [])

        started = self.client.post("/api/v1/sessions/connect",
                                   json={"account_ids": account_ids, "concurrency": 101})
        self.assertEqual(started.status_code, 202, started.text)
        task_id = started.json()["task_id"]
        expected_states = {"ready": 25, "waiting_egress": 76}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            states = Counter(row["state"] for row in self.client.get("/api/v1/sessions").json())
            if states == expected_states:
                break
            time.sleep(.01)
        self.assertEqual(states, expected_states)
        groups = self.client.get("/api/v1/egress-groups")
        self.assertEqual(groups.status_code, 200, groups.text)
        self.assertEqual(groups.json()[0]["online_count"], 25)
        cancelled = self.client.post(f"/api/v1/tasks/{task_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(wait_task(self.client, task_id)["state"], "cancelled")
        states = Counter(row["state"] for row in self.client.get("/api/v1/sessions").json())
        self.assertEqual(states, {"ready": 25, "offline": 76})

    def test_frontend_static_assets_history_routes_and_api_404(self):
        (self.directory / "private.txt").write_text("outside frontend", encoding="utf-8")
        self.assertEqual(self.client.get("/").text, "<html>Local application</html>")
        self.assertEqual(self.client.get("/tasks/history").text, "<html>Local application</html>")
        self.assertIn("application", self.client.get("/app.js").text)
        self.assertEqual(self.client.head("/app.js").status_code, 200)
        self.assertEqual(self.client.get("/missing.js").status_code, 404)
        self.assertEqual(self.client.get("/assets/missing").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/unknown").status_code, 404)
        self.assertEqual(self.client.post("/api/v1/unknown", json={}).status_code, 404)
        self.assertEqual(self.client.get("/api/unknown").status_code, 404)
        self.assertEqual(self.client.get("/%2e%2e/private.txt").status_code, 404)


class LoopbackCLIAndEventsTests(unittest.TestCase):
    def test_cli_uses_http_and_sse_disconnect_does_not_cancel_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            release_execution = threading.Event()
            finish_execution = threading.Event()

            async def held_executor(*args, **kwargs):
                while not release_execution.is_set():
                    await asyncio.sleep(0.01)
                for number in (1, 2):
                    args[1].log_callback(f"simulated task log {number}")
                while not finish_execution.is_set():
                    await asyncio.sleep(0.01)
                return await simulated_executor(*args, **kwargs)

            application = simulated_application(Path(temporary), executor=held_executor)
            web = create_app(application=application)
            server = uvicorn.Server(uvicorn.Config(web, log_level="error", lifespan="on"))
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(128)
                port = listener.getsockname()[1]
                thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
                thread.start()
                try:
                    deadline = time.monotonic() + 5
                    while not server.started and thread.is_alive() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(server.started)
                    base = ["--url", f"http://127.0.0.1:{port}", "--json"]
                    runner = CliRunner()
                    with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
                        created = runner.invoke(cli, base + ["accounts", "create", "--label", "cli-sample",
                             "--account", "simulated-cli", "--password", "local-cli-secret"])
                    self.assertEqual(created.exit_code, 0, created.output)
                    account_id = json.loads(created.stdout)["account_id"]
                    messages = Path(temporary) / "messages.txt"
                    messages.write_text("#time=0|", encoding="utf-8")
                    legacy = Path(temporary) / "legacy.json"
                    legacy.write_text(json.dumps({"workers": [{"id": "old-worker",
                        "accounts": {"labels": ["cli-sample"]},
                        "steps": [{"kind": "messages", "file": "messages.txt"}]}]}), encoding="utf-8")
                    original = legacy.read_bytes()
                    report_file = Path(temporary) / "migration.json"
                    migrated = runner.invoke(cli, base + ["tasks", "migrate", "--file", str(legacy),
                                                         "--output", str(report_file)])
                    self.assertEqual(migrated.exit_code, 0, migrated.output)
                    report = json.loads(migrated.stdout)
                    self.assertEqual(report["tasks"][0]["account_ids"], [account_id])
                    self.assertFalse(report["source_modified"])
                    self.assertEqual(json.loads(report_file.read_text(encoding="utf-8")), report)
                    self.assertEqual(legacy.read_bytes(), original)
                    self.assertNotIn("file", report["tasks"][0]["steps"][0])
                    self.assertEqual(runner.invoke(cli, base + ["tasks", "list"]).stdout.strip(), "[]")
                    refused = runner.invoke(cli, base + ["tasks", "migrate", "--file", str(legacy),
                                                        "--output", str(legacy)])
                    self.assertNotEqual(refused.exit_code, 0)
                    self.assertEqual(legacy.read_bytes(), original)
                    snapshot = Path(temporary) / "task.json"
                    snapshot.write_text(json.dumps(task_payload(account_id)), encoding="utf-8")
                    before = runner.invoke(cli, base + ["tasks", "validate", "--file", str(snapshot)])
                    self.assertEqual(before.exit_code, 0, before.output)
                    self.assertTrue(json.loads(before.stdout)["valid"])
                    started = runner.invoke(cli, base + ["tasks", "start", "--file", str(snapshot)])
                    self.assertEqual(started.exit_code, 0, started.output)
                    task_id = json.loads(started.stdout)["task_id"]

                    async def read_logs():
                        async with aiohttp.ClientSession(trust_env=False) as client:
                            async with client.get(f"http://127.0.0.1:{port}/api/v1/events",
                                                  params={"task_id": task_id}) as response:
                                self.assertEqual(response.status, 200)
                                self.assertEqual(response.content_type, "text/event-stream")
                                async with asyncio.timeout(5):
                                    self.assertEqual(await response.content.readuntil(b"\n\n"), b": connected\n\n")
                                    release_execution.set()
                                    logs = []
                                    while len(logs) < 2:
                                        frame = await response.content.readuntil(b"\n\n")
                                        self.assertTrue(frame.startswith(b"data: "), frame)
                                        event = json.loads(frame[6:])
                                        self.assertEqual(event["data"]["task_id"], task_id)
                                        if event["event"] == "log":
                                            logs.append(event["data"])
                                    return logs

                    streamed = asyncio.run(read_logs())
                    self.assertEqual([row["message"] for row in streamed],
                                     ["simulated task log 1", "simulated task log 2"])
                    still_running = runner.invoke(cli, base + ["tasks", "status", task_id])
                    self.assertEqual(still_running.exit_code, 0, still_running.output)
                    self.assertEqual(json.loads(still_running.stdout)["state"], "running")
                    cleanup_deadline = time.monotonic() + 2
                    while application.events._subscribers and time.monotonic() < cleanup_deadline:
                        time.sleep(0.01)
                    self.assertEqual(len(application.events._subscribers), 0)
                    finish_execution.set()
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        status = runner.invoke(cli, base + ["tasks", "status", task_id])
                        self.assertEqual(status.exit_code, 0, status.output)
                        record = json.loads(status.stdout)
                        if record["state"] in {"succeeded", "failed", "cancelled"}:
                            break
                        time.sleep(0.01)
                    self.assertEqual(record["state"], "succeeded")
                    logs = runner.invoke(cli, base + ["logs", "show", task_id])
                    self.assertEqual(logs.exit_code, 0, logs.output)
                    self.assertEqual(json.loads(logs.stdout)[:2], streamed)
                    listed = runner.invoke(cli, base + ["accounts", "list"])
                    self.assertEqual(listed.exit_code, 0, listed.output)
                    self.assertNotIn("local-cli-secret", listed.output)
                    self.assertEqual(json.loads(listed.stdout)[0]["account_id"], account_id)
                    rejected = runner.invoke(cli, base + ["tasks", "status", "missing-task"])
                    self.assertEqual(rejected.exit_code, 1)
                    error = json.loads(rejected.stdout)["error"]
                    self.assertEqual(error["code"], "cli_error")
                    self.assertTrue(error["message"].startswith("not_found:"), error)
                finally:
                    release_execution.set()
                    finish_execution.set()
                    server.should_exit = True
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())

if __name__ == "__main__":
    unittest.main()
