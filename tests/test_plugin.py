import asyncio
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import web
import bilibili_api
import feedparser


ROOT = Path(__file__).resolve().parents[1]


class Config(dict):
    saves = 0

    def save_config(self):
        self.saves += 1


class Context:
    def __init__(self):
        self.sent = []
        self.fail = False

    async def send_message(self, target, chain):
        if self.fail:
            return False
        self.sent.append((target, chain.text))
        return True


class Chain:
    def message(self, text):
        self.text = text
        return self


class Star:
    def __init__(self, context):
        self.context = context


class Event:
    unified_msg_origin = 'qq:GroupMessage:888'

    def plain_result(self, text):
        return text


def decorator(*args):
    return lambda function: function


def load_plugin():
    tools = SimpleNamespace()
    modules = {
        'astrbot.api': SimpleNamespace(AstrBotConfig=Config, logger=logging.getLogger('jupiter-test')),
        'astrbot.api.event': SimpleNamespace(
            AstrMessageEvent=Event, MessageChain=Chain,
            filter=SimpleNamespace(command=decorator, permission_type=decorator, PermissionType=SimpleNamespace(ADMIN=1)),
        ),
        'astrbot.api.star': SimpleNamespace(Star=Star, Context=Context, StarTools=tools),
    }
    spec = importlib.util.spec_from_file_location('jupiter_fixture', ROOT / 'main.py', submodule_search_locations=[str(ROOT)])
    plugin = importlib.util.module_from_spec(spec)
    modules['jupiter_fixture'] = plugin
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(plugin)
        subscriptions = sys.modules['jupiter_fixture.subscriptions']
        lifecycle = sys.modules['jupiter_fixture.lifecycle']
    return plugin, subscriptions, lifecycle, tools


PLUGIN, SUBSCRIPTIONS, LIFECYCLE, TOOLS = load_plugin()


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name) / 'data'

        def data_dir(name):
            path = self.data / 'plugin_data' / name
            path.mkdir(parents=True, exist_ok=True)
            return path

        TOOLS.get_data_dir = data_dir
        schema = json.loads((ROOT / '_conf_schema.json').read_text(encoding='utf-8'))
        self.config = Config({key: field['default'] for key, field in schema.items()})
        self.config['target_umo'] = Event.unified_msg_origin
        self.context = Context()
        self.plugin = PLUGIN.Jupiter(self.context, self.config)
        self.addAsyncCleanup(self.plugin.terminate)

    async def pause_polling(self):
        await asyncio.Future()

    async def test_single_plugin_and_subscription_only(self):
        stars = [value for value in vars(PLUGIN).values() if isinstance(value, type) and value is not Star and issubclass(value, Star)]
        self.assertEqual(stars, [PLUGIN.Jupiter])
        self.config['bili_uids'] = ['45']
        self.plugin.subscriptions.poll = self.pause_polling
        await self.plugin.initialize()
        task = self.plugin.subscriptions.task
        self.assertIsNotNone(task)
        await self.plugin.initialize()
        self.assertIs(self.plugin.subscriptions.task, task)
        self.assertFalse(self.plugin.tasks)
        replies = [reply async for reply in self.plugin.show_status(Event())]
        self.assertIn('45', replies[0])
        self.assertEqual([reply async for reply in self.plugin.bind(Event())][0].startswith('Jupiter'), True)
        self.assertTrue(self.config.saves)
        replies = [reply async for reply in self.plugin.test(Event())]
        self.assertTrue(replies)
        self.assertEqual(len(self.context.sent), 1)

    async def test_bridge_message_and_shutdown(self):
        self.config.update(rooms=['123:45'], bridge_token='fixture')
        heartbeat = asyncio.Event()

        async def serve(request):
            self.assertEqual(request.query['token'], 'fixture')
            socket = web.WebSocketResponse(protocols=('laplace-event-bridge-role-client',))
            await socket.prepare(request)
            await socket.send_json({'type': 'ping', 'timestamp': 1234})
            pong = await socket.receive_json()
            self.assertEqual(pong['respondingTo'], 1234)
            await socket.send_json([
                {'type': 'message', 'origin': 123, 'uid': 0, 'message': 'unknown UID'},
                {'type': 'message', 'origin': 124, 'uid': 45, 'message': 'other room'},
                {'type': 'message', 'origin': 123, 'uid': 45, 'username': '主播', 'message': '版聊测试'},
            ])
            heartbeat.set()
            async for _ in socket:
                pass
            return socket

        app = web.Application()
        app.router.add_get('/', serve)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        self.config['bridge_url'] = f'ws://127.0.0.1:{runner.addresses[0][1]}/'
        await self.plugin.initialize()
        await asyncio.wait_for(heartbeat.wait(), timeout=1)
        for _ in range(40):
            if self.context.sent:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(self.context.sent), 1)
        self.assertIn('版聊测试', self.context.sent[0][1])
        await self.plugin.terminate()
        self.assertTrue(self.plugin.bridge_session.closed)
        self.assertTrue(all(task.done() for task in self.plugin.tasks))

    async def test_dynamic_baseline_retry_and_restart(self):
        self.config['bili_uids'] = ['45']
        self.plugin.subscriptions.poll = self.pause_polling
        await self.plugin.initialize()
        monitor = self.plugin.subscriptions

        class User:
            items = []

            def __init__(self, *args, **kwargs):
                pass

            async def get_dynamics_new(self):
                return {'items': self.items}

        with patch.object(SUBSCRIPTIONS.user, 'User', User):
            User.items = [{'id_str': '10'}]
            await monitor.check(None, None, Event.unified_msg_origin, 'dynamic', 45)
            self.assertFalse(self.context.sent)
            User.items = [{'id_str': '11'}, {'id_str': '9'}, {'id_str': '10'}]
            self.context.fail = True
            with self.assertRaises(RuntimeError):
                await monitor.check(None, None, Event.unified_msg_origin, 'dynamic', 45)
            self.context.fail = False
            await monitor.check(None, None, Event.unified_msg_origin, 'dynamic', 45)
            self.assertEqual(len(self.context.sent), 1)
            self.assertIn('/11', self.context.sent[0][1])
            monitor.state = json.loads(monitor.state_path.read_text(encoding='utf-8'))
            await monitor.check(None, None, Event.unified_msg_origin, 'dynamic', 45)
            self.assertEqual(len(self.context.sent), 1)

    async def test_live_notifications_from_both_sources_are_combined(self):
        self.config['bili_rooms'] = ['123']
        self.plugin.subscriptions.poll = self.pause_polling
        await self.plugin.initialize()
        monitor = self.plugin.subscriptions

        class Room:
            status = 0

            def __init__(self, *args, **kwargs):
                pass

            async def get_room_play_info(self):
                return {'live_status': self.status, 'room_id': 123}

        with patch.object(SUBSCRIPTIONS.live, 'LiveRoom', Room):
            await monitor.check(None, None, Event.unified_msg_origin, 'live', 123)
            Room.status = 1
            await asyncio.gather(
                monitor.check(None, None, Event.unified_msg_origin, 'live', 123),
                self.plugin.announce_live(Event.unified_msg_origin, 123, True),
            )
        self.assertEqual(len(self.context.sent), 1)
        self.plugin.room_aliases[12] = 123
        await self.plugin.announce_live(Event.unified_msg_origin, 12, True)
        self.assertEqual(len(self.context.sent), 1)

    async def test_rss_baseline_and_new_entry(self):
        self.config['feed_urls'] = ['https://example.com/feed']
        self.plugin.subscriptions.poll = self.pause_polling
        await self.plugin.initialize()
        monitor = self.plugin.subscriptions
        entries = SUBSCRIPTIONS.feed_entries(b'<rss version="2.0"><channel><title>Feed</title><item><guid>1</guid><title>old</title></item></channel></rss>', 'https://example.com/feed')

        async def fetch(*args):
            return entries

        with patch.object(SUBSCRIPTIONS, 'read_feed', fetch):
            await monitor.check(None, None, Event.unified_msg_origin, 'feed', self.config['feed_urls'][0])
            entries.append(('second', 'new RSS item'))
            await monitor.check(None, None, Event.unified_msg_origin, 'feed', self.config['feed_urls'][0])
            await monitor.check(None, None, Event.unified_msg_origin, 'feed', self.config['feed_urls'][0])
        self.assertEqual(self.context.sent, [(Event.unified_msg_origin, 'new RSS item')])

    async def test_legacy_config_and_state_migration(self):
        legacy_dir = self.data / 'config'
        legacy_dir.mkdir(parents=True)
        legacy_path = legacy_dir / 'astrbot_plugins_EoP_config.json'
        legacy_path.write_text(json.dumps({
            'bili_uids': ['45'], 'bili_rooms': ['123'], 'feed_urls': [],
            'target_umo': 'qq:GroupMessage:999', 'bili_sessdata': 'fixture-secret',
        }), encoding='utf-8')
        state_dir = TOOLS.get_data_dir('astrbot_plugin_eop')
        key = hashlib.sha256(json.dumps([Event.unified_msg_origin, 'dynamic', 45]).encode()).hexdigest()
        state = {key: '10'}
        (state_dir / 'subscription_state.json').write_text(json.dumps(state), encoding='utf-8')
        self.plugin.subscriptions.poll = self.pause_polling
        await self.plugin.initialize()
        self.assertEqual(self.config['bili_uids'], ['45'])
        self.assertEqual(self.config['target_umo'], Event.unified_msg_origin)
        self.assertEqual(self.config['bili_sessdata'], 'fixture-secret')
        self.assertEqual(self.plugin.subscriptions.state, state)
        self.assertTrue(self.config['legacy_eop_imported'])
        self.assertEqual(json.loads(legacy_path.read_text())['target_umo'], 'qq:GroupMessage:999')
        await self.plugin.terminate()
        restarted = PLUGIN.Jupiter(self.context, self.config)
        restarted.subscriptions.poll = self.pause_polling
        try:
            await restarted.initialize()
            self.assertEqual(restarted.subscriptions.state, state)
            self.assertEqual(self.config.saves, 1)
        finally:
            await restarted.terminate()

    async def test_invalid_subscription_config_still_allows_realtime(self):
        self.config.update(rooms=['123:45'], bili_uids=['bad'])

        async def watch(room):
            await asyncio.Future()

        self.plugin.watch_room = watch
        await self.plugin.initialize()
        self.assertTrue(self.plugin.subscriptions.problem)
        self.assertEqual(len(self.plugin.tasks), 2)

    async def test_invalid_realtime_config_still_allows_subscriptions(self):
        self.config.update(rooms=['bad'], bili_uids=['45'])
        self.plugin.subscriptions.poll = self.pause_polling
        await self.plugin.initialize()
        self.assertTrue(self.plugin.errors)
        self.assertIsNotNone(self.plugin.subscriptions.task)

    async def test_shutdown_deadline_covers_both_workers(self):
        release = asyncio.Event()

        async def stubborn():
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass

        tasks = [asyncio.create_task(stubborn()) for _ in range(3)]
        self.plugin.tasks.add(tasks[0])
        self.plugin.subscriptions.task = tasks[1]
        self.plugin.subscriptions.check_task = tasks[2]
        await asyncio.sleep(0)
        try:
            with patch.object(LIFECYCLE, 'SHUTDOWN_TIMEOUT', 0.03):
                await asyncio.wait_for(self.plugin.terminate(), timeout=0.2)
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin.send_notification(Event.unified_msg_origin, 'stale')
            with self.assertRaises(asyncio.CancelledError):
                self.plugin.subscriptions.save_state('stale', True)
        finally:
            release.set()
            await asyncio.gather(*tasks)

    async def test_repeated_reload_cleans_all_workers(self):
        class Client:
            def __init__(self, *args, **kwargs):
                self._LiveDanmaku__tasks = []
                self._AsyncEvent__tasks = set()
                self._LiveDanmaku__client = self
                self._LiveDanmaku__ws = 1
                self.closed = False

            def add_event_listener(self, *args):
                pass

            def remove_all_event_listener(self):
                pass

            async def connect(self):
                self._LiveDanmaku__tasks.append(asyncio.create_task(asyncio.sleep(100)))
                await asyncio.Future()

            async def ws_close(self, socket):
                self.closed = True

        for _ in range(20):
            config = Config(self.config, rooms=['123:45'], bili_uids=['45'])
            plugin = PLUGIN.Jupiter(self.context, config)
            plugin.subscriptions.poll = self.pause_polling
            with patch.object(PLUGIN.live, 'LiveDanmaku', Client):
                await plugin.initialize()
                await asyncio.sleep(0)
                client = plugin.clients[123]
                children = list(client._LiveDanmaku__tasks)
                await asyncio.wait_for(plugin.terminate(), timeout=0.3)
                await plugin.terminate()
                self.assertTrue(client.closed)
                self.assertTrue(plugin.subscriptions.task.done())
                self.assertTrue(all(task.done() for task in children))
                self.assertTrue(all(task.done() for task in plugin.tasks | plugin.cleanup_tasks))


if __name__ == '__main__':
    unittest.main()
