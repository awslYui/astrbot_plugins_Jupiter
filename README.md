# Jupiter 直播与动态通知

v0.4.0 将监听房间与关注用户分开，支持自定义直播间通知前缀和独立的直播状态轮询间隔。它监听 B 站直播间弹幕，并轮询 B 站动态、开播状态与 RSS、Atom、JSON 订阅源，统一向绑定的 AstrBot 会话推送通知。功能设计参考 [LAPLACE Jupiter](https://github.com/laplace-live/jupiter) 和 [LAPLACE EoP](https://laplace.live/eye-of-providence)。

## 安装与升级

1. 在 AstrBot WebUI 的 **插件 → 安装插件（+）→ 从链接安装** 中填入本仓库地址，或更新已安装的 Jupiter。需要 AstrBot 4.26.0+，QQ 侧使用支持主动消息的适配器，例如 OneBot v11。[仓库地址](https://github.com/awslYui/astrbot_plugins_Jupiter) · [AstrBot 插件安装](https://docs.astrbot.app/use/plugin.html)
2. 如果此前安装了独立 EoP，先在 WebUI 停用它，再更新 Jupiter。合并版第一次启动会查找旧 EoP 的配置，导入动态 UID、房间号、订阅地址和去重记录。Jupiter 已填写的群绑定与 Cookies 优先使用。导入情况可用 `/jupiter_status` 查看。
3. 打开下方的管理面板，填写需要的房间或订阅来源，点击“保存并应用”。用 AstrBot 管理员账号在目标 QQ 群发 `/jupiter_bind`，再发 `/jupiter_test`，确认实际收到测试消息。若使用 AstrBot 通用插件配置表单，保存后需重载插件。

若旧版本已经卡在卸载阶段，先停止 AstrBot，在停止状态下替换插件文件，再启动。旧配置通常在 `data/config/`，订阅记录在 `data/plugin_data/`。[AstrBot OneBot v11](https://docs.astrbot.app/platform/aiocqhttp.html)

若安装后容器反复重启，也应先停止容器，在挂载的 `data/plugins/astrbot_plugin_jupiter/` 内替换为当前版本的文件，再启动。保留 `data/config/` 和 `data/plugin_data/`。无法直接替换时，将插件目录移到 `data/plugins/` 之外，启动 AstrBot 后再安装当前版本。

## v0.4.0 关注用户与通知格式

在 **监听与订阅 → 监听直播间** 只添加房间号，在下方 **关注用户 UID** 填写要追踪的人。例如关注嘉然时填写 `672328094`，她在任一已添加房间发弹幕都会通知，与该房间属于谁无关。插件不会自动发现她所在的全部直播间；需要自行添加监听房间。

每个房间的 **通知名称（可选）** 用于展示，不参与 UID 匹配。留空时在首次需要通知前查询房间主播名，并在当前实例中缓存；查询失败先显示房间号，5 分钟后有新通知时再尝试。手动填写名称可直接使用，无需查询。

在 **通知设置 → 直播间通知前缀** 编辑，默认 `【{主播名}直播间】`。支持 `{主播名}`、`{房间号}`，也可填写固定文字或留空。页面使用第一个监听房间提供预览，点击“保存并应用”生效。例如设置通知名称为 `嘉然` 时，默认弹幕消息为：

```text
【嘉然直播间】和纯子：测试
```

直播间通知不再追加直播间链接。动态与 RSS 通知继续保留原文链接。开播、下播、醒目留言和礼物通知使用相同的直播间前缀。

旧配置 `房间号:主播UID` 在升级时自动拆分：房间号进入监听房间，旧主播 UID 合并到全局关注列表。请在 WebUI 删除不再需要关注的 UID；更改房间的通知名称不会改变关注用户。管理面板和 `/jupiter_status` 分别显示收到的弹幕、匹配的弹幕和成功提交平台的通知数，计数从本次加载开始。

直播状态轮询独立运行，默认每轮完成后等待 30 秒，最低可设 5 秒；慢动态或 RSS 请求不再阻塞直播检查。每个房间的请求耗时和房间之间的 1 秒间隔会增加实际检测延迟，不能保证设为 5 秒就一定在 5 秒内通知。失败时等待逐步延长到最多 300 秒，成功后恢复配置间隔。动态与 RSS 仍最低 60 秒。实时开播、下播事件不受轮询间隔影响。

## 连接兼容修订（继承 v0.3.1）

实时弹幕继续使用同版本 B 站 SDK 的认证、协议、心跳和事件处理，仅将 WebSocket 固定为 Jupiter 独立持有的 aiohttp 客户端。SDK 默认会优先选择环境中可用的 `curl_cffi`；17.4.2 的 curl 适配器在线程中直接读取异步 WebSocket 的底层句柄，该用法与新版 curl_cffi 的后台接收不兼容。上游文档明确要求异步连接使用公开接收接口，避免底层句柄竞争。[curl_cffi WebSocket 文档](https://github.com/lexiforest/curl_cffi/blob/main/docs/websockets.rst)

此修订不更改插件功能、配置格式、自动监听行为或其他插件的 SDK 全局客户端选择。关闭和重连时只清理 Jupiter 自己的 WebSocket、会话及后台任务，继续保持总计 3 秒的关闭等待上限。

安装成功后无 Python 退出堆栈、直接再次启动的日志，只能证明进程曾中断，不能单独确定段错误、内存不足或外部重启。此版本排除了上述已确认的不安全接收路径；若仍有异常，需要结合 Docker 的退出码、OOM 状态及宿主机记录核实，不应仅凭知识库等非致命报错归因。

## 管理面板

更新并重载后，在 **AstrBot WebUI → 插件 → Jupiter → 插件详情 → 管理面板** 打开。页面沿用 AstrBot 的登录权限和明暗主题。

- **监听与订阅** 独立编辑监听房间、房间通知名称与关注用户 UID，查看收到、匹配、已发计数；也可编辑动态 UID、开播房间和 RSS 地址。
- **通知设置** 可编辑统一推送会话、直播间通知前缀、两类轮询间隔和通知开关，也可测试已保存的推送目标。
- **连接与凭证** 可配置现有 Bridge 和 B 站凭证。已保存的敏感字符串用配置状态展示，留空沿用，点击清除后保存可删除。

点击 **保存并应用** 会校验并保存配置，停止旧监听实例，再用新设置启动监听。全部后台任务继续使用 3 秒关闭等待上限。其他会话同时修改设置时，页面会提示重新载入，避免覆盖修改。

## 配置

| 配置项 | 填写方式 |
| --- | --- |
| `rooms` | 每项只填房间号，如 `22637261` |
| `extra_uids` | 关注用户的 B 站 UID，适用于所有监听房间，不自动关注房间主播 |
| `room_names` | 可选，每项填写 `房间号:通知名称`，如 `22637261:嘉然` |
| `danmaku_prefix` | 直播间通知前缀，默认 `【{主播名}直播间】`，支持 `{主播名}`、`{房间号}` |
| `bili_uids` | 每项一个 B 站账号 UID，订阅该账号动态 |
| `bili_rooms` | 每项一个直播间号，轮询开播状态 |
| `feed_urls` | 完整的 RSS、Atom 或 JSON 订阅地址，也可用已有 RSSHub 路由 |
| `interval_seconds` | 动态与 RSS 每轮结束后的等待秒数，默认 120，最低 60 |
| `live_interval_seconds` | 直播状态每轮结束后的等待秒数，默认 30，最低 5 |
| `notify_stream` | 从弹幕事件源推送开播、下播，默认关闭 |
| `notify_live_end` | 轮询房间时同时提醒下播，默认关闭 |
| `notify_superchat` | 推送醒目留言，默认关闭 |
| `gift_min_yuan` | 付费礼物通知最低金额，单位元，默认 `0` 关闭 |
| `target_umo` | `/jupiter_bind` 自动填写当前群或私聊会话 |
| `bridge_url` / `bridge_token` | 可选的现有 Event Bridge 地址与凭证。地址留空时直连 B 站 |
| `bili_sessdata` / `bili_buvid3` | 需要登录访问 B 站时，填入自己账号对应的 Cookie 字段值 |
| `bili_dedeuserid` | 提供登录 Cookies 的账号 UID，用于直连弹幕 |

例如，`rooms` 添加你要监听的多个房间，`extra_uids` 配置 `672328094`，即可接收嘉然在这些房间发出的弹幕；另在 `bili_uids` 添加她的 UID 可订阅账号动态。仅填写动态 UID 或订阅地址，也可单独运行订阅功能。

未开播房间的弹幕只要带有已配置的 UID 并由事件源送达，就会触发通知。`/jupiter_status` 显示连接状态和本次加载收到的弹幕数，实际版聊接收情况需要在目标房间验证。实时源和轮询同时报告同一房间的相同开播状态时，合并版会合并通知。短房间号会在实时源返回正式房间号后关联。[B 站连接库](https://github.com/Nemo2011/bilibili-api) · [Event Bridge](https://laplace.live/chat/event-bridge)

## 指令

| 指令 | 用途 |
| --- | --- |
| `/jupiter_bind` | 将全部通知绑定到当前群或私聊 |
| `/jupiter_test` | 验证向绑定会话主动发送消息 |
| `/jupiter_status` | 查看弹幕、订阅和配置导入状态 |

以上指令需要 AstrBot 管理员权限。每个插件实例绑定一个会话，重新绑定会切换全部通知的目标。

## 旧 EoP 数据导入

支持从 `data/config/astrbot_plugin_eop_config.json` 或 `data/config/astrbot_plugins_EoP_config.json` 导入，首次成功后记住导入结果。若同时存在两份配置，状态指令会提示手动填写订阅设置。订阅去重记录从 `data/plugin_data/astrbot_plugin_eop/subscription_state.json` 复制到 Jupiter 的数据目录。已有的 Jupiter 订阅状态优先使用。

旧 EoP 和 Jupiter 的推送群如果不同，合并版采用 Jupiter 当前绑定。新的目标会话会为各订阅来源建立首次检查基线。安装目录或配置文件采用其他名称时，可以在 WebUI 将旧 EoP 的订阅项目填入合并版。

## 运行与重载

首次检查新的订阅来源时，插件记录当前条目与直播状态，后续变化才推送。动态与订阅去重状态保存在 `data/plugin_data/astrbot_plugin_jupiter/subscription_state.json`。动态和 RSS 通知包含正文及原文链接。

关闭时同时取消弹幕、轮询和当前请求，并在总计 3 秒的等待上限内收尾。停止后的实例拒绝后续通知和状态写入。断线期间的弹幕可能遗漏，订阅来源当前已移除的旧条目也可能无法补取。

依赖从 `requirements.txt` 安装到 AstrBot 实际运行的 Python 环境。Cookies 可在 WebUI 配置，敏感字段会遮罩显示，实际值存储在 AstrBot 配置文件中。[AstrBot 插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html)

安装依赖后，在仓库根目录执行 `python -m unittest discover -s tests` 可验证功能合并、配置导入和重载清理。测试使用本地网络服务和 AstrBot 接口替身，QQ 实际投递通过 `/jupiter_test` 验证。
