"""Explicitly enabled, verified exits shared by all account connections."""
import asyncio
from ipaddress import ip_address
from uuid import uuid4
import yaml

from src.service.contracts import EgressGroupInput, ServiceError
from src.service.worker_process import prepare_worker_executable


class EgressService:
    def __init__(self, store, sessions, workers, events):
        self.store, self.sessions, self.workers, self.events = store, sessions, workers, events
        self._changing = set()
        self._verified = set()
        self._creating = set()
        self._groups_cache: dict[str, dict] | None = None
        self._write_lock = asyncio.Lock()

    async def load(self):
        groups = await asyncio.to_thread(self.store.list_egress_groups)
        self._groups_cache = {group["group_id"]: group for group in groups}

    def _groups(self):
        if self._groups_cache is None:
            self._groups_cache = {group["group_id"]: group for group in self.store.list_egress_groups()}
        return self._groups_cache

    def _group(self, group_id):
        group = self._groups().get(group_id)
        if group is None:
            raise ServiceError("egress_not_found", "出口分组不存在", 404)
        return group

    def get(self, group_id):
        return self._profile(self._group(group_id))

    async def get_async(self, group_id):
        if self._groups_cache is None:
            await self.load()
        return self.get(group_id)

    def _profile(self, group):
        group_id = group["group_id"]
        if self.workers.mihomo:
            group = {**group, 'routing_confirmed': group['clash_policy'] == 'DIRECT' or (
                self.workers.mihomo.state == 'ready' and group['clash_policy'] in self.workers.mihomo.nodes)}
        profile = self.workers.profile(group_id)
        return {**group, "max_online": 25, "enabled": group.get("enabled", False),
                **profile, **self.sessions.egress_usage(group_id),
                "active": group_id in self._verified and group.get("enabled", False)
                          and group["routing_confirmed"] and group_id not in self._changing,
                "waiting_count": self.sessions.waiting_count}

    def list(self):
        return [self._profile(group) for group in self._groups().values()]

    async def list_async(self):
        if self._groups_cache is None:
            await self.load()
        return self.list()

    def available(self):
        candidates = (self._profile(group) for group_id, group in (self._groups_cache or {}).items()
                      if group.get("enabled") and group_id in self._verified and group_id not in self._changing)
        return [group for group in candidates if group["active"] and group.get("exit_ip")]

    def check(self, group_id, *, connecting=False):
        if group_id in self._changing:
            raise ServiceError("egress_changing", "出口正在修改，请稍后重试", 409)
        group = self.get(group_id)
        if connecting and not group["active"]:
            raise ServiceError("egress_unconfirmed", "出口需要检测、确认规则并启用", 409)
        return group

    def _enable_check(self, group_id, value):
        if not value.enabled:
            return
        if not value.routing_confirmed or group_id not in self._verified:
            raise ServiceError("egress_unconfirmed", "请先检测出口并确认 Clash 规则", 409)
        ip = self.workers.profile(group_id)["exit_ip"]
        for group in self.list():
            if group["group_id"] != group_id and group.get("exit_ip") == ip:
                if group["enabled"] or group["online_count"]:
                    raise ServiceError("egress_duplicate_ip", "相同公网 IP 已在池中启用或仍有连接", 409)
        for session in self.sessions._sessions.values():
            if session.slot and session.egress_group_id != group_id and session.exit_ip == ip:
                raise ServiceError("egress_duplicate_ip", "该公网 IP 仍有其他出口的连接", 409)

    def _new_group(self, value):
        value = value if isinstance(value, EgressGroupInput) else EgressGroupInput.model_validate(value)
        if self.workers.mihomo:
            self.workers.mihomo.validate_policy(value.clash_policy)
            value = value.model_copy(update={'routing_confirmed': True})
        if value.enabled:
            raise ServiceError("egress_unconfirmed", "新出口须先检测并确认后启用", 409)
        if value.name in self._creating:
            raise ServiceError("egress_duplicate", "出口名称已存在", 409)
        self._check_name(value.name)
        return value

    def _check_name(self, name, group_id=None):
        if any(group["name"] == name and key != group_id for key, group in self._groups().items()):
            raise ServiceError("egress_duplicate", "出口名称已存在", 409)

    def create(self, value):
        value, group_id = self._new_group(value), uuid4().hex
        prepare_worker_executable(self.store.directory, group_id)
        self._remember(self.store.save_egress_group(group_id, value.model_dump()))
        return self.get(group_id)

    async def create_async(self, value):
        value, group_id = self._new_group(value), uuid4().hex
        self._creating.add(value.name)
        try:
            await asyncio.to_thread(prepare_worker_executable, self.store.directory, group_id)
            async with self._write_lock:
                self._check_name(value.name)
                await self._save_group(group_id, value.model_dump())
            return self.get(group_id)
        finally:
            self._creating.discard(value.name)

    async def _save_group(self, group_id, config):
        group = await asyncio.to_thread(self.store.save_egress_group, group_id, config)
        self._remember(group)

    def _remember(self, group):
        self._groups()[group["group_id"]] = group
        self.events.publish("egress", {"group_id": group["group_id"]})

    async def import_nodes(self):
        groups = self._groups().values()
        policies = {group['clash_policy'] for group in groups}
        names = {group['name'] for group in groups}
        for node in self.workers.mihomo.nodes:
            if node not in policies:
                name, suffix = node, 2
                while name in names:
                    name = f'{node[:70]} ({suffix})'
                    suffix += 1
                await self.create_async({'name': name, 'clash_policy': node})
                names.add(name)

    def runtime(self):
        return self.workers.mihomo.status() if self.workers.mihomo else {
            'mode': 'external', 'state': 'external', 'nodes': [], 'pid': None}

    def _idle(self, group_id):
        if self.sessions.egress_usage(group_id)["online_count"]:
            raise ServiceError("egress_busy", "出口仍有连接或正在清理，请先停用并断开", 409)

    async def update(self, group_id, value):
        original = self.check(group_id)
        value = value if isinstance(value, EgressGroupInput) else EgressGroupInput.model_validate(value)
        if self.workers.mihomo:
            self.workers.mihomo.validate_policy(value.clash_policy)
            value = value.model_copy(update={'routing_confirmed': True})
        if value.name in self._creating:
            raise ServiceError("egress_duplicate", "出口名称已存在", 409)
        self._check_name(value.name, group_id)
        route_changed = original["clash_policy"] != value.clash_policy
        if route_changed:
            self._idle(group_id)
            value = value.model_copy(update={"routing_confirmed": bool(self.workers.mihomo), "enabled": False})
        self._enable_check(group_id, value)
        self._changing.add(group_id)
        try:
            if route_changed:
                self._verified.discard(group_id)
                await self.workers.stop(group_id)
            async with self._write_lock:
                self._check_name(value.name, group_id)
                self._enable_check(group_id, value)
                await self._save_group(group_id, value.model_dump())
        finally:
            self._changing.discard(group_id)
        self.sessions.wake()
        return self.get(group_id)

    async def delete(self, group_id):
        self.check(group_id)
        self._idle(group_id)
        self._changing.add(group_id)
        try:
            self._verified.discard(group_id)
            await self.workers.stop(group_id)
            async with self._write_lock:
                await asyncio.to_thread(self.store.delete_egress_group, group_id)
                self._groups().pop(group_id, None)
        finally:
            self._changing.discard(group_id)
        self.events.publish("egress", {"group_id": group_id, "deleted": True})
        self.sessions.wake()

    def failed(self, group_id):
        self._verified.discard(group_id)
        group = self._group(group_id)
        self._remember(self.store.save_egress_group(group_id, {**group, "enabled": False}))
        self.sessions.wake()

    async def failed_async(self, group_id):
        self._verified.discard(group_id)
        async with self._write_lock:
            group = self._groups().get(group_id)
            if group is not None:
                await self._save_group(group_id, {**group, "enabled": False})
        self.sessions.wake()

    async def probe(self, group_id):
        self.check(group_id)
        if self.workers.mihomo:
            group = self._group(group_id)
            self.workers.mihomo.proxy_url(group['clash_policy'])
        self._idle(group_id)
        self._verified.discard(group_id)
        self._changing.add(group_id)
        try:
            result = await self.workers.probe(group_id)
            self.workers.worker(group_id).exit_ip = str(ip_address(result["exit_ip"]))
            self._verified.add(group_id)
            group = self.get(group_id)
            self._enable_check(group_id, EgressGroupInput.model_validate(
                {k: v for k, v in group.items() if k in EgressGroupInput.model_fields}))
        except BaseException:
            await self.failed_async(group_id)
            raise
        finally:
            self._changing.discard(group_id)
        self.sessions.wake()
        return self.get(group_id)

    def rules(self):
        if self.workers.mihomo:
            return {'yaml': '', 'instructions': '项目已托管 Mihomo，无需外部进程分流规则。'}
        rules = [f"PROCESS-PATH,{prepare_worker_executable(self.store.directory, g['group_id'])},{g['clash_policy']}"
                 for g in self.store.list_egress_groups()]
        return {"yaml": yaml.safe_dump({"mode": "rule", "find-process-mode": "strict", "rules": rules},
                                       allow_unicode=True, sort_keys=False),
                "instructions": "将 rules 合并到 Clash 规则最前面。直连出口使用 DIRECT；程序不自动改写 Clash。"}
