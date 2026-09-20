<div align="center">

# 哈基米音乐点歌插件

<p align="center">
  <a href="https://github.com/YoinSama/astrbot_plugin_hachimitsu_music"><img src="https://img.shields.io/badge/当前版本-v1.2.2-blue.svg?style=for-the-badge&color=76bad9" alt="当前版本 v1.2.2" /></a>&nbsp;<img src="https://img.shields.io/badge/AstrBot-%3E%3D4.27.2-orange.svg?style=for-the-badge" alt="AstrBot >= 4.27.2" />&nbsp;<a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg?style=for-the-badge" alt="MIT License" /></a>
</p>

<img src="https://picture.yoinsama.com/file/1789514987665_logo_MAX.png" alt="哈基米音乐点歌" />

**一个基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的群聊点歌AI生成插件：从 [hajihami.com](https://hajihami.com) 的 B站播放量总榜里点歌，音频直接以语音消息发到群里。支持 OneBotv11（[SnowLuma](https://github.com/SnowLuma/SnowLuma)/ [NapCat（未测试）](https://github.com/NapNeko/NapCatQQ)）。**

</div>

<!-- [![AstrBot](https://img.shields.io/badge/AstrBot-插件市场入口-ff69b4?style=for-the-badge)](https://cloud.astrbot.app/plugin/YoinSama/astrbot_plugin_hachimitsu_music) -->

## 功能特色

###  点歌

- **随机来一首**：`/哈基米` 从「总榜 + 五个风格池」加权抽样（默认总榜 50% / 每风格池 10%）
- **关键词点播**：`/哈基米 <关键词>` 模糊搜索前 5 首，回复序号即点播（60 秒等待，可回复「取消」）
- **语音直发**：下载的音频转成紧凑 mp3，以语音消息发送，QQ 内点击即播

###  稳定与限流

- **四层闸门**：准入（群/用户黑名单）→ 限流（冷却 + 日配额 + 全局速率）→ 队列（并发 + 排队）→ 同曲去重，拦截都会说明原因和剩余等待秒数
- **B站风控熔断**：触发 `-352 / -412 / -799` 时自动暂停 120 秒再恢复
- **三级音质降级 + 兜底**：拿不到目标音轨就自动降档，不会让点歌失败
- **LRU 音频缓存**：热门歌曲直接复用缓存，不用重复下载

###  WebUI 控制台

- **运行状态**：榜单条数、风格池规模、Cookie 状态、熔断/队列/今日计数都能看到
- **可视化配置**：音质、发送档位、随机权重、限流参数改完即时生效
- **配额管理**：查看与一键重置群 / 用户配额
- **榜单刷新**：手动从 Notion 全量拉取
- **缓存清理**：一键清空音频缓存释放磁盘
- **B站登录**：控制台内生成二维码扫码，或向管理员发起协助登录续期

## 使用方法

### 指令

| 指令 | 行为 | 权限 |
| --- | --- | --- |
| `/哈基米` | 随机一首（总榜 50% + 五个风格池各 10%） | 所有人 |
| `/哈基米 <关键词>` | 模糊搜索，最多 5 条，回复序号点播 | 所有人 |
| `/哈基米 帮助` | 用法说明 | 所有人 |
| `/哈基米 状态` | 运行状态 | 管理员 |
| `/哈基米状态` | 同上（独立入口，便于在「指令管理」页单独启停） | 管理员 |

#### 发出的消息形态：

```
《曼波の小曲》《登山の小曲》La La La（孤高曼波）
作者：手柄推荐2026
https://www.bilibili.com/video/BV1FMefeGEKU
[语音消息 —— 整首]
```

## 安装

1. **从文件安装**：从本仓库下载项目 Zip 压缩包，在 AstrBot 的「插件」页点击「安装插件」→「从文件安装」，选择下载的 Zip 包，点击安装。
2. **从链接安装**：复制 `https://github.com/YoinSama/astrbot_plugin_hachimitsu_music.git`，在「插件」页点击「安装插件」→「从链接安装」，粘贴仓库链接，点击安装。
3. **重载插件** 使其生效。

**首次启动会从 Notion 全量拉取榜单（约 10 秒），之后按配置的周期刷新。**

## 配置选项

> [!NOTE]
> 以下配置情况仅供参考，请以插件配置面板中各字段的说明为准。

### 基础

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否响应点歌 |
| `audio_quality` | `192k` | 从 B站 下载哪条源音轨：`192k` / `132k` / `64k` / `flac`（flac 需大会员，否则自动降级 192K） |
| `vocal_preset` | `standard` | 发给 QQ 的编码档位：`high`(44.1k/128k) / `standard`(24k/64k) / `lite`(16k/32k) |

**发送档位**（实测同一首 133 秒的作品）：

| 档位 | 参数 | 体积 | base64 载荷 |
| --- | --- | --- | --- |
| 高 `high` | 44100Hz / 128k | 2.12 MB | 2.83 MB |
| **标准 `standard`（默认）** | 24000Hz / 64k | **1.06 MB** | 1.41 MB |
| 省流 `lite` | 16000Hz / 32k | 0.53 MB | 0.71 MB |

### 随机与榜单

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `top_n` | `1000` | 总榜随机池范围（前 N 名） |
| `top_weight` | `50` | 总榜权重 |
| `style_weight` | `10` | 每个风格池权重（五个合计 50） |
| `rank_refresh_hours` | `24` | 榜单刷新周期（小时） |

目前预设的五个风格池：`曼波好听～` / `冰🧊！` / `哈基周金曲` / `原教旨主义` / `婉约派`。

### B站账号（可选）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `cookie` | 空 | `SESSDATA=...; bili_jct=...` 形式的 Cookie |
| `enable_admin_assist` | `true` | Cookie 失效时是否私聊管理员请求续期 |
| `admin_ids` | 空 | 管理员 QQ 号（接收协助请求 + 可用「状态」指令） |
| `admin_request_cooldown_minutes` | `30` | 两次协助请求的最短间隔 |

不填 Cookie 也能正常用，只是音质上限 192K。

Cookie 失效时，插件会私聊管理员请求确认；管理员回复「确定」后插件本地渲染登录二维码（登录 token 不交给任何第三方），扫码成功自动落盘到 `data/plugin_data/`。管理员不协助也会自动回退匿名模式继续跑，功能不中断。

### 限流与队列

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `user_cooldown_seconds` | 30 | 同一用户两次点歌的最短间隔（秒），**填 -1 不限制** |
| `group_cooldown_seconds` | 10 | 同一群两次点歌的最短间隔（秒），**填 -1 不限制** |
| `user_daily_limit` | 20 | 单个用户每天最多点歌数量（首），每天 0 点重置，**填 -1 不限制** |
| `group_daily_limit` | 100 | 单个群每天最多点歌数量（首），每天 0 点重置，**填 -1 不限制** |
| `global_per_minute` | 10 | 所有群合计每分钟最多处理数量（首/分钟），**填 -1 不限制** |
| `max_concurrency` | 2 | 同时处理的点歌任务数上限（个），建议 2~3，**填 -1 不限制** |
| `queue_max` | 20 | 等待队列最大长度（个），超出后提示稍后再试，**填 -1 不限制** |
| `task_timeout_seconds` | 30 | 单个点歌任务从开始到发出的最长耗时（秒），超时中断 |
| `dedup_window_seconds` | 300 | 同一首歌在此时间内再次点播直接复用缓存（秒），不重复请求也不占配额 |
| `cache_max_mb` | 2048 | 音频文件 LRU 缓存体积上限（MB），超出按最久未使用淘汰 |

> ⚠️ 上表里标了「填 -1 不限制」的 7 项：**关掉限制请填 -1，不要填 0。**
> 填 0（或填了非数字）会被当成无效值，自动改成 1 —— 比如 `max_concurrency=0`
> 会让插件直接卡死且没有任何报错。被自动纠正时，启动日志和网页控制台都会明确提示。

所有拦截都会明确回复原因和剩余等待秒数。

### 作品长度

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `min_seconds` | 0 | 短于这个长度的作品不参与点歌（秒），填 0 或 -1 表示不限制最短 |
| `max_seconds` | 600 | 长于这个长度的作品不参与点歌（秒），填 -1 不限制最长 |
| `resample_max` | 3 | 随机抽到超限作品时最多自动换几首，用尽则放行最后一首 |

- 随机点歌抽到太长的会**自动换一首**，用户全程无感；搜索时超时的作品直接不显示
- 已失效的稿件（约 1.7%）一并跳过
- 判定用的是 `view` 接口白送的 `duration` 字段，**不额外发任何请求**，也不用预热
- 闸门在扣配额之前，所以被跳过的歌不占用户/群的日配额
- ⚠️ `max_seconds` 是唯一不回退 1 的限制项：填 0 会按默认 **600** 处理

### 接入控制

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `group_allowlist` | 空 | 群白名单，非空时仅列表内群可用 |
| `group_blocklist` | 空 | 群黑名单，列表内群禁用（白名单非空时以白名单为准） |
| `user_blocklist` | 空 | 用户黑名单，列表内 QQ 号禁用 |

## WebUI 控制台

插件自带一个 WebUI 页面，在 AstrBot 插件详情页里打开，无需额外部署。提供：

- **运行状态**：榜单条数/更新时间、各风格池规模、Cookie 与会员状态、实际音质、熔断、队列、缓存占用
- **常用配置**：音质档位、随机权重、限流参数即时保存
- **配额管理**：可勾选的群 / 用户配额列表（带筛选框）+ 手动输入兜底 + 一键重置（只清计数，不清熔断 —— 熔断反映的是上游风控信号）
- **操作**：手动刷新榜单、清空音频缓存、发起 B站 扫码登录、向管理员私聊发起登录请求（**管理员需要先私聊一次 bot，让插件缓存会话 id**）

## 平台支持与要求

| 平台 | 适配器 | 协议框架 |
| --- | --- | --- |
| OneBot v11 |  aiocqhttp | 建议 SnowLuma / NapCat（未测试）|

## ToDo计划

### 未完成需求
- 更多的预设风格池

## 注意事项

> [!WARNING]
> 1. **来源说明**：榜单数据来自 hajihami 站点，作品数据源来自 bilibili，本项目仅供个人学习与娱乐，不得商用。
> 2. **平台限制**：目前只支持了OneBotv11的消息平台，使用SnowLuma进行了测试（NapCat应该也能使用）。
> 3. **ffmpeg 可选**：没装 ffmpeg 时会自动回退原始 m4a 直发（体积略大，音质一样），不影响使用（**目前没有测试过无 ffmpeg 环境安装插件的情况**）。
> 4. **风控**：B站 可能对高频请求做风控，触发后插件自动熔断 120 秒再恢复（一般不会触发）。

## 常见问题 (FAQ)

### 点了没反应 / 提示「风控熔断」

**现象**：发送点歌后机器人回复「B站 侧暂时限制访问，已暂停点歌 N 秒」或直接无响应。

**原因**：触发了 B站 风控（`-352 / -412 / -799`）或处于熔断冷却期。

**处理**：等待约 120 秒自动恢复；若频繁触发，可降低 `global_per_minute` 或调大 `user_cooldown_seconds`。

### 配了大会员 Cookie 还是只有 192K

**原因**：Cookie 未正确填写、已过期，或该账号并非有效大会员。

**处理**：在 WebUI 控制台用「扫码登录」重新登录；或确认 `bili.cookie` 含有效 `SESSDATA` 且账号 `vipStatus == 1`会员生效。未检测到大会员时插件会自动降级到 192K，不会报错。（其实这几档音质只有细微的差别，个人感觉一些哈基米音乐带点失真才是 true music）

### 没有安装 ffmpeg 能跑吗？

**能？**。插件在找不到 ffmpeg 时回退原始 m4a 直发（协议端自带 ffmpeg 转 silk），音质一致，仅体积略大。建议安装 ffmpeg 以获得更可控的转码档位（**没有测试过无ffmpeg环境，全是大肥鱼老师的文案**）。

### 想调整风格池比例？

修改配置 `random.top_weight`（总榜权重）与 `random.style_weight`（每风格池权重）即可，保存后即时生效。

## 免责声明

- 音频来源为 B站，仅供个人学习与交流使用，**请勿用于任何商业用途**。
- 数据来源站点 hajihami.com 明确声明**不得商用**；本插件仅提供检索与播放能力。
- 请遵守 B站 的用户协议，合理设置限流参数，不要高频请求。
- 使用本插件产生的一切后果由使用者自行承担。

## 灵感来源

本插件在实现过程中参考了以下项目的设计与经验（均已在代码注释中标注出处）：

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) —— 插件框架与官方开发文档
- [zhiyu-astrbot-hjm](https://github.com/oxoax/zhiyu-astrbot-hjm) —— 一款随机哈基米语音的AstrBot插件
- [wbndmqaq/astrbot_plugin_qqmusic](https://github.com/wbndmqaq/astrbot_plugin_qqmusic) —— 从 [Yunzai-Bot qqmusic-plugin](https://github.com/zaras123/qqmusic-plugin) 移植而来的 AstrBot 版本
- [drdon1234/astrbot_plugin_media_parser](https://github.com/drdon1234/astrbot_plugin_media_parser) —— 流媒体聚合解析器
- [the1812/Bilibili-Evolved](https://github.com/the1812/Bilibili-Evolved) —— 强大的哔哩哔哩增强脚本

## 特别鸣谢

- 感谢 `pinkillqaq` 大佬制作的哈基米音乐整合网站 [hajihami.com](https://hajihami.com)，联系方式：asaxinw@gmail.com
- 感谢创作😺哈基米音乐😻的全民制作人
- 感谢 Astrbot 的开源社区
- 感谢 Deepseek-V4.1-Flash 对本项目的大力支持

## 许可证

[MIT License](https://mit-license.org/) 根据 MIT 开源协议，你可以自由使用、修改、分发代码，但需保留上述版权声明。

**项目初期可能存在很多bug，如果遇到问题欢迎提交 Issue 和 Pull Request 来改进这个插件！**
