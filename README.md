# 木桶面板状态日报（astrbot_plugin_mutong_status）

> **⚠️ AI 生成项目声明**  
> 本项目为 AI 生成项目：代码、文档与示例主要由 AI 生成/辅助完成，可能存在错误、遗漏、兼容性或安全风险。请在使用前自行审查、测试并自行承担风险。  
> **作者 Bilibili 主页**：<https://space.bilibili.com/3546855692568823>

一个 AstrBot 插件：每天在指定时间，向指定 QQ 群推送木桶面板（Pterodactyl / 翼龙内核）上 Java 版 Minecraft 服务器的状态日报。

支持 `aiocqhttp`（NapCat / Lagrange 等）和 QQ 官方机器人（`qq_official` / `qq_official_webhook`）的群主动消息。

## 效果示例

```text
🎮 木桶面板 · 服务器日报
🕐 2026-10-01 21:00
🏷️ 服务器：我的生存服
📊 状态：🟢 运行中
👥 在线玩家：3 / 20
🖥️ CPU：25.5% / 200%（0.26 / 2.00 核）
🧠 内存：1.00 GiB / 4.00 GiB（25.0%）
💾 硬盘：2.00 GiB / 10.00 GiB（20.0%）
🌐 网络：↓ 3.91 KiB/s  ↑ 1.95 KiB/s
📶 延迟：38 ms（优秀）
⏱️ 运行时长：1 天 1 小时
🧩 版本：Paper 1.20.6
📝 MOTD：欢迎来到我的服务器
```

## 数据是从哪来的

| 数据 | 来源 |
| --- | --- |
| 在线人数 / 最大人数 | 直连 MC 服务端做 Server List Ping（SLP） |
| 延迟（网络质量） | SLP 请求往返耗时，单位为毫秒 |
| 上下行速率 | 面板 `/resources` 接口的累计流量，间隔两次采样计算 |
| CPU / 内存 / 硬盘 / 运行时长 | 面板 Pterodactyl Client API |

在线人数拿不到时（例如服务端关闭了 `enable-status`），可以启用「控制台兜底」：通过面板 WebSocket 执行 `list` 命令解析人数。

## 安装

把 `astrbot_plugin_mutong_status` 整个文件夹放进 AstrBot 的插件目录。也可以在 AstrBot 管理面板的插件页面上传本文件夹的 zip 包，然后重载插件。



本插件依赖 `aiohttp`（AstrBot 自带）和可选的 `websockets`（AstrBot 自带），一般无需额外安装。

## 配置步骤

### 1. 创建面板 API 密钥

1. 浏览器登录木桶面板：<https://vat-panel.mutong1.com/>
2. 右上角头像 → **Account Settings（账户设置）** → **API Credentials（API 密钥）**
3. 点击 **Create API Key（创建 API 密钥）**，描述随便填，Allowed IPs 留空表示不限制
4. 复制生成的密钥（形如 `ptlc_xxxxxxxx`，只显示一次）

> 这是 **Client API** 密钥（`ptlc_` 开头），不是 Application API 密钥（`ptla_` 开头），不要弄混。

### 2. 获取服务器标识符

在面板里进入你的服务器，浏览器地址栏形如：

```text
https://vat-panel.mutong1.com/server/Nka7aRk1
```

其中 `Nka7aRk1` 就是「服务器标识符」，填到插件配置的 `server_id`。

### 3. 填写插件配置

在 AstrBot 插件配置页填写：

| 配置项 | 说明 |
| --- | --- |
| `enabled` | 是否启用每日定时推送 |
| `push_time` | 推送时间，24 小时制 `HH:MM`，例如 `21:00` |
| `timezone` | 时区，默认 `Asia/Shanghai` |
| `target_groups` | 目标群：填群号（使用 `default_platform`），或完整 UMO，如 `aiocqhttp:GroupMessage:123456`。QQ 官方机器人需要填 `group_openid`，推荐用命令绑定 |
| `default_platform` | 纯群号时使用的平台 ID：QQ 机器人一般是 `aiocqhttp`，QQ 官方机器人是 `qq_official` |
| `panel_base_url` | 面板地址，默认 `https://vat-panel.mutong1.com` |
| `api_key` | 上一步创建的 `ptlc_` 密钥 |
| `server_id` | 服务器标识符，如 `Nka7aRk1` |
| `server_address` | 可选的 `域名或IP:端口`，留空自动使用面板默认分配地址 |
| `network_sample_interval` | 网络测速采样间隔，默认 2 秒 |
| `request_timeout` | 请求超时，默认 10 秒 |
| `verify_ssl` | 面板证书校验，自签名证书才需要关闭 |
| `console_fallback` | SLP 失败时用面板控制台 `list` 取人数 |
| `mention_all` | 推送时是否 @全体成员 |
| `report_title` | 日报标题 |
| `include_disk` / `include_uptime` / `include_motd` | 是否显示硬盘 / 运行时长 / 版本和 MOTD |

## 命令

| 命令 | 说明 |
| --- | --- |
| `/mc状态`（别名 `/mcstat`） | 立即查询并在当前会话显示状态 |
| `/mc推送 绑定` | 把当前群加入每日推送（需要 AstrBot 管理员） |
| `/mc推送 解绑` | 取消当前群的推送 |
| `/mc推送 列表` | 查看所有推送目标 |
| `/mc推送 测试` | 立即向所有目标群推送一次 |

推荐做法：先填好面板地址、API 密钥、服务器标识符，然后到目标群里发一次 `/mc推送 绑定`，就不用关心 UMO 格式了。

## QQ 官方机器人（主动消息）

QQ 官方机器人现在允许在群内开启机器人主动发言，配置步骤：

1. 在 QQ 开放平台为机器人开通「主动消息」相关权限，并确认机器人已发布 / 可用；
2. 在目标 QQ 群里，由群主或管理员在群设置 / 机器人管理里允许该机器人主动发言；
3. 让机器人进群后，在群里发送一次 `/mc推送 绑定`（需要 AstrBot 管理员账号执行）；
4. 用 `/mc推送 测试` 验证一次主动消息能否发出。

几点说明：

- QQ 官方机器人的群标识是 `group_openid`（一串字母数字），**不是群号**，所以 `target_groups` 里不要填群号，直接用绑定命令最省事；
- AstrBot 的 QQ 官方适配器只有在收到过该群消息后才会登记会话场景；插件在每次推送前会自动登记一次，因此 AstrBot 重启后即使群里没人说话，定时推送也能正常发出；
- QQ 官方接口不支持 @全体成员，`mention_all` 只对 `aiocqhttp` 类平台生效；
- 主动消息有额度 / 频率限制，本插件每天固定时间只发一条，一般不会超限。

## 常见问题

**CPU 的百分比怎么理解？**

Pterodactyl 中 `100% = 1 个 CPU 核心`。如果面板给服务器限制的是 200%，那么跑满就是 2 个核心，插件会同时显示百分比和核数。

**在线人数查不到怎么办？**

1. 检查 `server_address` 是否填写正确（可以从面板的「网络」页面复制分配地址，如 `1.2.3.4:25565`）；
2. 检查服务端 `server.properties` 中 `enable-status=true`；
3. 打开 `console_fallback`，让插件改用面板控制台执行 `list` 命令；
4. 服务器离线时人数显示 0 属于正常现象。

插件不做 SRV 记录解析：如果你使用的是域名，且该域名靠 SRV 记录指向真实端口，请在 `server_address` 里直接填写真实的 `host:port`。

**提示“面板 API 密钥无效或已过期（HTTP 401）”？**

重新创建一个 Client API 密钥并更新配置。密钥只在创建时显示一次，页面刷新后无法再查看。

**提示“面板上找不到该服务器（HTTP 404）”？**

`server_id` 填错了。注意不是服务器名称，而是面板 URL 里 `/server/` 后面的那一段。

**定时推送没有发出？**

1. 确认 `enabled` 为开、`push_time` 格式正确；
2. 确认已经通过 `target_groups` 配置或 `/mc推送 绑定` 添加了目标群；
3. QQ 官方机器人：确认已在群内允许机器人主动发言，并按上面的步骤完成绑定；
4. 查看 AstrBot 日志中 `[木桶面板]` 和 `[QQOfficial]` 开头的记录。

## 自测

不填真实面板也能验证采集逻辑（本地 mock 面板 + mock MC 服务端）：

```powershell
$env:PYTHONPATH = "D:\QQBOT\astrbot\backend\app"
& "D:\QQBOT\astrbot\backend\python\python.exe" tests\selftest.py
```

## 说明

- 本项目为 AI 生成项目，作者 Bilibili 主页：<https://space.bilibili.com/3546855692568823>。
- API 密钥等配置存放在 AstrBot 的插件配置目录中，请注意不要把配置文件或日志分享给别人。
- 本插件只读取面板资源数据和 MC 服务端状态，不会修改服务器、不会执行管理命令（控制台兜底只发送一条 `list` 查询命令）。