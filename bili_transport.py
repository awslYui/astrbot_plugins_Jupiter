"""An owned aiohttp WebSocket transport for the pinned Bilibili SDK.

The SDK selects a process-wide client, which may be curl_cffi installed by
another plugin. Its 17.4.2 curl adapter reads AsyncWebSocket's raw curl handle
from a thread instead of using the public async receive API. Never use that
adapter for Jupiter's long-running WebSocket connections.
"""

import asyncio
import importlib.util

from bilibili_api import live
from bilibili_api.clients.AioHTTPClient import AioHTTPClient


class OwnedAioHTTPClient(AioHTTPClient):
    async def ws_close(self, cnt):
        # The SDK adapter retains every closed socket in this dictionary.
        # Pop before awaiting so concurrent cleanup is idempotent.
        socket = self._AioHTTPClient__wss.pop(cnt, None)
        if socket is not None:
            await socket.close()

    async def close(self):
        # Closing only ClientSession leaves WebSocket response objects open.
        # Runtime's shared shutdown deadline also bounds this cleanup.
        try:
            await asyncio.gather(*(self.ws_close(cnt) for cnt in list(self._AioHTTPClient__wss)), return_exceptions=True)
        finally:
            await super().close()


class LiveTransport:
    def __init__(self):
        self.client = None
        self.stopping = False

    def get_client(self):
        if self.stopping:
            raise RuntimeError('Jupiter 弹幕连接正在关闭')
        if self.client is None:
            self.client = OwnedAioHTTPClient(timeout=20)
        return self.client

    async def close(self):
        self.stopping = True
        if self.client is not None:
            await self.client.close()
            self.client._AioHTTPClient__wss.clear()


def create_danmaku(room, **kwargs):
    transport = LiveTransport()
    # Execute a private live-module namespace, keeping the SDK's existing
    # authentication, packet parsing, heartbeat and event handling unchanged.
    # Its relative imports still resolve to the installed, pinned SDK. Do not
    # register this namespace in sys.modules or replace live.get_client: other
    # plugins must retain their own client selection and sessions.
    spec = importlib.util.spec_from_file_location('bilibili_api._jupiter_live', live.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.get_client = transport.get_client
    client = module.LiveDanmaku(room, **kwargs)
    client._jupiter_transport = transport
    return client
