import asyncio
import hashlib
import json
import math
from pathlib import Path

from astrbot.api.web import request
from yarl import URL

from .lifecycle import consume_task
from .runtime import room_config
from .subscriptions import sources_from_config


VERSION = '0.3.0'
PLUGIN_NAME = 'astrbot_plugin_jupiter'
SECRET_FIELDS = ('bridge_token', 'bili_sessdata', 'bili_buvid3')
SCHEMA = json.loads((Path(__file__).parent / '_conf_schema.json').read_text(encoding='utf-8'))
EDITABLE_FIELDS = set(SCHEMA) - {'legacy_eop_imported'}


def revision(config):
    encoded = json.dumps(dict(config), ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def validated_config(config, settings):
    if not isinstance(settings, dict) or set(settings) - EDITABLE_FIELDS:
        raise ValueError('设置包含无法识别的字段')
    updated = dict(config)
    for key, value in settings.items():
        field = SCHEMA[key]
        if key in SECRET_FIELDS and value is None:
            value = ''
        if field['type'] == 'list':
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f'{field["description"]}需要填写文本列表')
            value = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        elif field['type'] == 'bool':
            if not isinstance(value, bool):
                raise ValueError(f'{field["description"]}需要填写开关值')
        elif field['type'] in {'int', 'float'}:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f'{field["description"]}需要填写有效数字')
            if field['type'] == 'int' and not isinstance(value, int):
                raise ValueError(f'{field["description"]}需要填写整数')
        elif not isinstance(value, str):
            raise ValueError(f'{field["description"]}需要填写文本')
        if isinstance(value, str):
            value = value.strip()
        updated[key] = value
    if updated['interval_seconds'] < 60:
        raise ValueError('订阅轮询间隔至少为 60 秒')
    if updated['gift_min_yuan'] < 0:
        raise ValueError('礼物通知金额应大于或等于 0')
    room_config(updated['rooms'], updated['extra_uids'])
    for value in updated['extra_uids']:
        if not value.isascii() or not value.isdecimal() or not 0 < len(value) <= 20 or not int(value):
            raise ValueError('额外关注 UID 需要填写正整数')
    sources_from_config(updated)
    bridge = URL(updated['bridge_url'])
    if bridge and (bridge.scheme not in {'ws', 'wss'} or not bridge.host):
        raise ValueError('Event Bridge 地址应以 ws:// 或 wss:// 开头')
    return updated


class PluginWebUI:
    def __init__(self, plugin):
        self.plugin = plugin

    def register(self):
        for path, handler, methods in (
            ('state', self.state, ['GET']),
            ('config', self.save, ['POST']),
            ('test', self.test, ['POST']),
        ):
            self.plugin.context.register_web_api(f'/{PLUGIN_NAME}/{path}', handler, methods, 'Jupiter 管理面板')

    def unregister(self):
        routes = self.plugin.context.registered_web_apis
        routes[:] = [entry for entry in routes if getattr(entry[1], '__self__', None) is not self]

    def snapshot(self):
        config = self.plugin.config
        runtime = self.plugin.runtime
        settings = {key: config.get(key, field['default']) for key, field in SCHEMA.items() if key in EDITABLE_FIELDS and key not in SECRET_FIELDS}
        rooms = []
        for line in settings['rooms']:
            room, _, uid = line.partition(':')
            room_id = int(room) if room.isdecimal() and len(room) <= 20 else 0
            rooms.append({
                'room_id': room, 'uid': uid,
                'status': runtime.status.get(room_id, runtime.status.get('Bridge', '等待连接')),
                'messages': runtime.received.get(room_id, 0),
            })
        sources = [{'name': label, 'status': runtime.subscriptions.status.get(label, '等待首次检查')} for _, _, label in runtime.subscriptions.sources]
        warnings = [value for value in (runtime.errors, runtime.subscriptions.problem, runtime.migration_status) if value]
        return {
            'version': VERSION, 'revision': revision(config), 'settings': settings,
            'secrets_configured': {key: bool(config.get(key)) for key in SECRET_FIELDS},
            'running': runtime.initialized and not runtime.stopping and not self.plugin.closing,
            'rooms': rooms, 'sources': sources, 'warnings': warnings,
            'pending_messages': runtime.outbox.qsize(),
        }

    async def state(self):
        return {'status': 'ok', 'data': self.snapshot()}

    async def save(self):
        body = await request.json()
        if not isinstance(body, dict):
            return {'status': 'error', 'message': '请求需要包含 JSON 设置'}
        try:
            async with self.plugin.settings_lock:
                if self.plugin.closing:
                    raise ValueError('插件正在停止，请稍后重新打开页面')
                if body.get('revision') != revision(self.plugin.config):
                    raise ValueError('设置已在其他会话中改变，请重新载入后再保存')
                candidate = validated_config(self.plugin.config, body.get('settings'))
                await self.plugin.apply_settings(candidate)
        except ValueError as error:
            return {'status': 'error', 'message': str(error)}
        except Exception:
            return {'status': 'error', 'message': '设置应用失败，请检查 AstrBot 日志并刷新状态'}
        return {'status': 'ok', 'data': self.snapshot()}

    async def test(self):
        runtime = self.plugin.runtime
        target = self.plugin.config.get('target_umo', '').strip()
        if not target:
            return {'status': 'error', 'message': '请先保存推送会话，或在目标群发送 /jupiter_bind'}
        if self.plugin.closing or runtime.stopping:
            return {'status': 'error', 'message': '插件正在停止，请稍后重试'}
        task = asyncio.create_task(runtime.send_notification(target, '【Jupiter】管理面板推送测试'))
        runtime.tasks.add(task)
        task.add_done_callback(runtime.tasks.discard)
        task.add_done_callback(consume_task)
        done, _ = await asyncio.wait({task}, timeout=10)
        if not done:
            task.cancel()
            return {'status': 'error', 'message': '平台发送超时，请检查 QQ 协议端状态'}
        if task.cancelled() or task.exception() is not None or not task.result():
            return {'status': 'error', 'message': '发送失败，请检查推送会话和 QQ 适配器'}
        return {'status': 'ok', 'data': {'message': '已提交平台，请在目标 QQ 会话确认收到消息'}}
