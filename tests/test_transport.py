import asyncio
import json
import struct
import sys
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web
import bilibili_api
from bilibili_api import live
from bilibili_api.utils import network

import test_plugin as fixture


TRANSPORT = fixture.TRANSPORT


def packet(operation, body, version=0):
    if not isinstance(body, bytes):
        body = json.dumps(body).encode()
    return struct.pack('>IHHII', len(body) + 16, 16, version, operation, 1) + body


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.PluginTests.asyncSetUp(self)
        self.connected = asyncio.Event()
        self.pong = asyncio.Event()
        self.sent = asyncio.Event()
        self.sockets = []
        self.created = []
        self.auth = []
        self.close_after_auth = False
        self.malformed_after_auth = False
        self.original_factory = fixture.RUNTIME.create_danmaku
        self.original_create = TRANSPORT.OwnedAioHTTPClient.ws_create
        self.context_send = self.context.send_message

        async def send(target, chain):
            result = await self.context_send(target, chain)
            self.sent.set()
            return result

        self.context.send_message = send

        async def serve(request):
            socket = web.WebSocketResponse()
            self.sockets.append(socket)
            await socket.prepare(request)
            auth = await socket.receive_bytes(timeout=2)
            self.assertEqual(struct.unpack('>IHHII', auth[:16])[3], 7)
            self.auth.append(json.loads(auth[16:]))
            await socket.send_bytes(packet(8, {'code': 0}, 1))
            await socket.send_bytes(packet(5, {
                'cmd': 'DANMU_MSG',
                'info': [[], 'fixture room message', [45, 'streamer']],
            }))
            self.connected.set()
            if self.close_after_auth:
                await socket.close()
                return socket
            if self.malformed_after_auth:
                await socket.send_bytes(b'bad')
            async for incoming in socket:
                if incoming.type == aiohttp.WSMsgType.BINARY:
                    operation = struct.unpack('>IHHII', incoming.data[:16])[3]
                    if operation == 2:
                        self.pong.set()
                        await socket.send_bytes(packet(3, struct.pack('>I', 1), 1))
            return socket

        app = web.Application()
        app.router.add_get('/sub', serve)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        self.addAsyncCleanup(self.owner.terminate)
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        self.url = f'ws://127.0.0.1:{runner.addresses[0][1]}/sub'

        async def local_create(transport, url='', params=None, headers=None):
            return await self.original_create(transport, self.url, params or {}, headers or {})

        class Api:
            def __init__(self, *args, **kwargs):
                pass

            def update_params(self, **kwargs):
                return self

            @property
            async def result(self):
                return {}

        def create(room, **kwargs):
            client = self.original_factory(room, **kwargs)
            self.created.append(client)
            namespace = client.connect.__func__.__globals__

            async def room_id(room):
                return 123

            async def conf(room):
                return {'token': 'fixture-token', 'host_list': [{'host': 'fixture.invalid', 'wss_port': 443}]}

            namespace['LiveRoom'].get_room_id = room_id
            namespace['LiveRoom'].get_danmu_info = conf
            namespace['Api'] = Api
            return client

        self.enterContext(patch.object(TRANSPORT.OwnedAioHTTPClient, 'ws_create', local_create))
        self.enterContext(patch.object(fixture.RUNTIME, 'create_danmaku', create))
        # Any use of the process-wide transport (e.g. curl_cffi installed by
        # another plugin) is a regression. No external HTTP requests are made.
        self.foreign = self.enterContext(patch.object(network, 'get_client', side_effect=AssertionError('global SDK transport used')))
        self.config.update(rooms=['123:45'], bili_dedeuserid='45', bili_buvid3='fixture-buvid')

    async def test_real_sdk_auth_messages_and_heartbeat_use_owned_aiohttp(self):
        selected = bilibili_api.get_selected_client()
        shared = {name: dict(pool) for name, pool in network.session_pool.items()}
        original_getter = live.get_client
        await self.owner.initialize()
        await asyncio.wait_for(self.sent.wait(), timeout=2)
        await asyncio.wait_for(self.pong.wait(), timeout=2)
        self.assertIn('fixture room message', self.context.sent[0][1])
        self.assertEqual(self.auth[0]['uid'], 45)
        self.assertEqual(self.auth[0]['roomid'], 123)
        self.assertEqual(self.auth[0]['buvid'], 'fixture-buvid')
        transport = self.created[0]._jupiter_transport.client
        self.assertIsInstance(transport.get_wrapped_session(), aiohttp.ClientSession)
        await asyncio.wait_for(self.owner.terminate(), timeout=1)
        self.assertTrue(transport.get_wrapped_session().closed)
        self.assertFalse(transport._AioHTTPClient__wss)
        self.assertEqual(bilibili_api.get_selected_client(), selected)
        self.assertEqual(network.session_pool, shared)
        self.assertIs(live.get_client, original_getter)
        self.foreign.assert_not_called()

    async def test_real_sdk_repeated_reload_closes_owned_sessions_and_tasks(self):
        for _ in range(8):
            owner = fixture.PLUGIN.Jupiter(self.context, self.config)
            self.sent.clear()
            try:
                await owner.initialize()
                await asyncio.wait_for(self.sent.wait(), timeout=2)
            finally:
                await asyncio.wait_for(owner.terminate(), timeout=1)
            transport = self.created[-1]._jupiter_transport
            self.assertTrue(transport.stopping)
            self.assertTrue(transport.client.get_wrapped_session().closed)
            self.assertTrue(all(task.done() for task in owner.runtime.tasks | owner.runtime.cleanup_tasks))
        self.foreign.assert_not_called()

    async def test_stopping_one_runtime_does_not_close_another(self):
        other = fixture.PLUGIN.Jupiter(self.context, self.config)
        self.addAsyncCleanup(other.terminate)
        await self.owner.initialize()
        await asyncio.wait_for(self.sent.wait(), timeout=2)
        self.sent.clear()
        await other.initialize()
        await asyncio.wait_for(self.sent.wait(), timeout=2)
        first, second = self.created[-2:]
        self.assertIsNot(first._jupiter_transport.client, second._jupiter_transport.client)
        await self.owner.terminate()
        self.assertTrue(first._jupiter_transport.client.get_wrapped_session().closed)
        self.assertFalse(second._jupiter_transport.client.get_wrapped_session().closed)
        self.assertFalse(other.runtime.stopping)

    async def test_factory_has_no_network_side_effects_before_connect(self):
        client = self.original_factory(123, credential=bilibili_api.Credential())
        self.assertIsNone(client._jupiter_transport.client)
        self.assertNotIn('bilibili_api._jupiter_live', sys.modules)
        self.assertIsNot(type(client), live.LiveDanmaku)
        await client._jupiter_transport.close()
        with self.assertRaises(RuntimeError):
            client._jupiter_transport.get_client()

    async def wait_for_backoff(self):
        async with asyncio.timeout(2):
            while self.plugin.status.get(123) != '断线，等待重连':
                await asyncio.sleep(0.01)

    async def test_peer_disconnect_closes_session_and_enters_backoff(self):
        self.close_after_auth = True
        await self.owner.initialize()
        await asyncio.wait_for(self.connected.wait(), timeout=2)
        await self.wait_for_backoff()
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.created), 1)
        self.assertTrue(self.created[0]._jupiter_transport.client.get_wrapped_session().closed)
        self.assertFalse(self.plugin.stopping)
        self.foreign.assert_not_called()

    async def test_malformed_packet_does_not_escape_monitor_task(self):
        self.malformed_after_auth = True
        await self.owner.initialize()
        await self.wait_for_backoff()
        self.assertTrue(self.created[0]._jupiter_transport.client.get_wrapped_session().closed)
        self.assertFalse(self.plugin.stopping)
        self.assertTrue(all(not task.done() for task in self.plugin.tasks))


