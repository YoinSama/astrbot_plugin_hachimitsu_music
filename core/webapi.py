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

from .constants import (
    DURATION_MAX_DEFAULT,
    DURATION_RESAMPLE_DEFAULT,
    LOG_PREFIX,
    PLUGIN_NAME,
    POOL_MAX_COUNT,
    POOL_WEIGHT_NDIGITS,
    VOCAL_PRESETS,
)
from .rank import resolve_pool_weights
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

    def _random_section(self) -> dict:
        """取 ``random`` 配置分组，顺手补出缺失的分组。"""
        section = self._plugin.config.get("random")
        if not isinstance(section, dict):
            section = {}
            self._plugin.config["random"] = section
        return section

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
            (f"{base}/names/refresh", self.api_names_refresh, ["POST"], "强制重新拉取群名与昵称"),
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
                    "pools": plugin._random_pools(),
                    "pool_weights": plugin._random_manual(),
                    # 后端算好的最终概率表：手动的固定，其余均分剩余。
                    # 前端直接用它显示，避免前后端各算一套而对不上。
                    "pool_plan": plugin._random_plan(),
                    "top_n": plugin._cfg_int("random", "top_n", 1000),
                    "rank_refresh_hours": plugin._cfg_int("random", "rank_refresh_hours", 24),
                    "user_cooldown_seconds": plugin._cfg_int("limit", "user_cooldown_seconds", 30),
                    "group_daily_limit": plugin._cfg_int("limit", "group_daily_limit", 100),
                    "min_seconds": plugin._cfg_int("duration", "min_seconds", 0),
                    "max_seconds": plugin._cfg_int("duration", "max_seconds", DURATION_MAX_DEFAULT),
                    "resample_max": plugin._cfg_int(
                        "duration", "resample_max", DURATION_RESAMPLE_DEFAULT
                    ),
                },
                "guard": guard,
                "duration": {
                    "gate": plugin._duration_gate().describe(),
                    "known": plugin.duration.stats()["known"],
                },
                # 无效输入（填 0 / 非数字）已被自动纠正，前端展示成提示条
                "corrected": plugin.corrections(),
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

        pools = payload.get("pools")
        if pools is not None:
            if not isinstance(pools, list):
                return error_response("随机池必须是数组")
            cleaned: list[str] = []
            for item in pools:
                if not isinstance(item, str):
                    continue
                name = item.strip()
                if name and name not in cleaned:
                    cleaned.append(name)
            if not cleaned:
                return error_response("随机池至少要选一个")
            if len(cleaned) > POOL_MAX_COUNT:
                return error_response(f"随机池最多 {POOL_MAX_COUNT} 个")
            self._random_section()["pools"] = cleaned
            changed.append(f"随机池={len(cleaned)} 个")

        weights = payload.get("pool_weights")
        if weights is not None:
            if not isinstance(weights, dict):
                return error_response("随机池概率必须是对象")
            # 本次也改了池子就按新的算，否则沿用已保存的
            selected = cleaned if pools is not None else plugin._random_pools()
            raw: dict[str, float] = {}
            for key, value in weights.items():
                if not isinstance(key, str) or not key.strip():
                    continue
                try:
                    raw[key.strip()] = round(float(value), POOL_WEIGHT_NDIGITS)
                except (TypeError, ValueError):
                    continue
            # 没勾选的池不保存（它压根不会被抽到，留着只会让人误会）
            kept = {name: value for name, value in raw.items() if name in selected}
            plan = resolve_pool_weights(selected, kept)
            # 落盘 clamp 之后的值：配置文件里的数就是实际生效的数
            final = {name: plan[name] for name in kept if name in plan}
            self._random_section()["pool_weights"] = final
            changed.append(f"随机池概率={len(final)} 项")

        # (所属分组, 下限, 上限, 是否允许 -1 哨兵)
        # v1.2.0：7 个限流项与 duration 的上下限都放开 -1 —— 否则填「不限制」会被这里挡下。
        numeric = {
            "top_n": ("random", 1, 20000, False),
            "rank_refresh_hours": ("random", 0, 720, False),
            "user_cooldown_seconds": ("limit", -1, 86400, True),
            "group_cooldown_seconds": ("limit", -1, 86400, True),
            "user_daily_limit": ("limit", -1, 100000, True),
            "group_daily_limit": ("limit", -1, 100000, True),
            "global_per_minute": ("limit", -1, 100000, True),
            "max_concurrency": ("limit", -1, 32, True),
            "queue_max": ("limit", -1, 1000, True),
            "task_timeout_seconds": ("limit", 5, 600, False),
            "dedup_window_seconds": ("limit", 0, 86400, False),
            "cache_max_mb": ("limit", 64, 102400, False),
            "min_seconds": ("duration", -1, 86400, True),
            "max_seconds": ("duration", -1, 86400, True),
            "resample_max": ("duration", 1, 20, False),
        }
        for key, (section, low, high, allow_sentinel) in numeric.items():
            if key not in payload:
                continue
            try:
                value = int(payload[key])
            except (TypeError, ValueError):
                return error_response(f"{key} 必须是整数")
            if value == -1 and allow_sentinel:
                pass  # 哨兵：该项限制不生效
            elif not (low <= value <= high):
                return error_response(f"{key} 必须在 {low}~{high} 之间" + ("（或填 -1 表示不限制）" if allow_sentinel else ""))
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
        # 无效输入（填 0 / 非数字）已被自动纠正 —— 回给前端展示成提示条
        return json_response({"changed": changed, "corrected": plugin.corrections()})

    # ------------------------------------------------------------- 配额

    async def api_quota_list(self) -> dict:
        from astrbot.api.web import json_response

        snapshot = self._plugin.guard.snapshot()
        await self._attach_names(snapshot)
        return json_response(snapshot)

    async def api_names_refresh(self) -> dict:
        from astrbot.api.web import json_response

        snapshot = self._plugin.guard.snapshot()
        filled = await self._attach_names(snapshot, force=True)
        logger.info("%s WebUI 手动刷新名称，补齐 %d 项", LOG_PREFIX, filled)
        return json_response({"filled": filled})

    async def _attach_names(self, snapshot: dict, *, force: bool = False) -> int:
        """给配额快照里的每个 ID 补上名字（群名 / 昵称），原地修改。

        补不全不影响其它字段：拿不到名字的行由前端退回显示 ID。
        """
        names = getattr(self._plugin, "names", None)
        if names is None:
            for entry in snapshot.get("groups", []):
                entry.setdefault("name", "")
            for entry in snapshot.get("users", []):
                entry.setdefault("name", "")
            return 0

        group_ids = [entry.get("id") for entry in snapshot.get("groups", [])]
        user_ids = [entry.get("id") for entry in snapshot.get("users", [])]
        try:
            filled = await names.fill(
                self._plugin.context,
                group_ids,
                user_ids,
                force=force,
                groups_hint=group_ids,
            )
        except Exception as exc:  # noqa: BLE001 - 名称是展示增强，不该让配额接口失败
            logger.debug("%s 补齐配额名称失败：%s", LOG_PREFIX, exc)
            filled = 0

        for entry in snapshot.get("groups", []):
            entry["name"] = names.group_name(entry.get("id"))
        for entry in snapshot.get("users", []):
            entry["name"] = names.user_name(entry.get("id"))
        return filled

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
