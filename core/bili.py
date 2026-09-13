"""B站 API 客户端：WBI 签名、view（拿 cid / UP主）、playurl（拿 DASH 音轨）。

请求特征完全对齐生产插件 ``astrbot_plugin_media_parser``：

- **全量 WBI 签名** —— 所有 WBI 端点都签名，密钥缓存 6 小时 + ``asyncio.Lock``
  防止并发重复拉取。
- **规范浏览器请求头** —— UA / Referer 用站点根 / Origin / Accept-Language /
  Accept-Encoding。
- **完全不发设备身份** —— 不领 buvid、不发 b_lsid / _uuid。media_parser 整个
  代码库都没有这些字段，实测同样稳定，所以不引入这些不确定性。

⚠️ playurl 的参数集必须用 media_parser 这套，**不要用 Bilibili-Evolved 的**。
Bilibili-Evolved 跑在真实浏览器里，完整指纹 + 登录 Cookie 是它的护身符，
参数不规范也无所谓；我们脱离浏览器，这个豁免不存在。

关于风控（核心认知：**频率不是主因，请求特征才是**）：
``-352`` 风控校验失败、``-412`` IP 被风控（只能换 IP 或等）、``-799`` 请求过于频繁。
其中 -352 的恢复需要人工过滑块验证码，无人值守的机器人做不到，所以策略只能是
「预防 + 熔断」，不能指望「恢复」。
"""

from __future__ import annotations

import asyncio
import email.utils
import hashlib
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from .constants import LOG_PREFIX
from .utils import logger

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7"
SITE_ROOT = "https://www.bilibili.com"

NAV_API = "https://api.bilibili.com/x/web-interface/nav"
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
PLAYURL_API = "https://api.bilibili.com/x/player/wbi/playurl"

WBI_KEY_TTL_SECONDS = 6 * 60 * 60

# WBI 混排表（B站 公开的 64 项置换表）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# 命中这些错误码说明是被风控/限流了，应触发熔断而不是重试
RISK_CODES = {-352, -412, -799}

# 服务器时间与本地时间的允许偏差。B站 会比对 wts 与服务器时间，
# 偏差过大直接拒收（防重放，不是风控）。本机时间不准时会出现
# 「签名明明算对了却一直失败」这种很难查的问题，所以启动后自动校准。
TS_TOLERANCE_SECONDS = 30


class BiliError(RuntimeError):
    """B站 接口返回失败。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class BiliRiskError(BiliError):
    """命中 B站 风控（-352 / -412 / -799），应触发熔断。"""


def _check(payload: dict) -> dict:
    """校验 B站 的 ``code`` 字段并把 ``data`` 取出来。"""
    code = payload.get("code")
    if code == 0:
        data = payload.get("data")
        return data if isinstance(data, dict) else {}
    message = payload.get("message") or payload.get("msg") or "未知错误"
    if code in RISK_CODES:
        raise BiliRiskError(f"B站 风控（code={code}）：{message}", code=code)
    raise BiliError(f"B站 接口返回错误（code={code}）：{message}", code=code)


class BiliClient:
    """B站 接口客户端。一个实例复用到插件结束，内部维护连接池与 WBI 密钥缓存。"""

    def __init__(
        self, cookie: str = "", timeout: float = 30.0, max_concurrency: int = 3
    ) -> None:
        self._cookie = (cookie or "").strip()
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._mixin_key = ""
        self._mixin_key_expires = 0.0
        self._mixin_key_lock = asyncio.Lock()
        self._ts_offset = 0.0

    # ------------------------------------------------------------- 生命周期

    async def aclose(self) -> None:
        await self._client.aclose()

    def set_cookie(self, cookie: str) -> None:
        """运行时更新 Cookie（扫码续期成功后调用）。"""
        self._cookie = (cookie or "").strip()
        # Cookie 变化可能影响 nav 返回的登录态，但 WBI 密钥本身与登录无关，无需清缓存。

    @property
    def cookie(self) -> str:
        return self._cookie

    # ------------------------------------------------------------- 请求头

    def _api_headers(self) -> dict[str, str]:
        """API 请求头。"""
        headers = {
            "User-Agent": UA,
            "Referer": SITE_ROOT,
            "Origin": SITE_ROOT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": ACCEPT_LANGUAGE,
            "Accept-Encoding": "gzip, deflate",
        }
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers

    def media_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """媒体（音频流）下载请求头。"""
        headers = {
            "User-Agent": UA,
            "Referer": SITE_ROOT,
            "Origin": SITE_ROOT,
            "Accept": "*/*",
            "Accept-Language": ACCEPT_LANGUAGE,
            "Accept-Encoding": "gzip, deflate",
        }
        if self._cookie:
            headers["Cookie"] = self._cookie
        if extra:
            headers.update(extra)
        return headers

    # ------------------------------------------------------------- 时间校准

    def _update_ts_offset(self, date_header: str | None) -> None:
        """用响应头里的服务器时间校准本地时钟偏移。"""
        if not date_header:
            return
        try:
            server_ts = email.utils.parsedate_to_datetime(date_header).timestamp()
        except (TypeError, ValueError):
            return
        offset = server_ts - time.time()
        if abs(offset) > TS_TOLERANCE_SECONDS:
            logger.warning(
                "%s 本机时间与 B站 服务器相差 %.0f 秒，已自动校准（建议检查系统时间）",
                LOG_PREFIX,
                offset,
            )
        self._ts_offset = offset

    def _now_ts(self) -> int:
        return int(time.time() + self._ts_offset)

    # ------------------------------------------------------------- 基础请求

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict:
        async with self._semaphore:
            resp = await self._client.get(url, params=params, headers=self._api_headers())
        self._update_ts_offset(resp.headers.get("Date"))
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError as exc:
            raise BiliError(f"{url} 返回的不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise BiliError(f"{url} 返回结构异常")
        return payload

    async def raw_get(
        self, url: str, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        """给凭据流程用的原始 GET。

        扫码登录需要读响应头里的 ``Set-Cookie``，而且 ``passport`` 接口的
        ``code`` 语义与业务接口不同（轮询时的 86090 / 86101 都算正常进行中），
        所以这里不做统一校验，交给调用方判断。
        """
        async with self._semaphore:
            resp = await self._client.get(url, params=params, headers=self._api_headers())
        self._update_ts_offset(resp.headers.get("Date"))
        resp.raise_for_status()
        return resp

    # ------------------------------------------------------------- WBI 签名

    @staticmethod
    def _key_from_url(url: str) -> str:
        """从 wbi_img 的 URL 里取文件名（不含扩展名）作为密钥片段。"""
        return Path(urlparse(url).path).stem

    @staticmethod
    def mix_keys(img_key: str, sub_key: str) -> str:
        """按置换表混排 img_key + sub_key，取前 32 位作为 mixin_key。"""
        raw = img_key + sub_key
        return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]

    def _sign(self, params: dict[str, Any], mixin_key: str) -> dict[str, str]:
        """给参数追加 ``wts`` 与 ``w_rid``。

        规则：加 wts → 按 key 排序 → 剔除值里的 ``!'()*`` → urlencode →
        md5(query + mixin_key)。
        """
        signed = dict(params)
        signed["wts"] = self._now_ts()
        signed = dict(sorted(signed.items(), key=lambda item: item[0]))

        filtered: dict[str, str] = {}
        for key, value in signed.items():
            text = str(value)
            for ch in "!'()*":
                text = text.replace(ch, "")
            filtered[key] = text

        query = urlencode(filtered)
        filtered["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
        return filtered

    async def get_nav(self) -> dict:
        """请求 nav 接口，返回 ``data`` 段（含 isLogin / wbi_img / vipStatus 等）。

        ⚠️ 两个实测坑：

        1. **未登录时这个接口返回 ``code=-101``（账号未登录），但 ``data`` 里
           仍然带着 ``wbi_img``** —— 匿名模式的 WBI 签名密钥就靠它。所以 -101
           必须放行，不能当成错误，否则匿名模式整个跑不起来。
        2. 未登录时**不会**返回 ``vipStatus`` / ``vipType`` / ``vip``，匿名 ``data``
           只有 ``isLogin`` / ``wbi_img`` / ``ip_region`` 三个键。取值一律用
           ``.get()``，直接索引会 KeyError。
        """
        payload = await self._get_json(NAV_API)
        code = payload.get("code")
        if code in (0, -101):  # -101 = 未登录，但 data 依然可用
            data = payload.get("data")
            return data if isinstance(data, dict) else {}
        return _check(payload)

    async def get_mixin_key(self) -> str:
        """拿 mixin_key，带 6 小时缓存与并发锁。"""
        async with self._mixin_key_lock:
            now = time.monotonic()
            if self._mixin_key and now < self._mixin_key_expires:
                return self._mixin_key

            logger.debug("%s 正在获取 WBI 签名密钥…", LOG_PREFIX)
            nav_data = await self.get_nav()
            wbi_img = nav_data.get("wbi_img") or {}
            img_url = str(wbi_img.get("img_url") or "").strip()
            sub_url = str(wbi_img.get("sub_url") or "").strip()
            if not img_url or not sub_url:
                raise BiliError("获取 WBI 密钥失败：nav 未返回 img_url / sub_url")

            img_key = self._key_from_url(img_url)
            sub_key = self._key_from_url(sub_url)
            if not img_key or not sub_key:
                raise BiliError("获取 WBI 密钥失败：img_key / sub_key 为空")

            self._mixin_key = self.mix_keys(img_key, sub_key)
            self._mixin_key_expires = now + WBI_KEY_TTL_SECONDS
            logger.debug("%s WBI 密钥已刷新（有效期 6 小时）", LOG_PREFIX)
            return self._mixin_key

    # ------------------------------------------------------------- 业务接口

    async def get_view(self, bv: str) -> dict:
        """稿件信息：cid / 标题 / 时长 / 投稿 UP主。

        UP主 取 ``owner.name``（B站 投稿号）。注意榜单里的「全民制作人」是另一个
        字段，两者经常不同。
        """
        logger.info("%s 正在查询稿件信息（%s）…", LOG_PREFIX, bv)
        payload = await self._get_json(VIEW_API, {"bvid": bv})
        data = _check(payload)
        owner = data.get("owner") or {}
        return {
            "cid": data.get("cid"),
            "title": data.get("title") or "",
            "duration": data.get("duration") or 0,
            "up_name": owner.get("name") or "",
            "up_mid": owner.get("mid"),
        }

    async def get_playurl(self, bv: str, cid: int) -> dict:
        """拿 DASH 播放信息（含多档音轨）。

        参数集与 media_parser 完全一致：``bvid`` 而非 ``avid``、``qn=80``
        （实测 ``qn`` 完全不影响音频轨，用 127 既无用又超权限）、
        ``platform=pc``、``high_quality=1``。
        """
        logger.info("%s 正在获取音频播放地址（%s）…", LOG_PREFIX, bv)
        mixin_key = await self.get_mixin_key()
        params = {
            "bvid": bv,
            "cid": cid,
            "qn": 80,
            "fnver": 0,
            "fnval": 4048,
            "fourk": 1,
            "otype": "json",
            "platform": "pc",
            "high_quality": 1,
        }
        payload = await self._get_json(PLAYURL_API, self._sign(params, mixin_key))
        return _check(payload)

    async def get_views(self, bvs: list[str]) -> list[dict | None]:
        """并发拿多个稿件的 view（搜索结果要显示 UP主 名，一次要 5 条）。

        失败的位置返回 None，由调用方降级成「不显示 UP主名」，不阻塞搜索。
        """

        async def _one(bv: str) -> dict | None:
            try:
                return await self.get_view(bv)
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响整体
                logger.debug("%s 获取 %s 的 UP主 信息失败：%s", LOG_PREFIX, bv, exc)
                return None

        return list(await asyncio.gather(*(_one(bv) for bv in bvs)))
