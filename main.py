"""哈基米音乐点歌插件 —— 主入口。

群里发 ``/哈基米`` 随机来一首，``/哈基米 关键词`` 搜索后点播，
拉到的音频转成紧凑 mp3 以语音消息发出。

架构（每层只做一件事，便于单独验证）：

    core/constants.py  全局常量（含必须逐字节一致的全角风格名）
    core/utils.py      日志、ffmpeg 线程池调用、数据目录、格式化
    core/notion.py     Notion 私有 API：queryCollection + syncRecordValues
    core/rank.py       RankStore：缓存 / 风格索引 / 随机 / 搜索 / URL 清洗
    core/bili.py       B站：WBI 签名、view、playurl
    core/quality.py    音轨择优 + 会员门控 + 三级降级（含兜底）
    core/audio.py      下载 + Range 续传 + LRU 缓存
    core/encoder.py    ffmpeg 转码（线程池）
    core/delivery.py   语音投递 + 降级链
    core/guard.py      四层闸门：准入 / 限流 / 队列 / 去重
    core/auth.py       Cookie 校验与会员判定
    core/cookie_assist.py  管理员协助扫码登录
    core/webapi.py     WebUI 控制台后端接口

⚠️ 禁用 ``requests``（官方要求），所有网络请求走 httpx 异步；
⚠️ ffmpeg 在 ``asyncio.to_thread`` 里调用，绝不阻塞事件循环；
⚠️ 持久化数据写 ``data/plugin_data/<plugin_name>/``，不写插件目录。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.audio import AudioDownloadError, AudioFetcher
from .core.auth import AuthRuntime
from .core.bili import BiliClient, BiliError, BiliRiskError
from .core.constants import (
    DEAD_VIEW_CODES,
    DURATION_MAX_DEFAULT,
    DURATION_RESAMPLE_DEFAULT,
    LOG_PREFIX,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    SEARCH_CANDIDATE_EXTRA,
    SEARCH_LIMIT,
)
from .core.cookie_assist import CookieAssistant
from .core.delivery import build_caption, send_voice
from .core.duration import DEAD, DurationGate, DurationRejected, DurationStore
from .core.encoder import transcode_to_mp3
from .core.guard import Guard, QueueFull
from .core.limits import describe, resolve_bound, resolve_positive
from .core.logging_noise import install_noise_filter
from .core.quality import NoAudioTrack, pick_audio_track, track_size, track_url
from .core.rank import RankStore, video_url
from .core.utils import humanize_ago, humanize_size, plugin_data_dir
from .core.webapi import WebAPI

HELP_TEXT = (
    "哈基米音乐点歌\n"
    "· /哈基米 —— 随机来一首\n"
    "· /哈基米 <关键词> —— 搜索后回复序号点播\n"
    "· /哈基米 帮助 —— 显示这份说明\n"
    "· /哈基米状态 —— 查看运行状态（管理员）"
)

STATUS_WORDS = {"状态", "status"}
HELP_WORDS = {"帮助", "help", "?", "？"}


@register(PLUGIN_NAME, "Yoin", "哈基米音乐点歌：随机或搜索点播，音频以语音消息发出", PLUGIN_VERSION)
class HachimitsuMusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 挂上噪音过滤器：httpx 默认会在 INFO 级打印每一条请求的完整 URL，
        # 既刷屏又暴露带签名的地址（upsig / deadline / trid 之类都在里面）。
        # 这里只屏蔽我们自己的上游请求，真正的进度由插件用中文日志自己打出来。
        install_noise_filter()

        data_dir = plugin_data_dir()
        self.rank = RankStore(data_dir / "rank.json")
        # 作品时长缓存：随每次点播免费积累，不做全量预热
        self.duration = DurationStore(data_dir / "duration.json")
        self.duration.load()
        self.guard = Guard(config, data_dir)
        self.auth = AuthRuntime(config, data_dir)
        self.bili = BiliClient(timeout=30.0, max_concurrency=3)
        self.fetcher = AudioFetcher(data_dir / "audio", max_mb=self._cfg_int("limit", "cache_max_mb", 2048))
        self.cookie_assistant = CookieAssistant(self.auth, self.bili, data_dir)
        self.cookie_assistant.bind_context(context)

        self._webapi = WebAPI(self)
        self._register_web_apis()

        # 榜单拉取要 8~10 秒，不能阻塞插件加载
        try:
            asyncio.get_running_loop().create_task(self._startup())
        except RuntimeError:
            logger.warning("%s 当前没有事件循环，跳过启动预热", LOG_PREFIX)

    # =============================================================== 生命周期

    async def _startup(self) -> None:
        started = time.time()
        logger.info("%s 开始加载插件（版本 %s）…", LOG_PREFIX, PLUGIN_VERSION)

        # ① 配置
        quality = self.config.get("audio_quality", "192k")
        preset = self.config.get("vocal_preset", "standard")
        top_weight = self._cfg_int("random", "top_weight", 50)
        style_weight = self._cfg_int("random", "style_weight", 10)
        logger.info(
            "%s ① 读取配置完成 —— 下载音质=%s，发送档位=%s，随机权重=总榜%d/每风格%d",
            LOG_PREFIX,
            quality,
            preset,
            top_weight,
            style_weight,
        )
        # 无效输入（填 0 / 填了非数字）已被自动纠正，明确告诉管理员
        for item in self.corrections():
            logger.warning(
                "%s 配置已自动纠正 —— %s：%s", LOG_PREFIX, item["key"], item["message"]
            )
        logger.info("%s ①b 作品时长限制 —— %s", LOG_PREFIX, self._duration_gate().describe())

        # ② 榜单缓存
        loaded = self.rank.load()
        if loaded:
            stats = self.rank.stats()
            logger.info(
                "%s ② 榜单缓存命中 —— %s 条，更新于 %s（带风格标签 %d 条 / 重复 BV %d 个）",
                LOG_PREFIX,
                f"{stats['count']:,}",
                humanize_ago(stats["updated_at"]),
                stats["styled_count"],
                stats["duplicate_bv"],
            )
            self._log_pool_sizes()
        else:
            logger.info("%s ② 榜单缓存为空，开始从 Notion 拉取…", LOG_PREFIX)

        hours = self._cfg_int("random", "rank_refresh_hours", 24)
        if (not loaded) or self.rank.is_stale(hours):
            try:
                count = await self.rank.refresh()
                logger.info("%s ② 榜单已刷新 —— %s 条", LOG_PREFIX, f"{count:,}")
                self._log_pool_sizes()
            except Exception as exc:  # noqa: BLE001 - 拉取失败不该阻断插件加载
                logger.warning("%s 榜单拉取失败（%s），将沿用已有缓存", LOG_PREFIX, exc)

        # ③ Cookie 状态
        try:
            # 先把配置/扫码得到的 Cookie 喂给 BiliClient，
            # 否则 refresh 走的是匿名 nav，会误报「Cookie 已失效或未登录」。
            self.bili.set_cookie(self.auth.cookie_header())
            state = await self.auth.refresh(self.bili)
            await self._apply_cookie(state)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s Cookie 状态检查失败（%s）", LOG_PREFIX, exc)

        # ④ 缓存目录
        logger.info("%s ④ 音频缓存目录就绪 —— %s", LOG_PREFIX, self.fetcher.cache_dir)

        # ⑤ 接口与页面
        page_dir = Path(__file__).parent.as_posix()
        logger.info(
            "%s ⑤ 已注册 %d 个 Web 接口、1 个插件页面（%s/pages/console）",
            LOG_PREFIX,
            self._webapi.route_count,
            page_dir,
        )

        # ⑥ 完成
        logger.info("%s 插件加载完成，耗时 %.2f 秒", LOG_PREFIX, time.time() - started)

    async def _apply_cookie(self, state) -> None:
        """把校验结果同步给 BiliClient，并在失效时触发管理员协助。"""
        self.bili.set_cookie(self.auth.cookie_header())
        if state.logged_in:
            logger.info(
                "%s ③ B站 Cookie —— 已登录（%s），%s",
                LOG_PREFIX,
                state.uname or "未知账号",
                "大会员，可用 Hi-Res 音轨" if state.is_vip else "非大会员，音质上限 192K",
            )
        else:
            logger.info(
                "%s ③ B站 Cookie —— %s，将以匿名方式请求（音质上限 192K）",
                LOG_PREFIX,
                state.reason or "未配置",
            )

    def _log_pool_sizes(self) -> None:
        """打印各风格池规模（自检基线，出现 0 就说明解析出了问题）。"""
        pools = self.rank.pool_sizes()
        detail = " · ".join(f"{name} {count}" for name, count in pools.items())
        logger.info("%s ③ 风格池索引已建立 —— %s", LOG_PREFIX, detail)

    async def terminate(self) -> None:
        """插件卸载/停用时的收尾。"""
        try:
            await self.bili.aclose()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.fetcher.aclose()
        except Exception:  # noqa: BLE001
            pass
        logger.info("%s 插件已卸载", LOG_PREFIX)

    # =============================================================== 配置工具

    def _section(self, name: str) -> dict:
        value = self.config.get(name) if hasattr(self.config, "get") else None
        return value if isinstance(value, dict) else {}

    def _cfg_int(self, section: str, key: str, default: int) -> int:
        try:
            return int(self._section(section).get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_str(self, key: str, default: str) -> str:
        value = self.config.get(key, default) if hasattr(self.config, "get") else default
        return str(value or default)

    def _is_admin(self, user_id) -> bool:
        return str(user_id) in (self.auth.admin_ids() or [])

    # ------------------------------------------------------- 时长规则（v1.2.0）

    def _duration_gate(self) -> DurationGate:
        """按当前配置构造时长闸门。

        每次点歌现取，所以 WebUI 改完配置**立刻生效**，不用重载插件。
        """
        section = self._section("duration")
        low, _ = resolve_bound(section.get("min_seconds"), 0)
        high, _ = resolve_bound(section.get("max_seconds"), DURATION_MAX_DEFAULT)
        return DurationGate(low, high)

    def _resample_attempts(self, resample, gate: DurationGate) -> int:
        """最多试几首歌才放弃过滤。闸门没开就只试 1 次。"""
        if resample is None or not gate.enabled:
            return 1
        value, _ = resolve_positive(
            self._section("duration").get("resample_max"), DURATION_RESAMPLE_DEFAULT
        )
        return max(1, value)

    def _duration_corrections(self) -> list[dict]:
        """``duration`` 组里的无效输入清单。"""
        section = self._section("duration")
        out: list[dict] = []
        for key, default, resolver in (
            ("min_seconds", 0, resolve_bound),
            ("max_seconds", DURATION_MAX_DEFAULT, resolve_bound),
            ("resample_max", DURATION_RESAMPLE_DEFAULT, resolve_positive),
        ):
            _, note = resolver(section.get(key), default)
            if note:
                out.append({"key": f"duration.{key}", "message": note})
        return out

    def corrections(self) -> list[dict]:
        """全部「无效输入已被自动纠正」的说明，供启动日志与 WebUI 提示条使用。"""
        return [*self.guard.corrections, *self._duration_corrections()]

    def _remember_duration(self, bv: str, view: dict | None) -> int | None:
        """把 ``view`` 里白送的时长记进缓存并返回秒数。

        拿不到（接口失败 / 字段缺失）返回 ``None`` —— **未知一律放行**，
        硬当成超限会把整池歌都排除掉。
        """
        if not bv or not isinstance(view, dict):
            return None
        try:
            seconds = int(view.get("duration") or 0)
        except (TypeError, ValueError):
            return None
        if seconds <= 0:
            return None
        self.duration.put(bv, seconds)
        return seconds

    @staticmethod
    def _extract_arg(message_str: str) -> str:
        """剥离指令名，取后面的参数。``/哈基米 曼波`` → ``曼波``。"""
        text = (message_str or "").strip()
        parts = text.split(None, 1)
        return parts[1].strip() if len(parts) > 1 else ""

    # =============================================================== 指令

    @filter.command("哈基米")
    async def hajihami(self, event: AstrMessageEvent):
        """哈基米音乐点歌：不带参数随机一首；带关键词则模糊搜索前 5 个结果。"""
        if not self.config.get("enabled", True):
            await event.send(event.plain_result("点歌功能已关闭。"))
            return

        arg = self._extract_arg(event.message_str)

        if arg in HELP_WORDS:
            await event.send(event.plain_result(self._help_text()))
            return

        if arg in STATUS_WORDS:
            if not self._is_admin(event.get_sender_id()):
                await event.send(event.plain_result("只有管理员可以查看运行状态。"))
                return
            await event.send(event.plain_result(self._status_text()))
            return

        if arg:
            await self._search_flow(event, arg)
            return

        await self._random_flow(event)

    @filter.command("哈基米状态")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def hajihami_status(self, event: AstrMessageEvent):
        """查看哈基米音乐点歌插件的运行状态（榜单 / 音质 / 熔断 / 队列 / 今日计数）。仅管理员可用。"""
        await event.send(event.plain_result(self._status_text()))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """处理管理员对 Cookie 续期请求的回复（「确定」/「取消」）。"""
        try:
            if self.cookie_assistant.try_handle(event):
                event.stop_event()
        except Exception as exc:  # noqa: BLE001 - 不能因为协助流程炸掉消息处理
            logger.debug("%s 处理 Cookie 协助回复时出错：%s", LOG_PREFIX, exc)

    # =============================================================== 随机点歌

    async def _random_flow(self, event: AstrMessageEvent) -> None:
        gate = self._duration_gate()

        def _pick() -> dict | None:
            return self.rank.pick_random(
                top_n=self._cfg_int("random", "top_n", 1000),
                top_weight=self._cfg_int("random", "top_weight", 50),
                style_weight=self._cfg_int("random", "style_weight", 10),
                gate=gate,
                store=self.duration,
            )

        row = _pick()
        if not row:
            await event.send(
                event.plain_result("榜单还没准备好，请稍等片刻再试（或让管理员查看状态）。")
            )
            return
        # 抽到超限作品时用它换一首（用户全程无感，见 _deliver 的重抽循环）
        await self._deliver(event, row, resample=_pick)

    # =============================================================== 搜索点歌

    async def _search_flow(self, event: AstrMessageEvent, keyword: str) -> None:
        # v1.2.0：多取几条候选。get_views 是并发的，多取 3 条几乎不增加耗时，
        # 而超时的作品被剔掉后，用备选顶上仍能凑满 SEARCH_LIMIT 条。
        pool = self.rank.search(keyword, limit=SEARCH_LIMIT + SEARCH_CANDIDATE_EXTRA)
        if not pool:
            await event.send(event.plain_result(f"没有找到和「{keyword}」相关的作品。"))
            return

        # 并发拿 UP主 名（顺带把 duration 一起免费拿到；失败的位置返回 None）
        pool_views = await self.bili.get_views([row.get("bv", "") for row in pool])

        gate = self._duration_gate()
        rows: list[dict] = []
        views: list[dict | None] = []
        for row, view in zip(pool, pool_views):
            seconds = self._remember_duration(row.get("bv") or "", view)
            if gate.enabled and not gate.allows(seconds):
                logger.debug(
                    "%s 搜索结果已剔除《%s》（%s 秒，超出 %s）",
                    LOG_PREFIX,
                    (row.get("title") or "")[:20],
                    seconds,
                    gate.describe(),
                )
                continue
            rows.append(row)
            views.append(view)
            if len(rows) >= SEARCH_LIMIT:
                break
        self.duration.flush()

        if not rows:
            await event.send(
                event.plain_result("没找到符合条件时长的作品，换个关键词试试。")
            )
            return

        lines = [f"模糊搜索前{len(rows)}个结果："]
        for index, (row, view) in enumerate(zip(rows, views), start=1):
            title = (row.get("title") or "未知作品").strip()
            up_name = (view or {}).get("up_name") or ""
            lines.append(f"{index}. {title}" + (f" - {up_name}" if up_name else ""))
        lines.append("（60 秒内回复序号，或回复「取消」）")
        await event.send(event.plain_result("\n".join(lines)))

        from astrbot.core.utils.session_waiter import SessionController, session_waiter

        chosen: dict = {}

        @session_waiter(timeout=60, record_history_chains=False)
        async def waiter(controller: SessionController, inner_event: AstrMessageEvent):
            text = (inner_event.message_str or "").strip()
            if text in ("取消", "cancel", "算了"):
                chosen["cancel"] = True
                await inner_event.send(inner_event.plain_result("已取消。"))
                controller.stop()
                return
            if text.isdigit() and 1 <= int(text) <= len(rows):
                chosen["index"] = int(text) - 1
                controller.stop()
                return
            await inner_event.send(
                inner_event.plain_result(f"请输入 1~{len(rows)} 的序号，或回复「取消」。")
            )

        try:
            await waiter(event)
        except TimeoutError:
            await event.send(event.plain_result("等太久啦，已取消这次点歌。"))
            return
        finally:
            event.stop_event()

        index = chosen.get("index")
        if index is None:
            return

        row = rows[index]
        view = views[index] if index < len(views) else None
        await self._deliver(event, row, view=view)

    # =============================================================== 投递主流程

    async def _deliver(
        self,
        event: AstrMessageEvent,
        row: dict,
        view: dict | None = None,
        resample=None,
    ) -> None:
        """把一个榜单行变成语音消息发出去。所有拒绝都明确回复原因。

        ``resample`` 是「换一首」的回调（返回新的榜单行或 ``None``）。
        随机点歌会传它 —— 抽到超限 / 失效的作品时**静默换一首再试**，
        用户全程无感（实测一次 ``view`` 仅 0.08 秒）。搜索点歌不传：
        结果列表已经过滤过，用户点哪首就是哪首。
        """
        group_id = event.get_group_id() or ""
        user_id = str(event.get_sender_id())

        # 熔断
        remaining = self.guard.circuit_remaining()
        if remaining > 0:
            await event.send(
                event.plain_result(
                    f"B站 侧暂时限制访问，已暂停点歌 {remaining} 秒（原因：{self.guard.circuit_reason}）。"
                )
            )
            return

        # 去重：窗口内同曲直接复用缓存，不占配额
        duplicate = self.guard.is_recent(bv)
        preset = self._cfg_str("vocal_preset", "standard")
        cached_mp3 = self.fetcher.mp3_path(bv, preset)
        if duplicate and cached_mp3.exists() and view is None:
            logger.debug("%s 命中同曲去重窗口，直接复用缓存（%s）", LOG_PREFIX, bv)
            await self._send_result(event, row, "", url, cached_mp3)
            return

        # 准入 + 限流
        decision = self.guard.check_access(group_id, user_id)
        if not decision.allowed:
            await event.send(event.plain_result(decision.message))
            return
        decision = self.guard.check_rate(group_id, user_id)
        if not decision.allowed:
            logger.debug("%s 限流拦截：%s", LOG_PREFIX, decision.reason)
            await event.send(event.plain_result(decision.message))
            return

        preset = self._cfg_str("vocal_preset", "standard")
        timeout = self._cfg_int("limit", "task_timeout_seconds", 30)
        gate = self._duration_gate()
        attempts = self._resample_attempts(resample, gate)

        current_row = row
        current_view = view
        for attempt in range(attempts):
            current_bv = current_row.get("bv") or ""

            # 去重：窗口内同曲直接复用缓存，不占配额
            cached_mp3 = self.fetcher.mp3_path(current_bv, preset)
            if (
                self.guard.is_recent(current_bv)
                and cached_mp3.exists()
                and current_view is None
            ):
                logger.debug("%s 命中同曲去重窗口，直接复用缓存（%s）", LOG_PREFIX, current_bv)
                await self._send_result(
                    event, current_row, "", video_url(current_bv), cached_mp3
                )
                return

            # 最后一次不再判时长：宁可放行一首长的，也不能让用户点歌失败。
            # ⚠️ attempts == 1 时（搜索点歌 / 闸门未开）必须照常判定 ——
            # 否则「换不了歌」的场景下闸门会整个失效。
            enforce = not (attempts > 1 and attempt + 1 >= attempts)
            try:
                async with self.guard.slot(group_id):
                    await asyncio.wait_for(
                        self._process(event, current_row, current_view, preset, gate, enforce),
                        timeout=timeout,
                    )
                return
            except DurationRejected as exc:
                logger.debug(
                    "%s 已跳过《%s》—— %s（第 %d/%d 次）",
                    LOG_PREFIX,
                    (current_row.get("title") or "")[:20],
                    exc.reason,
                    attempt + 1,
                    attempts,
                )
                picked = resample() if (resample is not None and attempt + 1 < attempts) else None
                if not picked:
                    # 换不到歌了（搜索点歌 / 次数用尽 / 榜单为空）→ 明确说一句，绝不静默
                    await event.send(
                        event.plain_result("这首作品暂时点不了（不合时长设置或稿件已失效），换一首试试。")
                    )
                    return
                current_row = picked
                current_view = None
            except QueueFull as exc:
                logger.debug("%s 队列已满：%s", LOG_PREFIX, exc)
                await event.send(event.plain_result("现在排队的点歌太多了，请稍后再试。"))
                return
            except asyncio.TimeoutError:
                await event.send(
                    event.plain_result(f"这首歌处理超时（超过 {timeout} 秒），请再试一次。")
                )
                return
            except BiliRiskError as exc:
                self.guard.trip_circuit(str(exc))
                await event.send(
                    event.plain_result("B站 侧触发了风控，已暂停点歌 120 秒，请稍后再试。")
                )
                return
            except NoAudioTrack as exc:
                await event.send(event.plain_result(f"这首作品拿不到可用音频：{exc}"))
                return
            except AudioDownloadError as exc:
                logger.warning("%s 音频下载失败：%s", LOG_PREFIX, exc)
                await event.send(event.plain_result("音频下载失败了，请稍后再试。"))
                return
            except Exception as exc:  # noqa: BLE001 - 兜底，别让插件崩
                from .core.utils import log_error

                log_error(f"{LOG_PREFIX} 点歌流程异常", f"{type(exc).__name__}: {exc}")
                await event.send(event.plain_result("点歌出错了，管理员可以查看日志了解详情。"))
                return

    async def _process(
        self,
        event: AstrMessageEvent,
        row: dict,
        view: dict | None,
        preset: str,
        gate: DurationGate | None = None,
        enforce_duration: bool = True,
    ) -> None:
        """真正干活的部分：拿时长 → 判闸门 → 拿音轨 → 下载 → 转码 → 发送。

        时长闸门放在 **②**（``view`` 之后、``commit()`` 之前）是刻意的：
        ``view`` 本来就必须走（要拿 ``cid``），响应里白送 ``duration``，
        所以判时长**零额外请求**；而这时还没下载、没扣配额，被跳过的歌
        既不产生流量也不占用户的每日额度。
        """
        started = time.time()
        bv = row.get("bv") or ""
        url = video_url(bv)
        group_id = event.get_group_id() or ""
        user_id = str(event.get_sender_id())

        # ① 稿件信息（顺带拿 cid；搜索结果里已经拿过就直接复用）
        if not view or not view.get("cid"):
            try:
                view = await self.bili.get_view(bv)
            except BiliError as exc:
                if exc.code in DEAD_VIEW_CODES:
                    # 稿件没了（实测 1.7%）→ 记成失效，之后一并排除
                    self.duration.mark_dead(bv)
                    raise DurationRejected(bv, DEAD, "稿件不可见或已失效") from exc
                raise
        cid = view.get("cid")
        if not cid:
            self.duration.mark_dead(bv)
            raise DurationRejected(bv, DEAD, "稿件信息里没有 cid")

        # ② 时长闸门（零额外请求：duration 是 view 白送的）
        seconds = self._remember_duration(bv, view)
        if seconds is None:
            self.duration.mark_dead(bv)
            raise DurationRejected(bv, DEAD, "拿不到作品时长")
        if enforce_duration and gate is not None and not gate.allows(seconds):
            raise DurationRejected(bv, seconds, f"时长 {seconds} 秒超出允许范围（{gate.describe()}）")
        self.duration.flush()

        # ③ 音轨择优
        play = await self.bili.get_playurl(bv, cid)
        dash = play.get("dash") or {}
        want = self._cfg_str("audio_quality", "192k")
        track, actual_quality, degrade = pick_audio_track(dash, want, self.auth.is_vip)
        if degrade:
            logger.warning("%s 音质降级 —— %s（实际 %s）", LOG_PREFIX, degrade, actual_quality)

        # 计数：只在实际开始处理时记
        self.guard.commit(group_id, user_id)

        # ③ 成品缓存直接命中（下载与转码全省）
        mp3_path = self.fetcher.mp3_path(bv, preset)
        if mp3_path.exists():
            self.fetcher.touch(mp3_path)
            logger.debug("%s 命中 mp3 缓存：%s", LOG_PREFIX, mp3_path.name)
            await self._send_result(event, row, view.get("up_name", ""), url, mp3_path)
            self._log_success(group_id, row, started, cached=True)
            return

        # ④ 源文件（可能已有缓存）
        src_path = self.fetcher.src_path(bv, actual_quality)
        if not src_path.exists():
            urls = [track_url(track)]
            backup = track.get("backupUrl") or track.get("backup_url") or []
            urls.extend(backup if isinstance(backup, list) else [])
            await self.fetcher.download(urls, src_path, self.bili.media_headers())
        else:
            self.fetcher.touch(src_path)

        # ⑥ 转码
        final_path, note = await transcode_to_mp3(src_path, preset, mp3_path)
        if note:
            logger.warning("%s %s", LOG_PREFIX, note)

        # ⑦ 发送
        await self._send_result(event, row, view.get("up_name", ""), url, final_path)
        self.guard.mark_sent(bv)
        self._log_success(group_id, row, started, size=track_size(track))

    async def _send_result(
        self,
        event: AstrMessageEvent,
        row: dict,
        up_name: str,
        url: str,
        audio_path,
    ) -> None:
        caption = build_caption(row, up_name, url)
        ok, channel, reason = await send_voice(event, caption, audio_path)
        if not ok:
            logger.warning("%s 语音发送降级到「%s」：%s", LOG_PREFIX, channel, reason)

    def _log_success(self, group_id: str, row: dict, started: float, size: int = 0, cached: bool = False) -> None:
        elapsed = time.time() - started
        logger.info(
            "%s 点歌成功 —— 群 %s，第 %s 名《%s》，耗时 %.1f 秒%s",
            LOG_PREFIX,
            group_id or "私聊",
            row.get("rank", "?"),
            (row.get("title") or "")[:24],
            elapsed,
            "（缓存命中）" if cached else (f"（{humanize_size(size)}）" if size else ""),
        )

    # =============================================================== 文案

    def _help_text(self) -> str:
        return HELP_TEXT

    def _status_text(self) -> str:
        stats = self.rank.stats()
        auth = self.auth.state_snapshot
        pools = stats["pools"]
        pool_text = " · ".join(f"{name} {count}" for name, count in pools.items())
        guard = self.guard.stats()
        preset = self._cfg_str("vocal_preset", "standard")
        quality = self._cfg_str("audio_quality", "192k")

        lines = [
            "哈基米音乐点歌 · 运行状态",
            f"榜单缓存　　{self._count_text(stats['count'])} 条（更新于 {humanize_ago(stats['updated_at'])}）",
            f"风格标签　　{stats['styled_count']} 条 / 重复 BV {stats['duplicate_bv']} 个",
            f"风格池　　　{pool_text}",
            "B站 Cookie　" + self._cookie_text(auth),
            f"音质设置　　下载 {quality} / 发送 {preset}",
            "上游熔断　　"
            + (
                f"{guard['circuit_remaining']} 秒后解除"
                if guard["circuit_remaining"]
                else "正常"
            ),
            f"队列　　　　{guard['waiting']} / {guard['queue_max']}",
            f"今日计数　　群 {guard['group_count']} 个 / 用户 {guard['user_count']} 个",
        ]
        return "\n".join(lines)

    @staticmethod
    def _count_text(count: int) -> str:
        return f"{count:,}"

    @staticmethod
    def _cookie_text(auth) -> str:
        if auth.logged_in:
            return f"已登录（{auth.uname or '未知'}）· {'大会员' if auth.is_vip else '非大会员'}"
        return f"未登录 · {auth.reason or '匿名模式'}"

    # =============================================================== Web API

    def _register_web_apis(self) -> None:
        self._webapi.register(self.context)
