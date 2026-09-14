"""B站 凭据运行时：Cookie 校验、会员判定、凭据落盘。

Cookie 来源有两个，**配置优先**：

1. `_conf_schema.json` 里的 ``bili.cookie``（用户手填）
2. 管理员扫码登录后落盘的 ``credentials.json``（插件自己维护）

校验方式：一次 ``x/web-interface/nav`` 同时拿到 ``isLogin`` 与 ``vipStatus``。
结果做缓存 —— 有效 300 秒、无效 60 秒，避免每次点歌都多打一次接口。

🔴 会员判定只能用 ``vipStatus == 1``，**不能用 ``vipType``**：
实测有个账号 ``vipType=1`` 但 ``vipStatus=0``，``vipDueDate`` 显示会员早在
2025-10-10 就到期了 —— ``vipType`` 在过期后仍保留历史值。

安全：Cookie 只落盘到 ``data/plugin_data/<plugin>/``，不进日志、不回显。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .constants import LOG_PREFIX
from .utils import logger, plugin_data_dir

# 校验结果缓存时长
VALID_TTL_SECONDS = 300
INVALID_TTL_SECONDS = 60

# 扫码回调里可能带的 Cookie 字段
COOKIE_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")


@dataclass
class AuthState:
    """一次校验的结果快照。"""

    logged_in: bool = False
    is_vip: bool = False
    vip_type: int = 0
    vip_due_ms: int = 0
    uname: str = ""
    source: str = "anonymous"  # config / credentials / anonymous
    reason: str = ""
    checked_at: float = 0.0
    cookie_signature: str = ""

    def as_dict(self) -> dict:
        return {
            "logged_in": self.logged_in,
            "is_vip": self.is_vip,
            "vip_type": self.vip_type,
            "uname": self.uname,
            "source": self.source,
            "reason": self.reason,
            "checked_at": self.checked_at,
        }


def build_cookie_header(credentials: dict) -> str:
    """把字段字典拼成 Cookie 请求头字符串。"""
    parts = [
        f"{key}={credentials[key]}"
        for key in COOKIE_KEYS
        if credentials.get(key)
    ]
    return "; ".join(parts)


def parse_cookie_header(header: str) -> dict:
    """把 Cookie 请求头字符串拆成字段字典。"""
    result: dict[str, str] = {}
    for chunk in str(header or "").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key = key.strip()
        value = value.strip()
        if key:
            result[key] = value
    return result


class AuthRuntime:
    """管理 Cookie 的取用、校验缓存与落盘。"""

    def __init__(self, config, data_dir: Path | None = None) -> None:
        self._config = config
        self._dir = data_dir or plugin_data_dir()
        self._cred_path = self._dir / "credentials.json"
        self._state = AuthState()
        self._runtime_credentials: dict = {}
        self._lock = asyncio.Lock()
        self._load_credentials()
        # 「删配置即登出」：若配置 Cookie 已清空，而扫码缓存原本依附于配置 Cookie，
        # 则在启动时一并清除，避免「删了配置还显示已登录」。
        self._reconcile_config_cookie()

    # ------------------------------------------------------------- 配置

    def _bili_section(self) -> dict:
        value = self._config.get("bili") if hasattr(self._config, "get") else None
        return value if isinstance(value, dict) else {}

    def reload_config(self, config) -> None:
        self._config = config
        self._reconcile_config_cookie()

    def configured_cookie(self) -> str:
        return str(self._bili_section().get("cookie") or "").strip()

    def _reconcile_config_cookie(self) -> None:
        """「删配置即登出」对账：配置 Cookie 为空时，清除扫码缓存，确保清空配置即等同于退出登录。"""
        if self.configured_cookie():
            return
        if self._runtime_credentials:
            logger.info("%s 配置 Cookie 为空，清除扫码缓存以彻底退出登录", LOG_PREFIX)
            self.clear_credentials()

    def set_config_cookie(self, header: str) -> None:
        """把扫码得到的 Cookie 回填进插件配置 ``bili.cookie``（单一来源）并落盘。

        这样无论手动填写还是扫码登录，Cookie 都只存在于配置一处；
        清空配置里的 Cookie 即等同于退出登录。
        """
        if not header:
            return
        bili = dict(self._bili_section())
        bili["cookie"] = header
        try:
            self._config["bili"] = bili
            save = getattr(self._config, "save_config", None)
            if callable(save):
                save()
            logger.info("%s 扫码 Cookie 已回填至配置 bili.cookie", LOG_PREFIX)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 配置 Cookie 回填失败（%s），本次仅内存生效", LOG_PREFIX, exc)

    # ------------------------------------------------------------- 凭据落盘

    def _load_credentials(self) -> None:
        if not self._cred_path.exists():
            return
        try:
            payload = json.loads(self._cred_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("%s 凭据文件读取失败（%s）", LOG_PREFIX, exc)
            return
        if isinstance(payload, dict):
            self._runtime_credentials = payload

    def save_credentials(self, credentials: dict) -> bool:
        """原子落盘凭据。失败只记录，不影响内存生效。"""
        payload = dict(credentials)
        payload.setdefault("login_time", int(time.time()))
        self._runtime_credentials = payload
        tmp = self._cred_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(self._cred_path)
        except OSError as exc:
            logger.warning("%s 凭据落盘失败（%s），本次仅内存生效", LOG_PREFIX, exc)
            return False
        return True

    def clear_credentials(self) -> None:
        self._runtime_credentials = {}
        try:
            self._cred_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("%s 凭据文件删除失败（%s）", LOG_PREFIX, exc)

    def runtime_cookie(self) -> str:
        header = str(self._runtime_credentials.get("cookie_header") or "").strip()
        if header:
            return header
        return build_cookie_header(self._runtime_credentials)

    # ------------------------------------------------------------- 取用

    def cookie_header(self) -> str:
        """当前生效的 Cookie 请求头（配置优先，其次扫码结果）。"""
        return self.configured_cookie() or self.runtime_cookie()

    def cookie_source(self) -> str:
        if self.configured_cookie():
            return "config"
        if self.runtime_cookie():
            return "credentials"
        return "anonymous"

    @property
    def is_vip(self) -> bool:
        return self._state.is_vip

    @property
    def state_snapshot(self) -> AuthState:
        return self._state

    # ------------------------------------------------------------- 校验

    async def refresh(self, client, force: bool = False) -> AuthState:
        """校验一次 Cookie 状态，带缓存。

        ``client`` 需要提供 ``get_nav()``（见 ``core.bili.BiliClient``）。
        """
        cookie = self.cookie_header()
        signature = f"{len(cookie)}:{cookie[:16]}"
        now = time.time()

        ttl = VALID_TTL_SECONDS if self._state.logged_in else INVALID_TTL_SECONDS
        fresh = (now - self._state.checked_at) < ttl
        same = self._state.cookie_signature == signature
        if not force and fresh and same:
            return self._state

        async with self._lock:
            now = time.time()
            if not force and (now - self._state.checked_at) < ttl and self._state.cookie_signature == signature:
                return self._state

            if not cookie:
                self._state = AuthState(
                    logged_in=False,
                    source="anonymous",
                    reason="未配置 Cookie，将以匿名方式请求（音质上限 192K）",
                    checked_at=now,
                    cookie_signature=signature,
                )
                return self._state

            try:
                nav = await client.get_nav()
            except Exception as exc:  # noqa: BLE001 - 校验失败不应中断点歌
                logger.warning("%s Cookie 校验失败（%s），本次按未登录处理", LOG_PREFIX, exc)
                self._state = AuthState(
                    logged_in=False,
                    source=self.cookie_source(),
                    reason=f"Cookie 校验失败：{exc}",
                    checked_at=now,
                    cookie_signature=signature,
                )
                return self._state

            # ⚠️ 未登录时这些字段根本不存在，必须 .get()
            logged_in = nav.get("isLogin") is True
            vip_status = nav.get("vipStatus")
            self._state = AuthState(
                logged_in=logged_in,
                # 只用 vipStatus 判定：实测有账号 vipType=1 但会员已过期（vipStatus=0）
                is_vip=vip_status == 1,
                vip_type=int(nav.get("vipType") or 0),
                vip_due_ms=int(nav.get("vipDueDate") or 0),
                uname=str(nav.get("uname") or ""),
                source=self.cookie_source(),
                reason="" if logged_in else "Cookie 已失效或未登录",
                checked_at=now,
                cookie_signature=signature,
            )
            return self._state

    def mark_invalid(self, reason: str) -> None:
        """外部发现 Cookie 不可用时标记失效，下次强制重新校验。"""
        self._state.logged_in = False
        self._state.is_vip = False
        self._state.reason = reason
        self._state.checked_at = 0.0

    # ------------------------------------------------------------- 管理员协助

    def admin_ids(self) -> list[str]:
        from .utils import parse_id_list

        return parse_id_list(self._bili_section().get("admin_ids"))

    def assist_enabled(self) -> bool:
        return bool(self._bili_section().get("enable_admin_assist", True))

    def assist_cooldown_minutes(self) -> int:
        section = self._bili_section()
        try:
            return max(0, int(section.get("admin_request_cooldown_minutes", 30)))
        except (TypeError, ValueError):
            return 30
