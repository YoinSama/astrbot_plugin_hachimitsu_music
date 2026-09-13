"""Notion 私有 API 客户端 —— 拉取 hajihami 的 B站播放量总榜。

⚠️ 不要试图爬 HTML：Notion 站点服务端只吐一个空壳，正文全靠 JS 渲染。
榜单本质是一个 collection，直接走两个私有接口即可：

1. ``/api/v3/queryCollection``  → 一次拿到全量行 ID（响应是 NDJSON 流）
2. ``/api/v3/syncRecordValues`` → 按 ID 批量拿行内容（单批 3000 条，JSON）

体积实测：queryCollection 返回约 1.8MB / 8.4 秒；syncRecordValues 每批约 425KB / 14 秒。
13218 行约需 1 + 5 次请求，因此刷新周期是小时级而不是分钟级。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from .constants import (
    LOG_PREFIX,
    NOTION_API,
    NOTION_COLLECTION_ID,
    NOTION_SPACE_ID,
    NOTION_SYNC_BATCH,
    NOTION_VIEW_ID,
)
from .utils import logger


class NotionError(RuntimeError):
    """Notion 拉取失败：网络异常、重试耗尽、或响应结构变化。"""


# 与浏览器一致的请求头。Origin/Referer 用 notion.so 站点根即可。
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/x-ndjson, application/json",
    "Origin": "https://www.notion.so",
    "Referer": "https://www.notion.so/",
}

_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")


# ------------------------------------------------------------------ 属性解析
#
# Notion 的属性值形状不统一，且同一个字段在不同行里可能是不同形状
# （实测 c^GY 有 3 种形状、UKe` 有 3 种）。所以下面全部写成防御式：
# 遇到不认识的结构返回空值，绝不抛异常。


def unwrap(record: Any) -> dict:
    """剥掉 syncRecordValues 返回的 {'value': {...}} 包装。"""
    value = record
    while isinstance(value, dict) and "value" in value and isinstance(value["value"], dict):
        value = value["value"]
    return value if isinstance(value, dict) else {}


def plain_text(prop: Any) -> str:
    """取富文本属性的纯文本。

    Notion 的属性值最外层永远是数组，元素是「富文本段」。
    只取 ``prop[0][0]``；直接 join 整个结构会得到重复文本。
    """
    if not isinstance(prop, list) or not prop:
        return ""
    segment = prop[0]
    if isinstance(segment, str):
        return segment
    if isinstance(segment, list) and segment and isinstance(segment[0], str):
        return segment[0]
    return ""


def multi_select(prop: Any) -> list[str]:
    """解析 multi_select 属性。

    🔴 这是本项目最容易踩的坑：multi_select 的原始值形状和普通文本**一模一样**，
    但 ``prop[0][0]`` 拿到的是「用英文逗号拼接的一整串」，而不是单个标签。
    必须 split(",") 才能拿到独立标签。

    漏掉 split 的后果是静默的：只标 1 个标签的行看起来正常（问题被掩盖），
    标 2 个以上的行会被当成「一个不存在的标签」。
    实测 13218 行里不 split 会得到 89 种假标签、split 后只有 17 种真标签，
    且「曼波好听～ / 冰🧊！/ 婉约派 / 哈基周金曲」四个会直接变成 0。
    """
    if not isinstance(prop, list) or not prop:
        return []
    first = prop[0]
    if isinstance(first, str):
        return [t.strip() for t in first.split(",") if t.strip()]
    if not isinstance(first, list) or not first:
        return []
    parts: list[str] = []
    for item in first:
        if not isinstance(item, str):
            continue
        parts.extend(item.split(","))
    return [t.strip() for t in parts if t.strip()]


def extract_bv(url: str) -> str:
    """从任意形式的视频链接里提取 BV 号。"""
    if not url:
        return ""
    match = _BV_RE.search(url)
    return match.group(0) if match else ""


# ------------------------------------------------------------------ 客户端


class NotionClient:
    """Notion 私有 API 的最小客户端。"""

    def __init__(self, timeout: float = 180.0, retries: int = 5) -> None:
        self._timeout = timeout
        self._retries = retries
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, payload: dict[str, Any]) -> str:
        """POST 并带回退重试。Notion 在高频小请求下会直接重置连接。"""
        url = f"{NOTION_API}{path}"
        last_error: Exception | None = None
        for attempt in range(self._retries):
            try:
                resp = await self._client.post(url, json=payload, headers=_HEADERS)
                resp.raise_for_status()
                return resp.text
            except Exception as exc:  # noqa: BLE001 - 需要兜住所有网络异常
                last_error = exc
                if attempt < self._retries - 1:
                    delay = 1.5 * (attempt + 1)
                    logger.warning(
                        "%s Notion 请求 %s 失败（%s），%.1f 秒后重试（第 %d/%d 次）",
                        LOG_PREFIX,
                        path,
                        exc,
                        delay,
                        attempt + 1,
                        self._retries,
                    )
                    await asyncio.sleep(delay)
        raise NotionError(f"请求 {path} 连续 {self._retries} 次失败：{last_error}") from last_error

    # ---------------------------------------------------------- 两步取数

    async def fetch_row_ids(self, limit: int = 20000) -> list[str]:
        """第一步：拿到全量行 ID（按播放量降序）。"""
        payload = {
            "collection": {"id": NOTION_COLLECTION_ID, "spaceId": NOTION_SPACE_ID},
            "collectionView": {"id": NOTION_VIEW_ID, "spaceId": NOTION_SPACE_ID},
            "loader": {
                "type": "reducer",
                "reducers": {
                    "collection_group_results": {"type": "results", "limit": limit}
                },
                # 显式按播放量降序。若传空数组会覆盖视图自带排序，导致名次全乱。
                "sort": [{"property": "f}<W", "direction": "descending"}],
                "filter": {"operator": "and", "filters": []},
                "searchQuery": "",
                "userTimeZone": "Asia/Shanghai",
            },
            "query": "",
            "aggregationQueries": [],
        }
        logger.info("%s 正在向 Notion 拉取榜单列表…", LOG_PREFIX)
        text = await self._post("/queryCollection", payload)

        # 响应是 NDJSON：逐行 JSON，取 reducerResults 里的 blockIds。
        ids: list[str] = []
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            reducer = (
                chunk.get("result", {})
                .get("reducerResults", {})
                .get("collection_group_results", {})
            )
            ids.extend(reducer.get("blockIds", []) or [])
        return ids

    async def fetch_blocks(self, row_ids: list[str]) -> dict[str, dict]:
        """第二步：按 ID 批量取行内容。

        单批 3000 条是实测出来的上限 —— 早期用 100 条连发，约 132 次后
        就会触发 ``Errno 10053`` 连接重置，重试也救不回来。
        """
        blocks: dict[str, dict] = {}
        total = len(row_ids)
        batches = range(0, total, NOTION_SYNC_BATCH)
        for index, start in enumerate(batches, start=1):
            chunk = row_ids[start : start + NOTION_SYNC_BATCH]
            requests = [
                {
                    "pointer": {"table": "block", "id": row_id, "spaceId": NOTION_SPACE_ID},
                    "version": -1,
                }
                for row_id in chunk
            ]
            text = await self._post("/syncRecordValues", {"requests": requests})
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise NotionError(f"syncRecordValues 返回的不是合法 JSON：{exc}") from exc
            blocks.update(payload.get("recordMap", {}).get("block", {}) or {})
            logger.info(
                "%s 正在下载榜单内容（%d/%d，本批 %d 条）…",
                LOG_PREFIX,
                min(start + NOTION_SYNC_BATCH, total),
                total,
                len(chunk),
            )
        return blocks

    # ---------------------------------------------------------- 高层接口

    async def fetch_rows(self, limit: int = 20000) -> list[dict]:
        """拉取全量榜单并解析成业务行。

        返回按播放量降序排列的列表，每项形如::

            {"rank": 1, "bv": "BV1xx...", "title": "...",
             "creator": "...", "play": 12345, "styles": ["冰🧊！"], "raw_url": "..."}
        """
        from .constants import FIELD_CREATOR, FIELD_PLAY, FIELD_STYLE, FIELD_TITLE, FIELD_URL

        row_ids = await self.fetch_row_ids(limit=limit)
        if not row_ids:
            raise NotionError("queryCollection 未返回任何行 ID —— 页面结构可能已变化")

        blocks = await self.fetch_blocks(row_ids)

        rows: list[dict] = []
        for row_id in row_ids:
            record = blocks.get(row_id)
            if not record:
                continue
            props = unwrap(record).get("properties") or {}
            if not isinstance(props, dict):
                continue

            raw_url = plain_text(props.get(FIELD_URL))
            bv = extract_bv(raw_url)
            play_text = plain_text(props.get(FIELD_PLAY))
            rows.append(
                {
                    "rank": len(rows) + 1,
                    "bv": bv,
                    "title": plain_text(props.get(FIELD_TITLE)),
                    "creator": plain_text(props.get(FIELD_CREATOR)),
                    "play": int(play_text) if play_text.isdigit() else None,
                    "styles": multi_select(props.get(FIELD_STYLE)),
                    "raw_url": raw_url,
                }
            )
        return rows
