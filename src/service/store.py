"""SQLite persistence. Runtime sessions never live in the database."""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import Column, Integer, JSON, MetaData, String, Table, create_engine, select, event, func

from src.service.contracts import ServiceError
from src.service.events import utc_now
from src.service.runtime import Redactor


metadata = MetaData()
accounts = Table("accounts", metadata, Column("account_id", String, primary_key=True), Column("config", JSON, nullable=False))
tasks = Table("tasks", metadata, Column("task_id", String, primary_key=True), Column("record", JSON, nullable=False))
egress_groups = Table("egress_groups", metadata, Column("group_id", String, primary_key=True), Column("config", JSON, nullable=False))
logs = Table("logs", metadata, Column("id", Integer, primary_key=True, autoincrement=True),
             Column("task_id", String, index=True), Column("account_id", String),
             Column("timestamp", String, nullable=False), Column("message", String, nullable=False))
task_step_batches = Table("task_step_batches", metadata, Column("id", Integer, primary_key=True),
                          Column("task_id", String, nullable=False), Column("account_id", String, nullable=False),
                          Column("records", JSON, nullable=False))
_SECRET_KEYS = Redactor._secret_keys - {"account", "username"}
_GENERATED_FIELDS = {"task_id", "account_id", "account_ids", "script_id", "created_at", "updated_at",
                     "started_at", "finished_at", "timestamp", "state", "kind", "mode", "session_policy",
                     "status", "business_confirmation", "code", "egress_group_id"}


class Store:
    def __init__(self, directory: Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.directory.chmod(0o700)
        self._redactor = Redactor(None)
        db = self.directory / "service.sqlite3"
        self.engine = create_engine("sqlite:///" + db.as_posix(), connect_args={"check_same_thread": False})

        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA cache_size=-32000")

        config = Config()
        config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        if os.name != "nt":
            db.chmod(0o600)
        for row in self.list_accounts(private=True):
            self._remember(row)

    def _remember(self, account):
        self.add_secret(account.get("password"))
        self.add_secret(account.get("account"))

    def add_secret(self, value):
        self._redactor.add(value)

    @property
    def secret_revision(self):
        return len(self._redactor.values)

    def redact_log(self, value):
        return self._redactor.clean(value)

    def redact(self, value, *, task_record=False):
        if isinstance(value, dict):
            return {key: deepcopy(item) if task_record and key in _GENERATED_FIELDS and (
                        isinstance(item, str) or item is None or
                        key == "account_ids" and isinstance(item, list) and all(isinstance(id_, str) for id_ in item)) else
                    "<redacted>" if str(key).lower() in _SECRET_KEYS else self.redact(item, task_record=task_record)
                    for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item, task_record=task_record) for item in value]
        if isinstance(value, str):
            return self._redactor.clean(value, replacement="<redacted>")
        return value

    @staticmethod
    def _account(row, private):
        result = {"account_id": row["account_id"], **deepcopy(row["config"])}
        if not private:
            result.pop("password", None)
        return result

    def list_accounts(self, private=False):
        with self.engine.connect() as connection:
            return [self._account(row, private) for row in connection.execute(select(accounts)).mappings()]

    def get_account(self, account_id, private=False):
        with self.engine.connect() as connection:
            row = connection.execute(select(accounts).where(accounts.c.account_id == account_id)).mappings().first()
        if row is None:
            raise ServiceError("not_found", "账号不存在", 404)
        return self._account(row, private)

    def create_account(self, config):
        account_id = uuid4().hex
        with self.engine.begin() as connection:
            connection.execute(accounts.insert().values(account_id=account_id, config=config))
        self._remember(config)
        return self.get_account(account_id)

    def update_account(self, account_id, config):
        self.get_account(account_id)
        with self.engine.begin() as connection:
            connection.execute(accounts.update().where(accounts.c.account_id == account_id).values(config=config))
        self._remember(config)
        return self.get_account(account_id)

    def delete_account(self, account_id):
        self.get_account(account_id)
        with self.engine.begin() as connection:
            connection.execute(accounts.delete().where(accounts.c.account_id == account_id))

    def list_egress_groups(self):
        with self.engine.connect() as connection:
            return [{"group_id": row.group_id, **row.config} for row in connection.execute(select(egress_groups))]

    def get_egress_group(self, group_id):
        with self.engine.connect() as connection:
            row = connection.execute(select(egress_groups).where(egress_groups.c.group_id == group_id)).first()
        if row is None:
            raise ServiceError("egress_not_found", "出口分组不存在", 404)
        return {"group_id": row.group_id, **row.config}

    def save_egress_group(self, group_id, config):
        with self.engine.begin() as connection:
            exists = connection.execute(select(egress_groups.c.group_id).where(egress_groups.c.group_id == group_id)).first()
            if exists:
                connection.execute(egress_groups.update().where(egress_groups.c.group_id == group_id).values(config=config))
            else:
                connection.execute(egress_groups.insert().values(group_id=group_id, config=config))
        return self.get_egress_group(group_id)

    def delete_egress_group(self, group_id):
        self.get_egress_group(group_id)
        with self.engine.begin() as connection:
            connection.execute(egress_groups.delete().where(egress_groups.c.group_id == group_id))

    def save_task(self, record):
        # Task parameters are already replaced by _snapshot. Preserve generated
        # identifiers/enums even when a password is a single character.
        record = self.redact(record, task_record=True)
        with self.engine.begin() as connection:
            streamed = [row["account_id"] for row in record.get("account_results", [])
                        if "_step_results_streamed" in row]
            connection.execute(task_step_batches.delete().where(task_step_batches.c.task_id == record["task_id"],
                                                                task_step_batches.c.account_id.not_in(streamed)))
            exists = connection.execute(select(tasks.c.task_id).where(tasks.c.task_id == record["task_id"])).first()
            if exists:
                connection.execute(tasks.update().where(tasks.c.task_id == record["task_id"]).values(record=record))
            else:
                connection.execute(tasks.insert().values(task_id=record["task_id"], record=record))

    def get_task(self, task_id):
        with self.engine.connect() as connection:
            row = connection.execute(select(tasks.c.record).where(tasks.c.task_id == task_id)).first()
            if row is None:
                raise ServiceError("not_found", "任务不存在", 404)
            return self._task_details(row[0], connection)

    def append_task_steps(self, task_id, account_id, records):
        self.append_task_steps_batch([(task_id, account_id, records)])

    def append_task_steps_batch(self, items, *, redacted=False):
        if not items:
            return
        rows = [{"task_id": task_id, "account_id": account_id,
                 "records": records if redacted else self.redact(records, task_record=True)}
                for task_id, account_id, records in items]
        with self.engine.begin() as connection:
            connection.execute(task_step_batches.insert(), rows)

    def get_task_details(self, snapshot):
        record = self.redact(snapshot, task_record=True)
        if any(row.get("_step_results_streamed") for row in record.get("account_results", [])):
            with self.engine.connect() as connection:
                return self._task_details(record, connection)
        return self._task_details(record, None)

    def _task_details(self, record, connection):
        targets = {}
        for row in record.get("account_results", []):
            if row.pop("_step_results_streamed", False):
                row["step_results"] = []
                targets[row["account_id"]] = row["step_results"]
        if targets:
            query = select(task_step_batches.c.account_id, task_step_batches.c.records).where(
                task_step_batches.c.task_id == record["task_id"],
                task_step_batches.c.account_id.in_(targets)).order_by(task_step_batches.c.id)
            for account_id, records in connection.execute(query):
                targets[account_id].extend(records)
        return record

    def list_tasks(self):
        with self.engine.connect() as connection:
            records = [self._task_details(row[0], connection) for row in connection.execute(select(tasks.c.record))]
        return sorted(records, key=lambda item: item.get("created_at", ""), reverse=True)

    def _task_summary_query(self):
        fields = ("task_id", "kind", "state", "created_at", "updated_at", "started_at", "finished_at", "error")
        rows = func.json_each(tasks.c.record, "$.account_results").table_valued("value")
        skipped = select(func.sum(func.json_extract(rows.c.value, "$.skipped_repetitions"))).select_from(rows).correlate(tasks).scalar_subquery()
        return select(
            *(tasks.c.record[name].label(name) for name in fields),
            func.coalesce(func.json_array_length(tasks.c.record, "$.account_results"), 0).label("account_count"),
            func.coalesce(skipped, 0).label("skipped_repetitions"),
        )

    def list_task_summaries(self):
        query = self._task_summary_query().order_by(tasks.c.record["created_at"].as_string().desc())
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def get_task_summary(self, task_id):
        with self.engine.connect() as connection:
            row = connection.execute(self._task_summary_query().where(tasks.c.task_id == task_id)).mappings().first()
        if row is None:
            raise ServiceError("not_found", "任务不存在", 404)
        return dict(row)

    def interrupt_pending(self):
        for record in self.list_tasks():
            if record["state"] in {"queued", "running", "cancelling"}:
                record.update(state="interrupted", updated_at=utc_now(), error={"code": "service_restart", "message": "服务重启，任务未重放"})
                for row in record.get("account_results", []):
                    if row.get("state") not in {"succeeded", "completed", "failed", "cancelled", "skipped"}:
                        row["state"] = "interrupted"
                self.save_task(record)

    def delete_tasks(self, task_ids):
        if not task_ids:
            return
        with self.engine.begin() as connection:
            connection.execute(task_step_batches.delete().where(task_step_batches.c.task_id.in_(task_ids)))
            connection.execute(logs.delete().where(logs.c.task_id.in_(task_ids)))
            connection.execute(tasks.delete().where(tasks.c.task_id.in_(task_ids)))

    def append_log(self, task_id, account_id, message):
        return self.append_logs_batch([{"task_id": task_id, "account_id": account_id, "timestamp": utc_now(),
                                        "message": str(message)}])[0]

    def append_logs_batch(self, rows, *, redacted=False):
        if not rows:
            return []
        if not redacted:
            rows = [{**row, "message": self.redact_log(row["message"])} for row in rows]
        with self.engine.begin() as connection:
            connection.execute(logs.insert(), rows)
            # A single SQLite writer owns these consecutive rowids until commit.
            last_id = connection.scalar(select(func.last_insert_rowid()))
        return [{"id": row_id, **row} for row_id, row in enumerate(rows, last_id - len(rows) + 1)]

    def prune_logs(self, task_ids, max_logs):
        with self.engine.begin() as connection:
            for task_id in task_ids:
                cutoff = select(logs.c.id).where(logs.c.task_id == task_id).order_by(
                    logs.c.id.desc()).offset(max_logs).limit(1).scalar_subquery()
                connection.execute(logs.delete().where(logs.c.task_id == task_id, logs.c.id <= cutoff))

    def get_logs(self, task_id, after=0, limit=200):
        query = select(logs).where(logs.c.task_id == task_id, logs.c.id > after).order_by(logs.c.id).limit(min(max(limit, 1), 1000))
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def close(self):
        self.engine.dispose()
