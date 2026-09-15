"""作品时长缓存与闸门（v1.2.0 §2）。

**数据从哪来：点播流程内顺手拿的，零额外请求。**

``_process`` 第一步 ``get_view(bv)`` 本来就必须走（要拿 ``cid``），而 ``view`` 响应里
就带着 ``duration``。所以不需要为"知道时长"多发任何请求，也不需要全量预热
（那是 1847 首 / 18 分钟，方案 v3 §2.7 已否决）。

``d = -1`` 表示稿件失效（实测约 1.7%，``62002 稿件不可见`` / ``62012``），
这类作品会被一并排除 —— 顺手省下必然失败的点歌。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .constants import LOG_PREFIX
from .utils import plugin_data_dir

TTL_SECONDS = 30 * 24 * 3600  # 与现有 view 缓存一致
DEAD = -1  # 稿件失效标记
SAVE_EVERY = 20  # 每积累这么多条新记录落一次盘（避免每首歌都写一遍文件）


class DurationStore:
    """``bv → 秒`` 的持久化缓存。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (plugin_data_dir() / "duration.json")
        self._items: dict[str, dict] = {}
        self._dirty = 0

    # ------------------------------------------------------------- 读写

    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        items = payload.get("items")
        if isinstance(items, dict):
            self._items = items

    def save(self) -> None:
        payload = {"version": 1, "items": self._items}
        self._dirty = 0
        tmp = self._path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except OSError as exc:
            # 写不进去不影响点歌，下次启动重建即可
            from .utils import logger

            logger.debug("%s 时长缓存写入失败（%s），本次只保留内存副本", LOG_PREFIX, exc)

    # ------------------------------------------------------------- 查询

    def get(self, bv: str) -> int | None:
        """返回秒数；``None`` 表示未知；``DEAD``(-1) 表示稿件失效。"""
        if not bv:
            return None
        entry = self._items.get(bv)
        if not isinstance(entry, dict):
            return None
        if time.time() - float(entry.get("t") or 0) > TTL_SECONDS:
            self._items.pop(bv, None)
            return None
        return int(entry.get("d") or 0)

    def put(self, bv: str, seconds: int) -> None:
        if not bv:
            return
        self._items[bv] = {"d": int(seconds), "t": time.time()}
        self._dirty += 1
        if self._dirty >= SAVE_EVERY:
            self.save()

    def flush(self) -> None:
        """有改动就落盘（流程结束/搜索结束时调一次，保证不丢）。"""
        if self._dirty:
            self.save()

    def is_dead(self, bv: str) -> bool:
        return self.get(bv) == DEAD

    def mark_dead(self, bv: str) -> None:
        self.put(bv, DEAD)

    # ------------------------------------------------------------- 统计

    def stats(self) -> dict:
        known = dead = 0
        now = time.time()
        for entry in self._items.values():
            if not isinstance(entry, dict):
                continue
            if now - float(entry.get("t") or 0) > TTL_SECONDS:
                continue
            known += 1
            if int(entry.get("d") or 0) == DEAD:
                dead += 1
        return {"known": known, "dead": dead}

    def clear(self) -> None:
        self._items = {}
        self.save()


class DurationRejected(Exception):
    """作品被时长规则拦下。

    在**下载之前、``guard.commit()`` 之前**抛出 —— 所以被换掉的歌既没有产生流量，
    也不占用户的每日配额。
    """

    def __init__(self, bv: str, seconds: int, reason: str = "") -> None:
        super().__init__(f"{bv} 时长 {seconds}s 被拦下：{reason or '超出允许范围'}")
        self.bv = bv
        self.seconds = seconds
        self.reason = reason


class DurationGate:
    """时长区间判定。"""

    def __init__(self, min_seconds: int = 0, max_seconds: int = 600) -> None:
        # -1（哨兵）= 不限；min 的 0 同样表示"不限最短"
        self.min_seconds = -1 if min_seconds < 0 else int(min_seconds)
        self.max_seconds = -1 if max_seconds < 0 else int(max_seconds)

    @property
    def enabled(self) -> bool:
        """闸门是否真的会拦下作品（两端都不限时不拦）。"""
        return (self.min_seconds > 0) or (self.max_seconds > 0)

    def allows(self, seconds: int | None) -> bool:
        """是否放行。

        - ``None``（时长未知）→ **放行**，交给流程内的闸门兜底
        - ``DEAD``（稿件失效）→ 不放行
        """
        if seconds is None:
            return True
        if seconds < 0:
            return False
        if self.min_seconds > 0 and seconds < self.min_seconds:
            return False
        # 「10 分钟及以上踢出」→ 边界用 >=：600 秒本身算超限
        if self.max_seconds > 0 and seconds >= self.max_seconds:
            return False
        return True

    def describe(self) -> str:
        if not self.enabled:
            return "不限制"
        low = "不限" if self.min_seconds <= 0 else f"{self.min_seconds}s"
        high = "不限" if self.max_seconds < 0 else f"{self.max_seconds}s"
        return f"{low} ~ {high}"
