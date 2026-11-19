import json

from astrbot.api.star import StarTools


def import_legacy_eop(config):
    if config.get('legacy_eop_imported'):
        return ''
    data_dir = StarTools.get_data_dir('astrbot_plugin_jupiter')
    config_dir = data_dir.parent.parent / 'config'
    candidates = [
        path for name in ('astrbot_plugin_eop_config.json', 'astrbot_plugins_EoP_config.json')
        if (path := config_dir / name).is_file()
    ]
    if not candidates:
        return ''
    if len(candidates) > 1:
        raise ValueError('发现多份旧 EoP 配置，请在 WebUI 手动填写订阅设置')
    legacy = json.loads(candidates[0].read_text(encoding='utf-8'))
    if not isinstance(legacy, dict):
        raise ValueError('旧 EoP 配置应为 JSON 对象')

    updated = dict(config)
    for field in ('bili_uids', 'bili_rooms', 'feed_urls'):
        previous = legacy.get(field, [])
        current = config.get(field, [])
        if not isinstance(previous, list) or not isinstance(current, list):
            raise ValueError('旧 EoP 订阅配置应为列表')
        if any(not isinstance(value, (str, int)) for value in current + previous):
            raise ValueError('旧 EoP 订阅配置包含无效条目')
        updated[field] = list(dict.fromkeys(str(value).strip() for value in current + previous))
    for field in ('target_umo', 'bili_sessdata', 'bili_buvid3'):
        if not config.get(field) and isinstance(legacy.get(field), str):
            updated[field] = legacy[field]
    for field, default in (('interval_seconds', 120), ('notify_live_end', False)):
        if config.get(field, default) == default and field in legacy:
            updated[field] = legacy[field]

    state_path = data_dir / 'subscription_state.json'
    legacy_state = data_dir.parent / 'astrbot_plugin_eop' / 'subscription_state.json'
    if not state_path.exists() and legacy_state.is_file():
        state = json.loads(legacy_state.read_text(encoding='utf-8'))
        if not isinstance(state, dict):
            raise ValueError('旧 EoP 订阅状态应为 JSON 对象')
        temp = state_path.with_suffix('.migration.tmp')
        temp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        temp.replace(state_path)

    updated['legacy_eop_imported'] = True
    config.update(updated)
    config.save_config()
    return '已导入旧 EoP 订阅设置；推送目标使用当前 Jupiter 绑定'
