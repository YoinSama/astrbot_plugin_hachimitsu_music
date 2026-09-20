"""全局常量。

跨模块共享的字面量集中在这里，避免同一份数据在多处硬编码后走样。
特别注意 STYLE_POOLS：里面的全角字符必须与 Notion 字段值逐字节一致，
所以它只放在代码里、不开放成配置项（手输全角字符极易出错且会静默失效）。
"""

# ---------------------------------------------------------------- 插件元信息

PLUGIN_NAME = "astrbot_plugin_hachimitsu_music"
PLUGIN_VERSION = "v1.2.2"
REPO_URL = "https://github.com/YoinSama/astrbot_plugin_hachimitsu_music"
ISSUE_URL = f"{REPO_URL}/issues/new"
LOG_PREFIX = "[哈基米音乐]"

# ---------------------------------------------------------------- Notion 榜单

NOTION_API = "https://www.notion.so/api/v3"
NOTION_PAGE_ID = "22ad0099-fb2c-80a2-98e5-d4a107d49327"
NOTION_SPACE_ID = "d449fa7f-9b0a-4674-8dc3-9fda326a6561"
NOTION_COLLECTION_ID = "229d0099-fb2c-80a4-bd0d-000b3e51a5c5"
# 「B站播放量总榜」视图（table，按播放量降序）
NOTION_VIEW_ID = "23ed0099-fb2c-80ae-8f11-000cb703c6b1"

# 单次 syncRecordValues 的批量大小。实测 3000 条/请求稳定；
# 早期用 100 条连发会在约 132 次后触发连接重置（Errno 10053）。
NOTION_SYNC_BATCH = 3000

# 字段名 → 语义。Notion 内部属性名含特殊符号，注意反引号与方括号。
FIELD_TITLE = "title"  # 作品名称
FIELD_URL = "^kTl"  # 视频链接（完整 URL，可能带追踪参数）
FIELD_PLAY = "f}<W"  # 播放量（数字）
FIELD_CREATOR = "c^GY"  # 全民制作人
FIELD_STYLE = "[qOF"  # 风格（multi_select）

# ---------------------------------------------------------------- 风格池

# 五个风格池。⚠️ 全角字符必须与 Notion 字段值一致：
# 「曼波好听～」用的是 U+FF5E，「冰🧊！」用的是 U+FF01。
STYLE_POOLS = ["曼波好听～", "冰🧊！", "哈基周金曲", "原教旨主义", "婉约派"]

# 总榜的来源标识（加权随机时与 STYLE_POOLS 并列）
TOP_SOURCE = "__top__"

# 启动自检基线：这些标签的数量级应当接近下表右列。
# 若「本该有数据」的标签出现 0，说明 multi_select 解析出了问题（漏了 split(",")）。
STYLE_BASELINE = {
    "现代主义": 842,
    "原教旨主义": 606,
    "翻唱": 461,
    "AI小马": 422,
    "曼波好听～": 343,
    "冰🧊！": 234,
    "婉约派": 82,
    "哈基周金曲": 25,
}

# ---------------------------------------------------------------- 音轨与编码

# B站 DASH 音频轨的 quality id → 档位名。
# 匿名最高只能拿到 30280（192K）；30250/30251 需大会员。
AUDIO_TRACK_QUALITY = {
    30280: "192k",
    30232: "132k",
    30216: "64k",
    30250: "dolby",
    30251: "flac",
}

# 下载音质的优先级（从高到低），用于降级时挑「下一档」
AUDIO_TRACK_ORDER = {
    "flac": ["flac", "dolby", "192k", "132k", "64k"],
    "192k": ["192k", "132k", "64k"],
    "132k": ["132k", "64k"],
    "64k": ["64k"],
}

# 发送给 QQ 的语音编码档位。
# standard 的 24kHz 正好是腾讯 silk 的原生采样率，几乎不用重采样，兼容性最好。
VOCAL_PRESETS = {
    "high": {"sample_rate": 44100, "bitrate": "128k"},
    "standard": {"sample_rate": 24000, "bitrate": "64k"},
    "lite": {"sample_rate": 16000, "bitrate": "32k"},
}
DEFAULT_VOCAL_PRESET = "standard"

# ---------------------------------------------------------------- 其他

BV_PATTERN = r"BV[0-9A-Za-z]{10}"
VIDEO_URL_TEMPLATE = "https://www.bilibili.com/video/{bv}"

# 搜索最多展示的结果数
SEARCH_LIMIT = 5
# 搜索时多取几条候选：超时的会被剔掉，用备选顶上，保证仍能凑满 SEARCH_LIMIT 条。
# v1.2.0 —— get_views 是并发的，多取 3 条几乎不增加耗时（单次 view 实测 0.08s）。
SEARCH_CANDIDATE_EXTRA = 3

# ---------------------------------------------------------------- 作品时长限制（v1.2.0）

# 最长时长默认值（秒）。max_seconds 填 0 / 无效值时回退到它 ——
# 这是唯一不回退 1 的限制项：回退 1 会让几乎所有作品都被踢掉，等于插件不可用。
DURATION_MAX_DEFAULT = 600
# 抽到超限作品时最多换几首（用尽则放行最后一首，绝不阻塞点歌）
DURATION_RESAMPLE_DEFAULT = 3
# view / playurl 返回这些 code = 稿件没法播，记成失效后一并排除，省下必然失败的点歌：
#   62002 视频被隐藏 / 62004 审核中 / 62012 仅UP主本人可见（实测约 1.7%）
#   -404 稿件不存在 / -688 地区限制 / -689 版权限制
DEAD_VIEW_CODES = {62002, 62004, 62012, -404, -688, -689}
