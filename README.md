# 哈基米音乐点歌（astrbot_plugin_hachimitsu_music）

在群聊里发送 `/哈基米`，随机发送一首哈基米音乐。

数据来自 [hajihami.com](https://hajihami.com) 的「B站播放量总榜」（一个 Notion 公开页面），

## 指令

| 指令 | 行为 | 权限 |
|---|---|---|
| `/哈基米` | 随机一首（总榜 50% + 五个风格池各 10%） | 所有人 |
| `/哈基米 <关键词>` | 模糊搜索，最多 5 条，回复序号点播 | 所有人 |
| `/哈基米 帮助` | 用法说明 | 所有人 |
| `/哈基米 状态` | 运行状态 | 管理员 |
| `/哈基米状态` | 同上（独立入口，便于在「指令管理」页单独启停） | 管理员 |

发出的消息形态：

```
《曼波の小曲》《登山の小曲》La La La（孤高曼波）
作者：手柄推荐2026
https://www.bilibili.com/video/BV1FMefeGEKU
[语音消息 —— 整首]
```

## 安装

1. 把本仓库放到 AstrBot 的 `data/plugins/` 下（目录名建议 `astrbot_plugin_hachimitsu_music`）
2. 装依赖：`pip install -r requirements.txt`（`httpx`、`qrcode[pil]`）
3. 确保系统里有 **ffmpeg**（不在 PATH 时插件会自动探几个常见安装位置，找不到就回退原始格式）
4. 重载插件

首次启动会从 Notion 全量拉取榜单（约 10 秒），之后按配置的周期刷新。

## 配置

配置项都能在 AstrBot 的插件配置页里可视化编辑，分四组：

### 基础

| 配置              | 默认         | 说明                                            |
| --------------- | ---------- | --------------------------------------------- |
| `enabled`       | `true`     | 是否响应点歌                                        |
| `audio_quality` | `192k`     | 从 B站 下载哪条源音轨：`192k` / `132k` / `64k` / `flac` |
| `vocal_preset`  | `standard` | 发给 QQ 的编码档位                                   |

**发送档位**（实测同一首 133 秒的作品）：

| 档位 | 参数 | 体积 | base64 载荷 |
|---|---|---|---|
| 高 `high` | 44100Hz / 128k | 2.12 MB | 2.83 MB |
| **标准 `standard`（默认）** | 24000Hz / 64k | **1.06 MB** | 1.41 MB |
| 省流 `lite` | 16000Hz / 32k | 0.53 MB | 0.71 MB |

### 随机与榜单

| 配置 | 默认 | 说明 |
|---|---|---|
| `top_n` | `1000` | 总榜随机池范围（前 N 名） |
| `top_weight` | `50` | 总榜权重 |
| `style_weight` | `10` | 每个风格池权重（五个合计 50） |
| `rank_refresh_hours` | `24` | 榜单刷新周期（小时） |

五个风格池：`曼波好听～` / `冰🧊！` / `哈基周金曲` / `原教旨主义` / `婉约派`。

### B站账号（可选）

| 配置                               | 默认     | 说明                                      |
| -------------------------------- | ------ | --------------------------------------- |
| `cookie`                         | 空      | `SESSDATA=...; bili_jct=...` 形式的 Cookie |
| `enable_admin_assist`            | `true` | Cookie 失效时是否私聊管理员请求续期                   |
| `admin_ids`                      | 空      | 管理员 QQ 号（接收协助请求 + 可用「状态」指令）             |
| `admin_request_cooldown_minutes` | `30`   | 两次协助请求的最短间隔                             |

不填 Cookie 也能正常用，只是音质上限 192K。

Cookie 失效时，插件会私聊管理员请求确认；管理员回复「确定」后插件本地渲染登录二维码
（登录 token 不交给任何第三方），扫码成功自动落盘到 `data/plugin_data/`。
**管理员不协助也会自动回退匿名模式继续跑**，功能不中断。

### 限流与队列

| 配置 | 默认 |
|---|---|
| `user_cooldown_seconds` | 30 |
| `group_cooldown_seconds` | 10 |
| `user_daily_limit` | 20 |
| `group_daily_limit` | 100 |
| `global_per_minute` | 10 |
| `max_concurrency` | 2 |
| `queue_max` | 20 |
| `task_timeout_seconds` | 30 |
| `dedup_window_seconds` | 300 |
| `cache_max_mb` | 2048 |

所有拦截都会**明确回复原因和剩余等待秒数**，绝不静默丢弃。

## 控制台

插件自带一个 WebUI 页面（`pages/console`），在 WebUI 的插件详情页里打开，提供：

- **运行状态**：榜单条数/更新时间、各风格池规模、Cookie 与会员状态、实际音质、熔断、队列、缓存占用
- **常用配置**：音质档位、随机权重、限流参数
- **配额管理**：可勾选的群/用户配额列表（带筛选框）+ 手动输入兜底 + 一键重置
  （**只清计数，不清熔断** —— 熔断反映的是上游风控信号）
- **操作**：手动刷新榜单、清空音频缓存、发起 B站 扫码登录

## 工作原理

```
榜单层 RankStore   Notion 私有 API → rank.json（全量，默认 24h 刷新）+ 内存风格索引
      ↓
解析层 BiliResolver BV →（view：cid + UP主）→（playurl + WBI 签名）→ 音轨择优
      ↓
下载层 AudioFetcher 异步下载 + Range 续传 + LRU 缓存（命中零请求）
      ↓
编码层 Encoder      ffmpeg（线程池）→ mono mp3
      ↓
投递层 Delivery     base64 直发 record 段 → 失败逐级降级
      ↓
闸门层 Guard        准入 / 限流 / 队列 / 去重
```

## 免责声明

- 音频来源为 B站，仅供个人学习与交流使用，**请勿用于任何商业用途**。
- 数据来源站点 hajihami.com 明确声明**不得商用**；本插件仅提供检索与播放能力，
  不存储、不二次分发内容。
- 请遵守 B站 的用户协议与 robots 规则，合理设置限流参数，不要高频请求。
- 使用本插件产生的一切后果由使用者自行承担。

## 灵感来源

本插件在实现过程中参考了以下项目的设计与经验（均已在代码注释中标注出处）：

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) —— 插件框架与官方开发文档
- [wbndmqaq/astrbot_plugin_qqmusic](https://github.com/wbndmqaq/astrbot_plugin_qqmusic) 
- [drdon1234/astrbot_plugin_media_parser](https://github.com/drdon1234/astrbot_plugin_media_parser) 
- [the1812/Bilibili-Evolved](https://github.com/the1812/Bilibili-Evolved) 

## License

MIT
