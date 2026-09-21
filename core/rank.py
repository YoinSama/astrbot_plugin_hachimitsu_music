"""RankStore —— 榜单的落盘、内存索引、随机取歌与关键词搜索。

存储选型（方案 §3.6 的五维实测对比结论）：**JSON + 内存预建索引**。
SQLite 体积更小、关键词搜索更快，但它是同步阻塞的 —— 每次取歌都得
``await asyncio.to_thread(...)``，会抢占 AstrBot 的默认线程池，而 ffmpeg 转码
也在用同一个池子（官方明文禁止阻塞事件循环）。随机取歌是每次点歌必走的
高频路径，JSON + 预建索引在这条路径上快约 47 倍，而它唯一的劣势
（搜索慢 15.6 倍）绝对值只有 0.7ms，相对 1~2 秒的点歌总耗时可以忽略。

将来若榜单涨到 10 万行以上、或需要复杂查询/全文检索，只改这一层即可。
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Iterable

from .constants import (
    DEFAULT_POOLS,
    LOG_PREFIX,
    POOL_MIN_WEIGHT,
    POOL_TOP_LABEL,
    POOL_WEIGHT_NDIGITS,
    STYLE_POOLS,
    VIDEO_URL_TEMPLATE,
)
from .duration import DurationGate, DurationStore
from .utils import logger, plugin_data_dir

# 落盘时保留的字段（raw_url 等中间字段不落盘，控制文件体积）
_KEEP_FIELDS = ("rank", "bv", "title", "creator", "play", "styles")

_CACHE_VERSION = 1


def video_url(bv: str) -> str:
    """把 BV 号还原成干净的视频链接。

    榜单里的 ``^kTl`` 字段存的是完整 URL，但实测前 3000 条里有 11 条带着
    追踪参数（``?vd_source=<分享者用户ID>``、``spm_id_from=...``、
    ``share_source=copy_web``）。所以统一提取 BV 后重建标准链接。
    """
    return VIDEO_URL_TEMPLATE.format(bv=bv) if bv else ""


def _round1(value: float) -> float:
    return round(float(value), POOL_WEIGHT_NDIGITS)


def _clamp(value: float, low: float, high: float) -> float:
    """夹到 [low, high]。上限低于下限时取下限（说明空间已经被占满了）。"""
    if high < low:
        return low
    return max(low, min(value, high))


def resolve_pool_weights(
    pools: "Iterable[str] | None", manual: dict | None = None
) -> dict[str, float]:
    """把「选中的池 + 手动指定的概率」算成一份完整的概率表（合计 100%）。

    规则（前端用同一套公式做即时预览，所见即所得）：

    - 在 ``manual`` 里的池按用户给的值**固定**，不参与剩余均分；
    - 其余池平摊剩下的概率；
    - 单池下限 ``POOL_MIN_WEIGHT``，总和不超 100% —— 超限的值自动压回当前
      可填的最大值（压回时要给后面的每个池留 1% 保底）；
    - 全部池都手动时，差额由列表里的最后一个池补齐，保证合计仍是 100%。
    """
    selected: list[str] = []
    for name in pools or []:
        if isinstance(name, str) and name and name not in selected:
            selected.append(name)
    if not selected:
        return {}

    manual = manual if isinstance(manual, dict) else {}
    fixed = [name for name in selected if name in manual]
    auto = [name for name in selected if name not in manual]
    floor = POOL_MIN_WEIGHT
    out: dict[str, float] = {}
    used = 0.0

    for index, name in enumerate(fixed):
        # 给后面的池留够保底：剩下的手动池 + 全部自动池，各 1%
        reserve = (len(fixed) - index - 1) * floor + len(auto) * floor
        ceiling = _round1(100.0 - used - reserve)
        try:
            value = _round1(float(manual[name]))
        except (TypeError, ValueError):
            value = floor
        out[name] = _clamp(value, floor, ceiling)
        used = _round1(used + out[name])

    if auto:
        each = _round1((100.0 - used) / len(auto))
        for name in auto:
            out[name] = max(floor, each)
        # 四舍五入会带来零点几的偏差，补回第一个自动池，让合计刚好是 100%
        drift = _round1(100.0 - sum(out.values()))
        if drift:
            out[auto[0]] = max(floor, _round1(out[auto[0]] + drift))
    elif fixed:
        last = fixed[-1]
        out[last] = max(floor, _round1(out[last] + (100.0 - sum(out.values()))))
    return out


class RankStore:
    """榜单缓存 + 内存索引。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (plugin_data_dir() / "rank.json")
        self._rows: list[dict] = []
        self._style_index: dict[str, list[int]] = {}
        self._updated_at: float = 0.0
        self._styled_count: int = 0
        self._duplicate_bv: int = 0

    # ------------------------------------------------------------- 只读属性

    @property
    def rows(self) -> list[dict]:
        return self._rows

    @property
    def size(self) -> int:
        return len(self._rows)

    @property
    def updated_at(self) -> float:
        return self._updated_at

    @property
    def styled_count(self) -> int:
        """带风格标签的行数（用于自检，实测约占 21%）。"""
        return self._styled_count

    @property
    def duplicate_bv(self) -> int:
        """重复 BV 的数量。

        实测 13218 行里有 79 个重复 BV。**故意不去重**：按需点播场景下
        重复只会让那首歌被抽中的概率略高，无害；去掉反而丢榜单原始信息。
        """
        return self._duplicate_bv

    # ------------------------------------------------------------- 索引

    def _build_index(self) -> None:
        """预建 ``{风格 → [行下标]}``。

        不建索引的话每次取歌都要遍历 13218 行做筛选（实测 0.21ms），
        建索引后是 0.0016ms（快 133 倍）。索引本身约 127KB、构建耗时 1ms，
        只在榜单刷新时做一次。
        """
        index: dict[str, list[int]] = {}
        seen_bv: set[str] = set()
        duplicate = 0
        styled = 0
        for position, row in enumerate(self._rows):
            styles = row.get("styles") or []
            if styles:
                styled += 1
            for style in styles:
                index.setdefault(style, []).append(position)
            bv = row.get("bv")
            if bv:
                if bv in seen_bv:
                    duplicate += 1
                else:
                    seen_bv.add(bv)
        self._style_index = index
        self._styled_count = styled
        self._duplicate_bv = duplicate

    def pool_sizes(self) -> dict[str, int]:
        """各风格池规模。WebUI 状态面板与启动自检都用它。"""
        return {style: len(self._style_index.get(style, [])) for style in STYLE_POOLS}

    def all_pool_sizes(self) -> dict[str, int]:
        """全部风格的规模（按数量降序），WebUI 上可以看全貌。"""
        return dict(
            sorted(
                ((k, len(v)) for k, v in self._style_index.items()),
                key=lambda item: item[1],
                reverse=True,
            )
        )

    # ------------------------------------------------------------- 落盘

    def load(self) -> bool:
        """从 rank.json 读入内存。文件不存在或损坏时返回 False。"""
        if not self._path.exists():
            return False
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("%s 榜单缓存读取失败（%s），将重新拉取", LOG_PREFIX, exc)
            return False
        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows:
            return False
        self._rows = rows
        self._updated_at = float(payload.get("updated_at") or 0.0)
        self._build_index()
        return True

    def _save(self) -> None:
        """原子写：先写临时文件再替换，避免写入中断留下半截 JSON。"""
        payload = {
            "version": _CACHE_VERSION,
            "updated_at": self._updated_at,
            "count": len(self._rows),
            "rows": [
                {key: row.get(key) for key in _KEEP_FIELDS} for row in self._rows
            ],
        }
        tmp = self._path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning("%s 榜单缓存写入失败（%s），本次只保留内存副本", LOG_PREFIX, exc)

    # ------------------------------------------------------------- 刷新

    async def refresh(self) -> int:
        """从 Notion 全量拉取并落盘。失败时抛异常，由调用方决定怎么处理。"""
        from .notion import NotionClient

        client = NotionClient()
        try:
            rows = await client.fetch_rows()
        finally:
            await client.aclose()

        rows = [
            {key: row.get(key) for key in _KEEP_FIELDS}
            for row in rows
            if row.get("bv")
        ]
        self._rows = rows
        self._updated_at = time.time()
        self._build_index()
        self._save()
        return len(rows)

    def is_stale(self, hours: float) -> bool:
        """缓存是否已过期。hours <= 0 视为永不过期。"""
        if hours <= 0:
            return False
        if not self._rows:
            return True
        return (time.time() - self._updated_at) > hours * 3600

    # ------------------------------------------------------------- 取歌

    def _eligible(
        self,
        positions: "Iterable[int]",
        gate: DurationGate | None,
        store: DurationStore | None,
    ) -> list[int]:
        """在候选下标里剔除「已知超限」和「已失效」的作品。

        v1.2.0：这只是**少走一趟冤枉路**的优化 —— 时长未知的作品一律保留，
        真正的判定在 ``_process`` 里做（那时 ``view`` 已经把真实时长带回来了）。
        把未知的也剔掉会把整个池子清空，所以这里必须"只剔已知的"。
        """
        if gate is None or store is None or not gate.enabled:
            return list(positions)
        kept: list[int] = []
        for position in positions:
            row = self._rows[position]
            if gate.allows(store.get(row.get("bv") or "")):
                kept.append(position)
        return kept

    def pick_random(
        self,
        top_n: int = 1000,
        pools: "Iterable[str] | None" = None,
        weights: dict[str, float] | None = None,
        gate: DurationGate | None = None,
        store: DurationStore | None = None,
    ) -> dict | None:
        """按「随机池」取一首歌。

        ``pools`` 是勾选的池名（含 ``POOL_TOP_LABEL``），``weights`` 是
        :func:`resolve_pool_weights` 算出的概率表。两者都为空时退回
        ``DEFAULT_POOLS``，保证永远有歌可出。

        ``gate`` + ``store`` 都给时，会先排除已知超时长 / 已失效的作品。
        三条兜底，**任何情况都不返回空**：

        1. 池内作品全被时长规则排除 → 该池回退为不过滤；
        2. 选中的池当前取不到歌（名字打错 / 风格本期消失）→ 概率让给其它选中池；
        3. 所有选中池都取不到歌 → 回退整榜。
        """
        if not self._rows:
            return None

        selected = [name for name in (pools or []) if isinstance(name, str) and name]
        if not selected:
            selected = list(DEFAULT_POOLS)
        plan = weights if isinstance(weights, dict) and weights else resolve_pool_weights(selected)

        limit = max(1, min(int(top_n), len(self._rows)))
        candidates: dict[str, list[int]] = {}
        for name in selected:
            if name == POOL_TOP_LABEL:
                positions = list(range(limit))
            else:
                positions = list(self._style_index.get(name) or [])
            if not positions:
                continue
            kept = self._eligible(positions, gate, store) or positions
            if kept:
                candidates[name] = kept

        if not candidates:
            logger.debug("%s 选中的池子当前都取不到歌，已回退整榜", LOG_PREFIX)
            positions = list(range(limit))
            kept = self._eligible(positions, gate, store) or positions
            return self._rows[random.choice(kept)] if kept else None

        names = list(candidates)
        odds = [max(0.0, float(plan.get(name, 0.0))) for name in names]
        if sum(odds) <= 0:  # 概率表没覆盖到这些池（例如全是手输错的池名）→ 等分兜底
            odds = [1.0] * len(names)

        source = random.choices(names, weights=odds, k=1)[0]
        logger.debug(
            "%s 随机取源：命中「%s」（%.1f%%），候选 %d 首",
            LOG_PREFIX,
            source,
            plan.get(source, 0.0),
            len(candidates[source]),
        )
        return self._rows[random.choice(candidates[source])]

    def search(self, keyword: str, limit: int = 5) -> list[dict]:
        """模糊搜索：标题或 UP主 命中全部关键词即可。

        结果按播放量降序（``_rows`` 本身就是榜单顺序），因此取前 limit 条
        就是播放量最高的 limit 条。
        """
        terms = [term.lower() for term in str(keyword).split() if term]
        if not terms:
            return []
        hits: list[dict] = []
        for row in self._rows:
            haystack = f"{row.get('title') or ''} {row.get('creator') or ''}".lower()
            if all(term in haystack for term in terms):
                hits.append(row)
                if len(hits) >= limit:
                    break
        return hits

    def get_by_bv(self, bv: str) -> dict | None:
        if not bv:
            return None
        for row in self._rows:
            if row.get("bv") == bv:
                return row
        return None

    # ------------------------------------------------------------- 统计

    def stats(self) -> dict:
        """给 WebUI 与启动日志用的快照。"""
        return {
            "count": len(self._rows),
            "updated_at": self._updated_at,
            "styled_count": self._styled_count,
            "duplicate_bv": self._duplicate_bv,
            "style_total": len(self._style_index),
            "pools": self.pool_sizes(),
        }
