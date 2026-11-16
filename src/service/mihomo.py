"""Service-owned Mihomo with fixed, authenticated loopback routes per node."""
import asyncio
from copy import deepcopy
import os
from pathlib import Path
import secrets
import socket
import sys

import yaml

from src.config import PROJECT_ROOT
from src.service.contracts import ServiceError
from src.service.worker_process import WorkerJob


class Mihomo:
    def __init__(self, config, directory, binary=None):
        self.source = Path(config).resolve()
        self.directory = Path(directory).resolve() / 'mihomo'
        self.binary = Path(binary).resolve() if binary else self.source.parent / (
            'mihomo.exe' if os.name == 'nt' else 'mihomo')
        self.nodes = []
        self.routes = {}
        self.process = self.monitor = self.job = self.log = None
        self.state = 'offline'
        self.on_lost = None
        self.password = secrets.token_hex(24)

    def status(self):
        return {'mode': 'managed', 'state': self.state, 'nodes': list(self.nodes),
                'pid': self.process.pid if self.process and self.process.returncode is None else None}

    def validate_policy(self, policy):
        if policy != 'DIRECT' and policy not in self.nodes:
            raise ServiceError('proxy_node_missing', '节点不在 mihomo/nodes.yaml 中，请检查名称并重启后台')

    def proxy_url(self, policy):
        self.validate_policy(policy)
        if policy == 'DIRECT':
            return None
        if self.state != 'ready' or self.process.returncode is not None:
            raise ServiceError('proxy_unavailable', 'Mihomo 未运行，代理出口不可用', 503)
        return f'socks5://aola:{self.password}@127.0.0.1:{self.routes[policy]}'

    def configuration(self):
        try:
            raw = yaml.safe_load(self.source.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError, yaml.YAMLError):
            raise ServiceError('proxy_config', f'无法读取节点配置：{self.source}') from None
        proxies = raw.get('proxies') if isinstance(raw, dict) else None
        if not isinstance(proxies, list) or not proxies:
            raise ServiceError('proxy_config', '节点配置须包含非空 proxies 数组；请将实际节点写入该数组')
        names = []
        for proxy in proxies:
            name = proxy.get('name') if isinstance(proxy, dict) else None
            if (not isinstance(name, str) or not name.strip() or name != name.strip()
                    or len(name) > 80 or any(c in name for c in ',\r\n') or name in names
                    or name.upper() in {'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'GLOBAL'}
                    or proxy.get('type') in {'direct', 'reject', 'pass'}):
                raise ServiceError('proxy_config', '代理节点名称须唯一、长度不超过 80，且不能为直连或内置策略')
            names.append(name)
        self.nodes = names
        # Do not import a subscription's system ports, TUN, controller or routing rules.
        return {'proxies': deepcopy(proxies), 'mode': 'rule', 'allow-lan': False,
                'log-level': 'warning', 'ipv6': False, 'find-process-mode': 'off',
                'rules': ['MATCH,REJECT'], 'listeners': []}

    async def spawn(self, arguments):
        command = [str(self.binary), *arguments]
        environment = dict(os.environ)
        if sys.platform == 'linux':
            # Bind before exec: a crashed backend must not leave its proxy running.
            command = [sys.executable, '-B', '-m', 'src.service.mihomo_host',
                       str(os.getpid()), *command]
            environment['PYTHONPATH'] = str(PROJECT_ROOT) + os.pathsep + environment.get('PYTHONPATH', '')
        self.process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.DEVNULL, stdout=self.log, stderr=self.log,
            env=environment, **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
        self.job.assign(self.process.pid)

    async def authenticate_listener(self, port):
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        try:
            writer.write(b'\x05\x01\x02')
            await writer.drain()
            if await reader.readexactly(2) != b'\x05\x02':
                raise OSError('Unexpected SOCKS listener')
            password = self.password.encode('ascii')
            writer.write(b'\x01\x04aola' + bytes([len(password)]) + password)
            await writer.drain()
            if await reader.readexactly(2) != b'\x01\x00':
                raise OSError('Unexpected SOCKS listener')
        finally:
            writer.close()
            await writer.wait_closed()

    async def start(self):
        config = self.configuration()
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise ServiceError('proxy_binary', f'缺少可执行的 Mihomo：{self.binary}')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        reservations = []
        try:
            for index, name in enumerate(self.nodes):
                reservation = socket.socket()
                reservations.append(reservation)
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
                self.routes[name] = port
                config['listeners'].append({'name': f'node-{index}', 'type': 'socks',
                    'listen': '127.0.0.1', 'port': port, 'proxy': name, 'udp': False,
                    'users': [{'username': 'aola', 'password': self.password}]})
            config_path = self.directory / 'config.yaml'
            with os.fdopen(os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                           'w', encoding='utf-8') as output:
                yaml.safe_dump(config, output, allow_unicode=True, sort_keys=False)
            config_path.chmod(0o600)
            self.log = os.fdopen(os.open(self.directory / 'mihomo.log',
                os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'ab', buffering=0)
            self.job = WorkerJob()
            self.state = 'starting'
            args = ['-d', str(self.directory), '-f', str(config_path)]
            await self.spawn(['-t', *args])
            if await asyncio.wait_for(self.process.wait(), 30) != 0:
                raise ServiceError('proxy_config', f'Mihomo 配置检查失败，详见 {self.directory / "mihomo.log"}')
            for reservation in reservations:
                reservation.close()
            await self.spawn(args)
            async with asyncio.timeout(20):
                for port in self.routes.values():
                    while True:
                        if self.process.returncode is not None:
                            raise ServiceError('proxy_start', f'Mihomo 启动失败，详见 {self.directory / "mihomo.log"}')
                        try:
                            await asyncio.wait_for(self.authenticate_listener(port), .5)
                            break
                        except (OSError, EOFError, TimeoutError):
                            await asyncio.sleep(.05)
            self.state = 'ready'
            self.monitor = asyncio.create_task(self.watch())
        except BaseException:
            await self.close()
            self.state = 'failed'
            raise
        finally:
            for reservation in reservations:
                reservation.close()

    async def watch(self):
        await self.process.wait()
        self.state = 'failed'
        if self.on_lost:
            await self.on_lost()

    async def close(self):
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None
        try:
            if self.process and self.process.returncode is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        finally:
            if self.job:
                self.job.close()
                self.job = None
            if self.log:
                self.log.close()
                self.log = None
            self.state = 'offline'
