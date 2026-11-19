import asyncio
import json
from time import time
from weakref import WeakSet

import aiohttp
from astrbot.api import logger
from astrbot.api.event import MessageChain
from bilibili_api import Credential, live
from yarl import URL

from .lifecycle import consume_task, finish_tasks
from .migration import import_legacy_eop
from .subscriptions import SubscriptionMonitor


def positive_id(value):
    if isinstance(value, bool):
        return None
    value = str(value).strip()
    return (int(value) or None) if len(value) <= 20 and value.isascii() and value.isdecimal() else None


def room_config(lines, extra_uids):
    watched = {uid for value in extra_uids if (uid := positive_id(value)) is not None}
    rooms = {}
    for line in lines:
        room, _, streamer = str(line).partition(':')
        room_id, streamer_uid = positive_id(room), positive_id(streamer)
        if room_id is None or streamer_uid is None:
            raise ValueError('每个直播间应填写 房间号:主播UID，且两个 ID 均为正整数')
        if room_id in rooms:
            raise ValueError(f'直播间 {room_id} 重复配置')
        rooms[room_id] = watched | {streamer_uid}
    return rooms


def label_event(kind, raw, watched, options):
    if kind == 'DANMU_MSG':
        info = raw.get('info', [])
        if isinstance(info, list) and len(info) >= 3 and isinstance(info[2], list) and len(info[2]) >= 2:
            uid, name = info[2][:2]
            if positive_id(uid) in watched and isinstance(info[1], str) and info[1].strip():
                return f'{name}：{info[1]}'
    if kind == 'message' and positive_id(raw.get('uid')) in watched:
        if isinstance(raw.get('message'), str) and raw['message'].strip():
            return f'{raw.get("username") or raw["uid"]}：{raw["message"]}'
    if kind in {'LIVE', 'live-start', 'PREPARING', 'live-end'} and options.get('notify_stream'):
        return '开播了' if kind in {'LIVE', 'live-start'} else '已下播'
    data = raw.get('data') if kind.isupper() else raw
    if not isinstance(data, dict):
        return None
    if kind in {'SUPER_CHAT_MESSAGE', 'superchat', 'super-chat'} and options.get('notify_superchat'):
        name = (data.get('user_info') or {}).get('uname') or data.get('username') or '观众'
        return f'醒目留言 · {name}：{data.get("message", "")}'
    threshold = max(0.0, float(options.get('gift_min_yuan', 0)))
    if kind == 'SEND_GIFT' and threshold and data.get('coin_type') == 'gold':
        total = data.get('total_coin')
        if isinstance(total, (int, float)) and total >= 1000 * threshold:
            return f'{data.get("uname", "观众")} 赠送 {data.get("giftName", "礼物")} ×{data.get("num", 1)}（¥{total / 1000:g}）'
    if kind == 'gift' and threshold and data.get('coinType') == 'gold':
        total = data.get('priceNormalized')
        if isinstance(total, (int, float)) and total >= threshold:
            return f'{data.get("username", "观众")} 赠送 {data.get("giftName", "礼物")} ×{data.get("giftAmount", 1)}（¥{total:g}）'
    return None


class MonitorRuntime:
    def __init__(self, context, config):
        self.context = context
        self.config = config
        self.tasks = set()
        self.cleanup_tasks = set()
        self.closed_clients = WeakSet()
        self.bridge_session = None
        self.clients = {}
        self.rooms = {}
        self.status = {}
        self.received = {}
        self.errors = ''
        self.stopping = False
        self.initialized = False
        self.migration_status = ''
        self.live_states = {}
        self.room_aliases = {}
        self.live_lock = asyncio.Lock()
        self.outbox = asyncio.Queue(maxsize=100)
        self.subscriptions = SubscriptionMonitor(config, self.send_notification, self.announce_live)

    async def initialize(self):
        if self.stopping or self.initialized:
            return
        self.initialized = True
        try:
            self.migration_status = import_legacy_eop(self.config)
        except (ValueError, OSError) as error:
            self.migration_status = f'旧 EoP 设置导入失败（{type(error).__name__}），请检查 WebUI 配置'
            logger.warning('[Jupiter] %s', self.migration_status)
        await self.subscriptions.initialize()
        try:
            self.rooms = room_config(self.config['rooms'], self.config['extra_uids'])
            bridge = URL(self.config.get('bridge_url', '').strip())
            if bridge and (bridge.scheme not in {'ws', 'wss'} or not bridge.host):
                raise ValueError('Event Bridge 地址应为 ws:// 或 wss:// 开头的完整地址')
        except ValueError as error:
            self.errors = str(error)
            logger.warning('[Jupiter] %s', self.errors)
            return
        if not self.rooms:
            return
        self.tasks.add(asyncio.create_task(self.deliver()))
        if bridge:
            self.tasks.add(asyncio.create_task(self.watch_bridge(bridge)))
        else:
            self.tasks.update(asyncio.create_task(self.watch_room(room)) for room in self.rooms)

    async def terminate(self):
        self.stopping = True
        subscription_tasks = self.subscriptions.stop()
        for client in list(self.clients.values()):
            self.stop_client(client)
        for task in self.tasks:
            if task is not asyncio.current_task():
                task.cancel()
        if self.bridge_session is not None:
            task = asyncio.create_task(self.bridge_session.close())
            self.cleanup_tasks.add(task)
            task.add_done_callback(consume_task)
        await finish_tasks(self.tasks | self.cleanup_tasks | subscription_tasks)

    async def send_notification(self, target, text):
        if self.stopping or asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        if target != self.config.get('target_umo', '').strip():
            raise RuntimeError('推送目标已改变')
        return await self.context.send_message(target, MessageChain().message(text))

    async def announce_live(self, target, room, current):
        async with self.live_lock:
            room = self.room_aliases.get(room, room)
            key = (target, room)
            if self.live_states.get(key) is current:
                return
            action = '开播' if current else '下播'
            sent = await self.send_notification(target, f'【Jupiter · {action}】B 站直播间 {room}\nhttps://live.bilibili.com/{room}')
            if not sent:
                raise RuntimeError('AstrBot 找不到绑定会话的适配器')
            self.live_states[key] = current

    def stop_client(self, client):
        if client in self.closed_clients:
            return
        self.closed_clients.add(client)
        client.remove_all_event_listener()
        # bilibili-api 17.4.2 has no public close method for a connecting or
        # failed LiveDanmaku. Its two task sets also outlive connect().
        for attribute in ('_LiveDanmaku__tasks', '_AsyncEvent__tasks'):
            tasks = list(getattr(client, attribute, ()))
            for task in tasks:
                if task is not asyncio.current_task():
                    task.cancel()
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                    task.add_done_callback(consume_task)
        transport = getattr(client, '_LiveDanmaku__client', None)
        socket = getattr(client, '_LiveDanmaku__ws', None)
        if transport is not None and socket is not None:
            task = asyncio.create_task(transport.ws_close(socket))
            self.cleanup_tasks.add(task)
            task.add_done_callback(self.cleanup_tasks.discard)
            task.add_done_callback(consume_task)

    async def watch_room(self, room):
        credential = Credential(
            sessdata=self.config.get('bili_sessdata') or None,
            dedeuserid=self.config.get('bili_dedeuserid') or None,
            buvid3=self.config.get('bili_buvid3') or None,
        )
        async def notice(packet):
            if self.stopping:
                return
            real_room = positive_id(packet.get('room_real_id'))
            if real_room:
                self.room_aliases[room] = real_room
            kind = packet.get('type')
            if kind == 'VERIFICATION_SUCCESSFUL':
                self.status[room] = '已连接'
            elif isinstance(packet.get('data'), dict):
                self.handle_event(room, kind, packet['data'])

        delay = 5
        while not self.stopping:
            client = live.LiveDanmaku(room, credential=credential, max_retry=0, retry_after=3)
            self.clients[room] = client
            client.add_event_listener('ALL', notice)
            self.status[room] = '连接中'
            try:
                await client.connect()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning('[Jupiter] 房间 %s 连接失败 (%s)', room, type(error).__name__)
            finally:
                self.stop_client(client)
            if self.stopping:
                return
            if self.status[room] == '已连接':
                delay = 5
            self.status[room] = '断线，等待重连'
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)

    async def watch_bridge(self, url):
        token = self.config.get('bridge_token', '').strip()
        protocols = ('laplace-event-bridge-role-client', token) if token else ()
        if token:
            url = url.update_query(token=token)
        delay = 5
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            self.bridge_session = session
            while not self.stopping:
                self.status['Bridge'] = '连接中'
                try:
                    async with session.ws_connect(url, protocols=protocols, heartbeat=30) as ws:
                        self.status['Bridge'] = '已连接'
                        delay = 5
                        async for packet in ws:
                            if self.stopping:
                                return
                            if packet.type == aiohttp.WSMsgType.TEXT:
                                await self.read_bridge(ws, packet.data)
                except (aiohttp.ClientError, ValueError, TimeoutError) as error:
                    logger.warning('[Jupiter] Bridge 连接失败 (%s)', type(error).__name__)
                if self.stopping:
                    return
                self.status['Bridge'] = '断线，等待重连'
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)

    async def read_bridge(self, ws, payload):
        if payload == 'ping':
            await ws.send_str('pong')
            return
        try:
            events = json.loads(payload)
        except ValueError:
            return
        if isinstance(events, dict):
            if events.get('type') == 'ping':
                await ws.send_json({'type': 'pong', 'timestamp': int(time() * 1000), 'respondingTo': events.get('timestamp')})
                return
            events = [events]
        if isinstance(events, list):
            for event in events:
                if isinstance(event, dict):
                    self.handle_event(positive_id(event.get('origin')), event.get('type'), event)

    def handle_event(self, room, kind, raw):
        if self.stopping or room not in self.rooms or not isinstance(kind, str):
            return
        if kind in {'message', 'DANMU_MSG'}:
            self.received[room] = self.received.get(room, 0) + 1
        target = self.config.get('target_umo', '').strip()
        if target and (text := label_event(kind, raw, self.rooms[room], self.config)):
            message = f'【Jupiter · 房间 {room}】{text[:700]}\nhttps://live.bilibili.com/{room}'
            stream = None
            if kind in {'LIVE', 'live-start', 'PREPARING', 'live-end'}:
                stream = (room, kind in {'LIVE', 'live-start'})
            try:
                self.outbox.put_nowait((target, message, stream))
            except asyncio.QueueFull:
                logger.warning('[Jupiter] 推送积压，跳过房间 %s 的一条通知', room)

    async def deliver(self):
        while not self.stopping:
            target, message, stream = await self.outbox.get()
            if not self.stopping and target == self.config.get('target_umo', '').strip():
                try:
                    if stream is not None:
                        await self.announce_live(target, *stream)
                    elif not await self.send_notification(target, message):
                        logger.warning('[Jupiter] AstrBot 找不到绑定会话的适配器')
                except Exception as error:
                    logger.warning('[Jupiter] QQ 通知发送失败 (%s)', type(error).__name__)
                await asyncio.sleep(1)

