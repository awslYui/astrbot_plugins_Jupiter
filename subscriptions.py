import asyncio
import hashlib
from html.parser import HTMLParser
import json
from urllib.parse import urlsplit

import aiohttp
from astrbot.api import logger
from astrbot.api.star import StarTools
from bilibili_api import Credential, live, user
import feedparser

from .lifecycle import consume_task


PLUGIN_NAME = 'astrbot_plugin_jupiter'
MAX_FEED_BYTES = 2 * 1024 * 1024
CHECK_TIMEOUT = 45.0


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def clean_text(value):
    parser = PlainText()
    parser.feed(value if isinstance(value, str) else '')
    parser.close()
    return ' '.join(' '.join(parser.parts).split())


def web_url(value):
    if isinstance(value, str):
        try:
            url = urlsplit(value.strip())
        except ValueError:
            return ''
        if url.scheme in {'http', 'https'} and url.netloc:
            return value.strip()
    return ''


def sources_from_config(config):
    sources = []
    for field, kind, label in (('bili_uids', 'dynamic', 'B 站 UID'), ('bili_rooms', 'live', '直播间')):
        values = dict.fromkeys(str(value).strip() for value in config[field])
        for value in values:
            if not (value.isascii() and value.isdecimal() and int(value) > 0):
                raise ValueError(f'{label} 只能填写正整数')
            sources.append((kind, int(value), f'{label} {int(value)}'))
    for index, url in enumerate(dict.fromkeys(config['feed_urls']), start=1):
        if not web_url(url):
            raise ValueError(f'订阅源 {index} 需要填写合法的 HTTP(S) 地址')
        sources.append(('feed', url.strip(), f'订阅源 {index}'))
    return sources


def dynamic_entries(data):
    if 'items' not in data or not isinstance(data['items'], (list, type(None))):
        raise ValueError('B 站动态返回了未识别的结构')
    entries = {}
    for item in data['items'] or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get('id_str', ''))
        if not item_id.isascii() or not item_id.isdecimal():
            continue
        modules = item.get('modules') or {}
        author = (modules.get('module_author') or {}).get('name') or 'UP 主'
        dynamic = modules.get('module_dynamic') or {}
        major = dynamic.get('major') or {}
        body = (dynamic.get('desc') or {}).get('text') or (major.get('archive') or {}).get('title')
        if not body:
            opus = major.get('opus') or {}
            body = opus.get('title') or (opus.get('summary') or {}).get('text')
        entries[item_id] = f'【Jupiter · B 站动态】{author}\n{str(body or "发布了新动态")[:450]}\nhttps://t.bilibili.com/{item_id}'
    return [(key, entries[key]) for key in sorted(entries, key=int)[-30:]]


def feed_entries(body, source_url):
    if body.lstrip().startswith(b'{') or body.startswith(b'\xef\xbb\xbf{'):
        feed = json.loads(body)
        items = feed.get('items')
        if not isinstance(items, list):
            raise ValueError('JSON 订阅源缺少 items 列表')
        title = feed.get('title')
    else:
        feed = feedparser.parse(body, response_headers={'content-location': source_url})
        if not feed.version:
            raise ValueError('无法识别 RSS 或 Atom 格式')
        items, title = feed.entries, feed.feed.get('title')
    entries = {}
    for item in reversed(items[:30]):
        if not isinstance(item, dict):
            continue
        link = web_url(item.get('link') or item.get('url') or item.get('external_url'))
        identity = item.get('id') or item.get('guid') or link or item.get('title')
        if not identity:
            continue
        key = hashlib.sha256(str(identity).encode()).hexdigest()
        headline = clean_text(item.get('title')) or '有新条目'
        summary = clean_text(item.get('summary') or item.get('description') or item.get('content_text'))
        name = clean_text(title) or '订阅源'
        text = f'【Jupiter · {name[:80]}】{headline[:160]}'
        if summary:
            text += '\n' + summary[:350]
        if link:
            text += '\n' + link[:500]
        entries[key] = text
    return list(entries.items())


async def read_feed(session, url):
    async with session.get(url) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > MAX_FEED_BYTES:
                raise ValueError('订阅源响应超过 2 MiB')
        return await asyncio.to_thread(feed_entries, bytes(body), str(response.url))


class SubscriptionMonitor:
    def __init__(self, config, send_message, send_live):
        self.send_message = send_message
        self.send_live = send_live
        self.config = config
        self.status = {}
        self.sources = []
        self.problem = ''
        self.task = None
        self.check_task = None
        self.stopping = False
        self.session = None

    async def initialize(self):
        if self.stopping or self.task is not None:
            return
        try:
            self.sources = sources_from_config(self.config)
            if not self.sources:
                return
            self.state_path = StarTools.get_data_dir(PLUGIN_NAME) / 'subscription_state.json'
            self.state = json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else {}
            if not isinstance(self.state, dict):
                raise ValueError('订阅状态文件格式异常')
        except (ValueError, OSError) as error:
            self.problem = f'配置或订阅状态读取失败（{type(error).__name__}）'
            logger.warning('[Jupiter · 订阅] %s', self.problem)
            return
        self.task = asyncio.create_task(self.poll())

    def stop(self):
        self.stopping = True
        tasks = {self.task, self.check_task} - {None, asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if self.session is not None:
            task = asyncio.create_task(self.session.close())
            task.add_done_callback(consume_task)
            tasks.add(task)
        return tasks

    def ensure_running(self):
        if self.stopping or asyncio.current_task().cancelling():
            raise asyncio.CancelledError

    def save_state(self, key, value):
        self.ensure_running()
        updated = {**self.state, key: value}
        temp = self.state_path.with_suffix('.tmp')
        temp.write_text(json.dumps(updated, ensure_ascii=False), encoding='utf-8')
        temp.replace(self.state_path)
        self.state = updated

    async def send(self, target, text):
        self.ensure_running()
        if target != self.config.get('target_umo', '').strip():
            raise RuntimeError('推送目标已改变')
        sent = await self.send_message(target, text)
        if not sent:
            raise RuntimeError('AstrBot 找不到绑定会话的适配器')

    async def check(self, session, credential, target, kind, value):
        self.ensure_running()
        key = hashlib.sha256(json.dumps([target, kind, value]).encode()).hexdigest()
        previous = self.state.get(key)
        if kind == 'live':
            info = await live.LiveRoom(value, credential=credential).get_room_play_info()
            status = int(info['live_status'])
            if status not in {0, 1, 2}:
                raise ValueError('直播间状态无效')
            current = status == 1
            if previous is not None and previous != current and (current or self.config.get('notify_live_end')):
                self.ensure_running()
                await self.send_live(target, int(info.get('room_id') or value), current)
            if previous != current:
                self.save_state(key, current)
            return

        if kind == 'dynamic':
            entries = dynamic_entries(await user.User(value, credential=credential).get_dynamics_new())
            if previous is None:
                self.save_state(key, entries[-1][0] if entries else '0')
                return
            for entry_id, text in entries:
                if int(entry_id) > int(previous):
                    await self.send(target, text)
                    self.save_state(key, entry_id)
                    previous = entry_id
            return

        entries = await read_feed(session, value)
        if previous is None:
            self.save_state(key, [entry_id for entry_id, _ in entries])
            return
        seen = set(previous)
        for entry_id, text in entries:
            if entry_id not in seen:
                await self.send(target, text)
                previous = (previous + [entry_id])[-1000:]
                self.save_state(key, previous)
                seen.add(entry_id)

    async def poll(self):
        credential = Credential(
            sessdata=self.config.get('bili_sessdata') or None,
            buvid3=self.config.get('bili_buvid3') or None,
        )
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            self.session = session
            while not self.stopping:
                target = self.config.get('target_umo', '').strip()
                if not target:
                    await asyncio.sleep(3)
                    continue
                for kind, value, label in self.sources:
                    while self.check_task is not None and not self.check_task.done():
                        await asyncio.sleep(1)
                    if self.stopping:
                        return
                    self.check_task = asyncio.create_task(self.check(session, credential, target, kind, value))
                    self.check_task.add_done_callback(consume_task)
                    try:
                        done, _ = await asyncio.wait({self.check_task}, timeout=CHECK_TIMEOUT)
                        if not done:
                            self.check_task.cancel()
                            raise TimeoutError
                        await self.check_task
                    except asyncio.CancelledError:
                        self.check_task.cancel()
                        raise
                    except Exception as error:
                        code = getattr(error, 'code', None) or getattr(error, 'status', None)
                        detail = f'{type(error).__name__} {code}' if isinstance(code, int) else type(error).__name__
                        state = f'检查或推送失败（{detail}）'
                        if self.status.get(label) != state:
                            logger.warning('[Jupiter · 订阅] %s %s', label, state)
                        self.status[label] = state
                    else:
                        self.status[label] = '已检查'
                    await asyncio.sleep(1)
                await asyncio.sleep(max(60, int(self.config['interval_seconds'])))

