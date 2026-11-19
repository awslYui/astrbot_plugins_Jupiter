import asyncio

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .runtime import MonitorRuntime
from .webui import PluginWebUI


class Jupiter(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.runtime = MonitorRuntime(context, config)
        self.webui = PluginWebUI(self)
        self.settings_lock = asyncio.Lock()
        self.closing = False
        self.initialized = False

    async def initialize(self):
        if self.closing or self.initialized:
            return
        self.initialized = True
        await self.runtime.initialize()
        self.webui.register()

    async def terminate(self):
        self.closing = True
        self.webui.unregister()
        async with self.settings_lock:
            await self.runtime.terminate()

    async def apply_settings(self, settings):
        previous = dict(self.config)
        self.config.update(settings)
        try:
            self.config.save_config()
        except Exception:
            self.config.clear()
            self.config.update(previous)
            raise
        await self.runtime.terminate()
        if not self.closing:
            self.runtime = MonitorRuntime(self.context, self.config)
            await self.runtime.initialize()

    @filter.command('jupiter_bind')
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bind(self, event: AstrMessageEvent):
        async with self.settings_lock:
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
            sent = await self.runtime.send_notification(target, '【Jupiter】弹幕与动态订阅统一推送测试')
        except Exception as error:
            yield event.plain_result(f'主动推送失败（{type(error).__name__}），请检查 AstrBot 的 QQ 适配器。')
            return
        yield event.plain_result('已调用主动发送，请确认目标会话实际收到。' if sent else '未找到绑定会话的适配器。')

    @filter.command('jupiter_status')
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_status(self, event: AstrMessageEvent):
        lines = ['Jupiter 已绑定会话' if self.config.get('target_umo') else 'Jupiter 尚未绑定会话']
        if self.runtime.errors:
            lines.append(self.runtime.errors)
        if self.runtime.migration_status:
            lines.append(self.runtime.migration_status)
        if 'Bridge' in self.runtime.status:
            lines.append(f'Bridge：{self.runtime.status["Bridge"]}')
        for room in self.runtime.rooms:
            lines.append(f'房间 {room}：{self.runtime.status.get(room, "等待事件源")}，本次加载收到弹幕 {self.runtime.received.get(room, 0)} 条')
        if self.runtime.subscriptions.problem:
            lines.append(self.runtime.subscriptions.problem)
        for _, _, label in self.runtime.subscriptions.sources:
            lines.append(f'{label}：{self.runtime.subscriptions.status.get(label, "等待首次检查")}')
        if not self.runtime.rooms and not self.runtime.subscriptions.sources and not self.runtime.errors:
            lines.append('请在管理面板添加直播间或动态订阅，点击“保存并应用”。')
        yield event.plain_result('\n'.join(lines))
