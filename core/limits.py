"""限制类配置的取值规则（v1.2.0）。

四条规则（方案 v3 §1.1）：

    -1                    → 该限制不生效（唯一哨兵）
    正整数                 → 该值
    0 / 负数(非 -1) / 非数字 → 回退 ``MIN_LIMIT``(1)，并给出纠正说明
    None（配置里没这个键）   → 回退该配置项的默认值

**为什么兜底是 1 而不是 0：**

- ``max_concurrency = 0`` → ``Semaphore(0)`` **永久死锁**，插件静默卡死、日志里
  什么都没有，是最难查的一类故障。
- ``*_daily_limit = 0`` / ``global_per_minute = 0`` → 所有人 / 全群点不了歌。

``1`` 是"最小可用值"：既保证功能还能跑，又不会静默失效。

本模块是所有限制取值的**唯一入口** —— 禁止在各处自己写 ``max(1, ...)``，
那种写法会把 ``-1`` 一起夹成 1（最严格，恰好与"关闭"相反）。
"""

from __future__ import annotations

UNLIMITED = -1  # 哨兵：该限制不生效
MIN_LIMIT = 1  # 无效输入的兜底：最小可用值

# 支持哨兵的配置项（方案 v3 §1.2，只有这 7 个）
SENTINEL_KEYS = (
    "user_cooldown_seconds",
    "group_cooldown_seconds",
    "user_daily_limit",
    "group_daily_limit",
    "global_per_minute",
    "max_concurrency",
    "queue_max",
)


def resolve_limit(raw, default: int) -> tuple[int, str | None]:
    """解析一个「限制类」配置值。

    Args:
        raw: 配置里的原始值。``None`` 表示配置里没有这个键（升级场景）。
        default: 该配置项的默认值，仅在 ``raw`` 为 ``None`` 时使用。

    Returns:
        ``(生效值, 纠正说明)``。纠正说明为 ``None`` 表示输入合法、无需提示；
        非空则说明输入无效已被纠正，调用方应当把它呈现给用户。
    """
    # 配置缺键（升级场景）走默认值，不是 MIN_LIMIT —— 否则老用户升级后限流会突然收紧
    if raw is None:
        return int(default), None

    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return MIN_LIMIT, f"「{raw}」不是数字，已按 {MIN_LIMIT} 处理"

    if value < 0:
        # 任何负数都归一成哨兵，避免 -2 / -99 这类值产生歧义
        return UNLIMITED, None
    if value == 0:
        return MIN_LIMIT, f"填 0 无效，已按 {MIN_LIMIT} 处理（不想限制请填 -1）"
    return value, None


def resolve_bound(raw, default: int) -> tuple[int, str | None]:
    """解析「区间上/下限」类配置（``duration.min_seconds`` / ``max_seconds``）。

    与 :func:`resolve_limit` 的唯一区别是**无效值回退 ``default`` 而不是 1**：
    ``max_seconds`` 回退成 1 会让几乎所有作品都被踢掉，等于把插件搞成不可用，
    比回退成 1 严重得多（方案 v3 §2.6 已定案）。

    取值：``负数 → UNLIMITED`` / ``0 或非数字 → default`` + 纠正说明 / 正整数 → 自身。

    **回退值等于输入值时不报警**（真机发现的假报警）：``min_seconds`` 的默认值
    就是 ``0``（= 不限最短），填 0 时回退成 0 —— 结果和输入一模一样，
    却弹一条「填 0 无效」的纠正说明，默认配置下控制台会永久挂一条黄条。
    所以这里只在**确实改了用户的值**时才给提示。
    """
    if raw is None:
        return int(default), None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return int(default), f"「{raw}」不是数字，已按默认 {default} 秒处理"
    if value < 0:
        return UNLIMITED, None
    if value == 0:
        fallback = int(default)
        if fallback == 0:
            # 回退值与输入相同 —— 没有实质改动，不打扰用户
            return 0, None
        return fallback, f"填 0 无效，已按默认 {fallback} 秒处理（不想限制请填 -1）"
    return value, None


def resolve_positive(raw, default: int) -> tuple[int, str | None]:
    """解析「正整数」类配置（如 ``duration.resample_max``），不支持哨兵。"""
    if raw is None:
        return int(default), None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return int(default), f"「{raw}」不是数字，已按默认 {default} 处理"
    if value < 1:
        return int(default), f"填 {value} 无效，已按默认 {default} 处理"
    return value, None


def is_unlimited(value: int) -> bool:
    """这个限制是否被哨兵关掉了。"""
    return value < 0


def describe(value: int) -> str:
    """给状态展示用的可读文本。"""
    return "不限制" if is_unlimited(value) else str(value)
