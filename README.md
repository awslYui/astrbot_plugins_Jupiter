# Jupiter 直播间通知 · AstrBot 插件

## v0.1.1 重载修复

关闭流程最多等待后台任务 3 秒。连接中、已连接和连接失败时均会清理本实例的弹幕回调、心跳任务与 WebSocket。停止后的实例拒绝新的弹幕通知和重连，重复初始化或关闭也可安全处理。

若 v0.1.0 已卡在卸载阶段，先停止 AstrBot 进程或容器，在停止状态下将本仓库最新版源码替换到对应插件目录，再启动 AstrBot。旧实例正在执行的关闭代码需要通过结束旧进程退出。保留 `data/config/` 中的插件配置。

监听 B 站直播间，按账号 UID 转发主播和额外关注用户的普通弹幕。插件默认直连 B 站，QQ 通知通过当前 AstrBot 的主动消息接口发送。已有 LAPLACE Event Bridge 时，可以在配置中选用该事件源。[Jupiter 原项目](https://github.com/laplace-live/jupiter) · [AstrBot 主动消息](https://docs.astrbot.app/dev/star/guides/send-message.html)

## 安装与使用

1. 在 AstrBot WebUI 打开 **插件 → 安装插件（+）→ 从链接安装**，填入 [本仓库地址](https://github.com/awslYui/astrbot_plugins_Jupiter)。也可从文件安装 `astrbot_plugin_jupiter.zip`。需要 AstrBot 4.18.0 或更高版本，以及支持主动消息的 QQ 适配器，例如 OneBot v11。若 WebUI 提示缺依赖，查看插件日志，在 AstrBot 的运行环境中按 `requirements.txt` 安装。[安装插件](https://docs.astrbot.app/use/plugin.html) · [OneBot v11](https://docs.astrbot.app/platform/aiocqhttp.html)
2. 打开插件配置，在「直播间与主播 UID」里新增一项，格式为 `房间号:主播UID`，例如 `123456:987654321`。房间号取自直播间地址，UID 取自主播空间地址。这是两个不同字段。保存后重载插件。
3. 用 AstrBot 管理员账号在目标 QQ 群发 `/jupiter_bind`，再发 `/jupiter_test`。确认群里收到独立的「QQ 主动推送测试」消息。
4. 发 `/jupiter_status` 检查连接和收到的弹幕条数。在房间未开播时，等主播真实发言，或用自己的测试直播间和主播账号发一条测试弹幕。

每个插件实例绑定一个 AstrBot 会话。重新执行绑定命令会切换目标。也可以直接在 WebUI 修改或清空 `target_umo`，保存后重载。

## 可选配置

| 配置项 | 用途 |
| --- | --- |
| `extra_uids` | 额外关注的 B 站 UID，适用于已配置的所有直播间 |
| `notify_stream` / `notify_superchat` | 分别开启开播下播、醒目留言通知，默认关闭 |
| `gift_min_yuan` | 转发达到门槛的付费礼物，单位为元，默认 `0` 关闭 |
| `bridge_url` / `bridge_token` | 使用已有 Event Bridge。此时房间号填写正式房间号；启用鉴权的实例填 Token |
| `bili_sessdata` / `bili_dedeuserid` / `bili_buvid3` | 直连 B 站时可选。若匿名连接被拒绝或收不到用于匹配的 UID，用自己的登录 Cookies。`bili_dedeuserid` 填提供 Cookies 的账号 UID |

Cookies 配在 WebUI，不要发到群里。AstrBot 会遮罩显示敏感字段，配置文件里仍保存实际值。[AstrBot 配置说明](https://docs.astrbot.app/dev/star/guides/plugin-config.html) · [B 站连接库](https://github.com/Nemo2011/bilibili-api) · [Event Bridge](https://laplace.live/chat/event-bridge)

## 版聊验证与功能范围

- 未开播时，只要事件源送来包含主播 UID 的普通弹幕，通知就会触发。实际是否收到，取决于 B 站、使用的账号、房间和云服务器网络。
- 已连接但计数一直为 0 时，先查事件源是否收到弹幕。计数上涨但主播消息没到 QQ 时，核对主播 UID、登录状态及 `/jupiter_test` 的结果。
- 这是按 AstrBot 场景实现的版本。原 Jupiter 的下播战报、多 Bridge 分流、大航海和进房通知未移植。实时事件逐条推送，断线期间的弹幕没有历史补发。
