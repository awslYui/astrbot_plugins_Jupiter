import asyncio
import json
from contextlib import suppress
from time import time

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from bilibili_api import Credential, live
from yarl import URL


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


class Jupiter(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.tasks = []
        self.clients = {}
        self.rooms = {}
        self.status = {}
        self.received = {}
        self.errors = ''
        self.stopping = False
        self.outbox = asyncio.Queue(maxsize=100)

    async def initialize(self):
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
        self.tasks.append(asyncio.create_task(self.deliver()))
        if bridge:
            self.tasks.append(asyncio.create_task(self.watch_bridge(bridge)))
        else:
            self.tasks.extend(asyncio.create_task(self.watch_room(room)) for room in self.rooms)

    async def terminate(self):
        self.stopping = True
        for client in self.clients.values():
            if client.get_status() == live.LiveDanmaku.STATUS_ESTABLISHED:
                with suppress(Exception):
                    await asyncio.wait_for(client.disconnect(), timeout=3)
            client.remove_all_event_listener()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def watch_room(self, room):
        credential = Credential(
            sessdata=self.config.get('bili_sessdata') or None,
            dedeuserid=self.config.get('bili_dedeuserid') or None,
            buvid3=self.config.get('bili_buvid3') or None,
        )
        client = live.LiveDanmaku(room, credential=credential, max_retry=0, retry_after=3)
        self.clients[room] = client

        async def notice(packet):
            kind = packet.get('type')
            if kind == 'VERIFICATION_SUCCESSFUL':
                self.status[room] = '已连接'
            elif isinstance(packet.get('data'), dict):
                self.handle_event(room, kind, packet['data'])

        client.add_event_listener('ALL', notice)
        delay = 5
        while not self.stopping:
            self.status[room] = '连接中'
            try:
                await client.connect()
            except Exception as error:
                logger.warning('[Jupiter] 房间 %s 连接失败 (%s)', room, type(error).__name__)
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
            while not self.stopping:
                self.status['Bridge'] = '连接中'
                try:
                    async with session.ws_connect(url, protocols=protocols, heartbeat=30) as ws:
                        self.status['Bridge'] = '已连接'
                        delay = 5
                        async for packet in ws:
                            if packet.type == aiohttp.WSMsgType.TEXT:
                                await self.read_bridge(ws, packet.data)
                except (aiohttp.ClientError, ValueError, TimeoutError) as error:
                    logger.warning('[Jupiter] Bridge 连接失败 (%s)', type(error).__name__)
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
            try:
                self.outbox.put_nowait((target, message))
            except asyncio.QueueFull:
                logger.warning('[Jupiter] 推送积压，跳过房间 %s 的一条通知', room)

    async def deliver(self):
        while True:
            target, message = await self.outbox.get()
            if target == self.config.get('target_umo', '').strip():
                try:
                    delivered = await self.context.send_message(target, MessageChain().message(message))
                    if not delivered:
                        logger.warning('[Jupiter] AstrBot 找不到绑定会话的适配器')
                except Exception as error:
                    logger.warning('[Jupiter] QQ 通知发送失败 (%s)', type(error).__name__)
                await asyncio.sleep(1)

    @filter.command('jupiter_bind')
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bind(self, event: AstrMessageEvent):
        self.config['target_umo'] = event.unified_msg_origin
        self.config.save_config()
        yield event.plain_result('Jupiter 已绑定当前会话。发送 /jupiter_test 验证主动推送。')

    @filter.command('jupiter_test')
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def test(self, event: AstrMessageEvent):
        target = self.config.get('target_umo', '').strip()
        if not target:
            yield event.plain_result('请在目标会话先执行 /jupiter_bind。')
            return
        try:
            sent = await self.context.send_message(target, MessageChain().message('【Jupiter】QQ 主动推送测试'))
        except Exception as error:
            yield event.plain_result(f'主动推送失败（{type(error).__name__}），请检查 AstrBot 的 QQ 适配器。')
            return
        yield event.plain_result('已调用主动发送，请确认目标会话实际收到。' if sent else '未找到绑定会话的适配器。')

    @filter.command('jupiter_status')
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_status(self, event: AstrMessageEvent):
        lines = ['Jupiter 已绑定会话' if self.config.get('target_umo') else 'Jupiter 尚未绑定会话']
        if self.errors:
            lines.append(self.errors)
        if 'Bridge' in self.status:
            lines.append(f'Bridge：{self.status["Bridge"]}')
        for room in self.rooms:
            lines.append(f'房间 {room}：{self.status.get(room, "等待事件源")}，本次加载收到弹幕 {self.received.get(room, 0)} 条')
        if not self.rooms and not self.errors:
            lines.append('请在 WebUI 配置直播间，然后重载插件。')
        yield event.plain_result('\n'.join(lines))
