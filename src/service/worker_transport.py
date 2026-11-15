"""Authenticated loopback control channel, independent of standard streams."""
import asyncio
import os
import secrets


MESSAGE_LIMIT = 16 * 1024 * 1024
PORT_ENV = "AOLA_WORKER_PORT"
TOKEN_ENV = "AOLA_WORKER_TOKEN"


class WorkerListener:
    def __init__(self):
        self.token = secrets.token_hex(32)
        self.port = None
        self.server = None
        self.connected = asyncio.get_running_loop().create_future()
        self.handshakes = set()
        self.claimed = False

    async def start(self):
        self.server = await asyncio.start_server(self._authenticate, "127.0.0.1", 0, limit=MESSAGE_LIMIT)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _authenticate(self, reader, writer):
        task = asyncio.current_task()
        self.handshakes.add(task)
        accepted = False
        try:
            token = await asyncio.wait_for(reader.readexactly(65), 5)
            if not secrets.compare_digest(token, (self.token + "\n").encode("ascii")):
                return
            if self.connected.done():
                return
            writer.write(b"ok\n")
            await writer.drain()
            if not self.connected.done():
                self.connected.set_result((reader, writer))
                accepted = True
        except (ConnectionError, OSError, asyncio.IncompleteReadError, TimeoutError):
            pass
        finally:
            if not accepted:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
            self.handshakes.discard(task)

    async def accept(self, timeout=15):
        channel = await asyncio.wait_for(asyncio.shield(self.connected), timeout)
        self.claimed = True
        return channel

    async def close(self):
        if self.server is not None:
            self.server.close()
        for task in list(self.handshakes):
            task.cancel()
        await asyncio.gather(*list(self.handshakes), return_exceptions=True)
        if not self.connected.done():
            self.connected.cancel()
        elif not self.claimed and not self.connected.cancelled():
            _, writer = self.connected.result()
            writer.close()
            await writer.wait_closed()
        # Server.wait_closed also waits for accepted connections on Python 3.12.
        # A claimed control connection deliberately remains alive after startup.
        if self.server is not None and not self.claimed:
            await self.server.wait_closed()


async def open_worker_connection():
    port = int(os.environ.pop(PORT_ENV))
    token = os.environ.pop(TOKEN_ENV)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port, limit=MESSAGE_LIMIT), 15)
    try:
        writer.write((token + "\n").encode("ascii"))
        await writer.drain()
        if await asyncio.wait_for(reader.readexactly(3), 5) != b"ok\n":
            raise ConnectionError("Worker control authentication failed")
    except BaseException:
        writer.close()
        await writer.wait_closed()
        raise
    return reader, writer
