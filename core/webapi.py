"""WebUI 控制台后端接口。

📖 两条官方约定：

- 路由**必须带插件名前缀**（``/{plugin_name}/xxx``），Page 端调用时**不带**前缀。
- 请求/响应一律用 ``astrbot.api.web`` 的 ``request`` / ``json_response`` /
  ``error_response``，不要把 FastAPI / Quart 的原始对象暴露出去。

bridge 返回值规则（前端要按这个写 try/catch）：
``{"status":"ok","data":v}`` → resolve 为 ``v``；普通 JSON → resolve 完整对象；
``error_response`` 或 HTTP 失败 → **reject 成 Error**。
"""

from __future__ import annotations

import asyncio
import base64
import io

from .constants import LOG_PREFIX, PLUGIN_NAME, VOCAL_PRESETS
from .cookie_assist import CookieAssistError
from .utils import humanize_ago, humanize_size, logger

# 可在 WebUI 里直接改的配置项（其余走 _conf_schema.json）
EDITABLE_QUALITY = ["192k", "132k", "64k", "flac"]
EDITABLE_PRESET = list(VOCAL_PRESETS.keys())


class WebAPI:
    """把插件能力暴露成一组 REST 接口。"""

    def __init__(self, plugin) -> None:
        self._plugin = plugin
        self._routes: list[str] = []

    @property
    def route_count(self) -> int:
        return len(self._routes)

    # ------------------------------------------------------------- 注册

    def register(self, context) -> None:
        base = f"/{PLUGIN_NAME}"
        specs = [
            (f"{base}/status", self.api_status, ["GET"], "运行状态快照"),
            (f"{base}/config", self.api_config, ["POST"], "保存常用配置"),
            (f"{base}/quota/list", self.api_quota_list, ["GET"], "列出配额使用情况"),
            (f"{base}/quota/reset", self.api_quota_reset, ["POST"], "重置指定配额"),
            (f"{base}/rank/refresh", self.api_rank_refresh, ["POST"], "手动刷新榜单"),
            (f"{base}/cache/clear", self.api_cache_clear, ["POST"], "清空音频缓存"),
            (f"{base}/bili/login", self.api_bili_login, ["POST"], "发起 B站 扫码登录"),
            (f"{base}/bili/request", self.api_bili_request, ["POST"], "向管理员发起 B站 登录请求"),
        ]
        for route, handler, methods, desc in specs:
            context.register_web_api(route, handler, methods, desc)
            self._routes.append(route)

    # ------------------------------------------------------------- 状态

    async def api_status(self) -> dict:
        from astrbot.api.web import json_response

        plugin = self._plugin
        stats = plugin.rank.stats()
        guard = plugin.guard.stats()
        cache_bytes = plugin.fetcher.size_bytes()
        # 自助刷新：被标脏（mark_invalid → checked_at=0）或 Cookie 变了才会真正请求
        # B站 nav；命中缓存时 refresh() 直接返回，不会额外发网络请求，所以放在这里是安全的。
        # 否则「管理员在私聊里扫码登录成功」后，WebUI 会一直停在旧的「未登录」快照。
        auth = plugin.auth.state_snapshot
        try:
            auth = await plugin.auth.refresh(plugin.bili)
        except Exception as exc:  # noqa: BLE001 - 状态接口不该因为校验异常而挂掉
            logger.debug("%s 状态接口刷新 Cookie 校验失败（%s）", LOG_PREFIX, exc)

        return json_response(
            {
                "rank": {
                    "count": stats["count"],
                    "updated_at": stats["updated_at"],
                    "updated_text": humanize_ago(stats["updated_at"]),
                    "styled_count": stats["styled_count"],
                    "duplicate_bv": stats["duplicate_bv"],
                    "style_total": stats["style_total"],
                    "pools": stats["pools"],
                    "all_pools": plugin.rank.all_pool_sizes(),
                },
                "bili": {
                    "logged_in": auth.logged_in,
                    "is_vip": auth.is_vip,
                    "uname": auth.uname,
                    "source": auth.source,
                    "reason": auth.reason,
                    "login_success_count": plugin.cookie_assistant.login_success_count,
                },
                "quality": {
                    "download": plugin.config.get("audio_quality", "192k"),
                    "preset": plugin.config.get("vocal_preset", "standard"),
                },
                "settings": {
                    "top_n": plugin._cfg_int("random", "top_n", 1000),
                    "top_weight": plugin._cfg_int("random", "top_weight", 50),
                    "style_weight": plugin._cfg_int("random", "style_weight", 10),
                    "rank_refresh_hours": plugin._cfg_int("random", "rank_refresh_hours", 24),
                    "user_cooldown_seconds": plugin._cfg_int("limit", "user_cooldown_seconds", 30),
                    "group_daily_limit": plugin._cfg_int("limit", "group_daily_limit", 100),
                },
                "guard": guard,
                "cache": {
                    "bytes": cache_bytes,
                    "text": humanize_size(cache_bytes),
                    "limit_mb": plugin._cfg_int("limit", "cache_max_mb", 2048),
                },
                "login_in_progress": plugin.cookie_assistant.login_in_progress,
            }
        )

    # ------------------------------------------------------------- 配置

    async def api_config(self) -> dict:
        from astrbot.api.web import error_response, json_response, request

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        plugin = self._plugin
        changed: list[str] = []

        quality = payload.get("audio_quality")
        if quality is not None:
            if quality not in EDITABLE_QUALITY:
                return error_response(f"下载音质只能是 {EDITABLE_QUALITY} 之一")
            plugin.config["audio_quality"] = quality
            changed.append(f"下载音质={quality}")

        preset = payload.get("vocal_preset")
        if preset is not None:
            if preset not in EDITABLE_PRESET:
                return error_response(f"发送档位只能是 {EDITABLE_PRESET} 之一")
            plugin.config["vocal_preset"] = preset
            changed.append(f"发送档位={preset}")

        numeric = {
            "top_n": ("random", 1, 20000),
            "top_weight": ("random", 0, 10000),
            "style_weight": ("random", 0, 10000),
            "rank_refresh_hours": ("random", 0, 720),
            "user_cooldown_seconds": ("limit", 0, 86400),
            "group_cooldown_seconds": ("limit", 0, 86400),
            "user_daily_limit": ("limit", 0, 100000),
            "group_daily_limit": ("limit", 0, 100000),
            "global_per_minute": ("limit", 0, 100000),
            "max_concurrency": ("limit", 1, 32),
            "queue_max": ("limit", 1, 1000),
            "task_timeout_seconds": ("limit", 5, 600),
            "dedup_window_seconds": ("limit", 0, 86400),
            "cache_max_mb": ("limit", 64, 102400),
        }
        for key, (section, low, high) in numeric.items():
            if key not in payload:
                continue
            try:
                value = int(payload[key])
            except (TypeError, ValueError):
                return error_response(f"{key} 必须是整数")
            if not (low <= value <= high):
                return error_response(f"{key} 必须在 {low}~{high} 之间")
            bucket = plugin.config.get(section)
            if not isinstance(bucket, dict):
                bucket = {}
                plugin.config[section] = bucket
            bucket[key] = value
            changed.append(f"{key}={value}")

        if not changed:
            return error_response("没有需要更新的配置项")

        try:
            plugin.config.save_config()
        except Exception as exc:  # noqa: BLE001 - 保存失败要明确告诉前端
            logger.warning("%s 配置保存失败：%s", LOG_PREFIX, exc)
            return error_response(f"配置保存失败：{exc}")

        plugin.guard.reload_config(plugin.config)
        # 配置变更后立即使 Cookie 校验缓存失效，下次校验会重新确认状态
        # （也顺带触发「删配置即登出」对账：配置 Cookie 清空时清掉依附它的扫码缓存）
        plugin.auth.reload_config(plugin.config)
        plugin.auth.mark_invalid("配置已保存，下次校验将重新确认 Cookie 状态")
        logger.info("%s 配置已通过控制台更新 —— %s", LOG_PREFIX, "，".join(changed))
        return json_response({"changed": changed})

    # ------------------------------------------------------------- 配额

    async def api_quota_list(self) -> dict:
        from astrbot.api.web import json_response

        return json_response(self._plugin.guard.snapshot())

    async def api_quota_reset(self) -> dict:
        from astrbot.api.web import error_response, json_response, request

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        guard = self._plugin.guard
        if payload.get("all"):
            cleared = guard.reset_all()
            logger.info("%s 已重置全部配额（%d 项）", LOG_PREFIX, cleared)
            return json_response({"cleared": cleared})

        groups = payload.get("groups") or []
        users = payload.get("users") or []
        if not isinstance(groups, list) or not isinstance(users, list):
            return error_response("groups / users 必须是字符串数组")
        if not groups and not users:
            return error_response("没有指定要重置的对象")

        cleared = guard.reset([str(item) for item in groups], [str(item) for item in users])
        logger.info(
            "%s 已重置配额 —— 群 %d 个 / 用户 %d 个",
            LOG_PREFIX,
            len(groups),
            len(users),
        )
        return json_response({"cleared": cleared})

    # ------------------------------------------------------------- 操作

    async def api_rank_refresh(self) -> dict:
        from astrbot.api.web import error_response, json_response

        plugin = self._plugin
        try:
            count = await plugin.rank.refresh()
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 手动刷新榜单失败：%s", LOG_PREFIX, exc)
            return error_response(f"刷新失败：{exc}")

        logger.info("%s 榜单已手动刷新 —— %s 条", LOG_PREFIX, f"{count:,}")
        return json_response({"count": count, "pools": plugin.rank.pool_sizes()})

    async def api_cache_clear(self) -> dict:
        from astrbot.api.web import json_response

        files, freed = self._plugin.fetcher.clear()
        logger.info(
            "%s 音频缓存已清空 —— %d 个文件，释放 %s",
            LOG_PREFIX,
            files,
            humanize_size(freed),
        )
        return json_response({"files": files, "freed": freed, "text": humanize_size(freed)})

    async def api_bili_login(self) -> dict:
        """发起扫码登录：返回二维码图片与链接，后台轮询直到完成。"""
        from astrbot.api.web import error_response, json_response

        plugin = self._plugin
        assistant = plugin.cookie_assistant
        if assistant.login_in_progress:
            return error_response("已有一轮扫码登录正在进行")

        try:
            login_url, qrcode_key = await assistant._generate()
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 申请登录二维码失败：%s", LOG_PREFIX, exc)
            return error_response(f"申请二维码失败：{exc}")

        try:
            image_base64 = await asyncio.to_thread(self._render_qr_base64, login_url)
        except Exception as exc:  # noqa: BLE001 - 没有 Pillow 时回退纯链接
            logger.debug("%s 本地生成二维码失败（%s），仅返回链接", LOG_PREFIX, exc)
            image_base64 = ""

        # 后台轮询，成功后自动落盘
        asyncio.get_running_loop().create_task(self._background_login(qrcode_key))

        return json_response(
            {
                "login_url": login_url,
                "qrcode_base64": image_base64,
                "expires_in": 180,
            }
        )

    async def api_bili_request(self) -> dict:
        """WebUI「向管理员发起登录请求」：私聊管理员求确认。"""
        from astrbot.api.web import error_response, json_response

        assistant = self._plugin.cookie_assistant
        try:
            await assistant.request_from_webui()
        except CookieAssistError as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 发起 B站 登录请求失败：%s", LOG_PREFIX, exc)
            return error_response(f"发起请求失败：{exc}")
        return json_response({"sent": True})

    @staticmethod
    def _render_qr_base64(login_url: str) -> str:
        import qrcode

        # 中等纠错 + 更大模块，提升手机扫码识别率（控制台内联二维码）
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=12,
            border=2,
        )
        qr.add_data(login_url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    async def _background_login(self, qrcode_key: str) -> None:
        assistant = self._plugin.cookie_assistant
        if assistant.login_in_progress:
            return
        assistant._login_in_progress = True
        try:
            ok, detail = await assistant._poll(qrcode_key)
            if ok:
                logger.info("%s 控制台发起的扫码登录成功 %s", LOG_PREFIX, detail)
                # 喂 Cookie + 强制校验已在 _poll 内完成，这里只做收尾（同步客户端 + 打日志），
                # 不再重复 refresh，省掉一次多余的 nav 请求。
                await self._plugin._apply_cookie(self._plugin.auth.state_snapshot)
            else:
                logger.info("%s 控制台发起的扫码登录未完成：%s", LOG_PREFIX, detail)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 控制台扫码登录轮询失败：%s", LOG_PREFIX, exc)
        finally:
            assistant._login_in_progress = False
