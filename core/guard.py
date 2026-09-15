"""防滥用四层闸门：准入 → 限流 → 队列 → 去重。

设计原则（方案 §14）：

- 所有拦截都**明确回复原因和剩余等待秒数**，绝不静默丢弃。
- 额度用尽（冷却中 / 日配额用完 / 队列满）属于**正常产品行为**，
  只记 ``debug`` / ``info`` 日志 + QQ 消息提示，**绝不写 error** ——
  否则会污染日志、误导排查。
- 真正的上游故障（B站 风控）才记 ``warning`` 并触发全局熔断。

队列的「同群公平轮转」用「全局并发上限 + 群内串行」实现：同一个群同时
只跑一个任务，不同群之间可以并发，这样单个群刷屏不会把队列占满。
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from .constants import LOG_PREFIX
from .limits import UNLIMITED, resolve_limit
from .utils import logger, plugin_data_dir, parse_id_list

CIRCUIT_BREAK_SECONDS = 120  # 命中风控后的全局熔断时长（方案 §14）

# 支持哨兵（-1 = 不生效）的限制项及其默认值，见 limits.SENTINEL_KEYS。
# 默认值必须与 _conf_schema.json 保持一致；缺键时用它兜底（升级场景）。
LIMIT_DEFAULTS = {
    "user_cooldown_seconds": 30,
    "group_cooldown_seconds": 10,
    "user_daily_limit": 20,
    "group_daily_limit": 100,
    "global_per_minute": 10,
    "max_concurrency": 2,
    "queue_max": 20,
}


@dataclass
class Decision:
    """闸门裁决结果。"""

    allowed: bool
    reason: str = ""
    wait_seconds: int = 0

    @property
    def message(self) -> str:
        """给用户看的中文提示（含剩余秒数）。"""
        if self.allowed:
            return ""
        if self.wait_seconds > 0:
            return f"{self.reason}，请 {self.wait_seconds} 秒后再试"
        return self.reason


class QueueFull(RuntimeError):
    """等待队列已满。"""


class Guard:
    """限流 / 队列 / 去重 / 熔断 / 配额读写。"""

    def __init__(self, config, data_dir: Path | None = None) -> None:
        self._config = config
        self._path = (data_dir or plugin_data_dir()) / "guard.json"
        self._data: dict = {"date": "", "groups": {}, "users": {}, "global": {"minute": []}}
        self._recent: dict[str, float] = {}
        self._circuit_until = 0.0
        self._circuit_reason = ""
        self._waiting = 0
        self._group_locks: dict[str, asyncio.Lock] = {}
        self._limits: dict[str, int] = {}
        self._corrections: list[dict] = []
        self._semaphore: asyncio.Semaphore | None = None

        self._reload_limits()
        self._load()

    # ------------------------------------------------------------- 配置读取

    def _section(self, name: str) -> dict:
        value = self._config.get(name) if hasattr(self._config, "get") else None
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _int(source: dict, key: str, default: int) -> int:
        try:
            return int(source.get(key, default))
        except (TypeError, ValueError):
            return default

    @property
    def corrections(self) -> list[dict]:
        """启动时/保存后收集到的「无效输入已被纠正」清单，供用户提示使用。"""
        return list(self._corrections)

    def _reload_limits(self) -> None:
        """重算全部哨兵限制项。

        ⚠️ 这里不能用 ``max(1, ...)`` —— 那会把哨兵 ``-1`` 一起夹成 1（最严格，
        恰好与"关闭"相反）。所有取值统一走 ``limits.resolve_limit``。
        """
        limits = self._section("limit")
        self._limits = {}
        self._corrections = []

        for key, default in LIMIT_DEFAULTS.items():
            value, note = resolve_limit(limits.get(key), default)
            self._limits[key] = value
            if note:
                self._corrections.append({"key": f"limit.{key}", "message": note})

        concurrency = self._limits["max_concurrency"]
        # -1 = 真不限并发（不加信号量）；否则正常限流
        self._semaphore = None if concurrency == UNLIMITED else asyncio.Semaphore(concurrency)
        self._queue_max = self._limits["queue_max"]

    def reload_config(self, config) -> None:
        """配置热更新后刷新内部参数。"""
        self._config = config
        self._reload_limits()

    # ------------------------------------------------------------- 持久化

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("%s 配额文件读取失败（%s），已重置", LOG_PREFIX, exc)
            return
        if isinstance(payload, dict):
            self._data = payload
        self._data.setdefault("groups", {})
        self._data.setdefault("users", {})
        self._data.setdefault("global", {"minute": []})

    def _save(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except OSError as exc:
            logger.debug("%s 配额文件写入失败（%s），本次仅内存生效", LOG_PREFIX, exc)

    def _ensure_today(self) -> None:
        """跨天则清零计数。"""
        today = time.strftime("%Y-%m-%d")
        if self._data.get("date") != today:
            self._data["date"] = today
            self._data["groups"] = {}
            self._data["users"] = {}
            self._data["global"] = {"minute": []}

    def _bucket(self, scope: str, key: str) -> dict:
        table = self._data.setdefault(scope, {})
        entry = table.get(key)
        if not isinstance(entry, dict):
            entry = {"day": 0, "last_ts": 0.0}
            table[key] = entry
        entry.setdefault("day", 0)
        entry.setdefault("last_ts", 0.0)
        return entry

    # ------------------------------------------------------------- 熔断

    def trip_circuit(self, reason: str, seconds: int = CIRCUIT_BREAK_SECONDS) -> None:
        """命中 B站 风控时熔断。熔断期间所有点歌直接回复，不发任何请求。"""
        self._circuit_until = time.time() + seconds
        self._circuit_reason = reason
        logger.warning(
            "%s 上游熔断已触发（%s），%d 秒内暂停点歌", LOG_PREFIX, reason, seconds
        )

    def circuit_remaining(self) -> int:
        remaining = self._circuit_until - time.time()
        return max(0, int(remaining)) if remaining > 0 else 0

    @property
    def circuit_reason(self) -> str:
        return self._circuit_reason

    # ------------------------------------------------------------- 准入

    def check_access(self, group_id: str, user_id: str) -> Decision:
        """第一层：启停与黑白名单。"""
        if not self._config.get("enabled", True):
            return Decision(False, "点歌功能已关闭")

        access = self._section("access")
        allowlist = parse_id_list(access.get("group_allowlist"))
        blocklist = parse_id_list(access.get("group_blocklist"))
        user_blocklist = parse_id_list(access.get("user_blocklist"))

        if user_id and user_id in user_blocklist:
            return Decision(False, "你已被禁止使用点歌")
        if group_id:
            if allowlist and group_id not in allowlist:
                return Decision(False, "本群不在允许列表中")
            if group_id in blocklist:
                return Decision(False, "本群已被禁止使用点歌")
        return Decision(True)

    # ------------------------------------------------------------- 限流

    def check_rate(self, group_id: str, user_id: str) -> Decision:
        """第二层：冷却 / 日配额 / 全局速率。"""
        self._ensure_today()
        now = time.time()

        # 全局每分钟
        minute_limit = self._limits["global_per_minute"]
        if minute_limit != UNLIMITED:
            window = [
                ts for ts in (self._data["global"].get("minute") or []) if now - ts < 60
            ]
            self._data["global"]["minute"] = window
            if len(window) >= minute_limit:
                wait = max(1, int(60 - (now - window[0])))
                return Decision(False, "当前点歌人数较多", wait)

        # 用户冷却
        if user_id:
            cooldown = self._limits["user_cooldown_seconds"]
            entry = self._bucket("users", user_id)
            if cooldown != UNLIMITED and entry["last_ts"]:
                elapsed = now - float(entry["last_ts"])
                if elapsed < cooldown:
                    return Decision(False, "你点歌太频繁了", int(cooldown - elapsed) + 1)

        # 群冷却
        if group_id:
            cooldown = self._limits["group_cooldown_seconds"]
            entry = self._bucket("groups", group_id)
            if cooldown != UNLIMITED and entry["last_ts"]:
                elapsed = now - float(entry["last_ts"])
                if elapsed < cooldown:
                    return Decision(False, "本群点歌太频繁了", int(cooldown - elapsed) + 1)

        # 用户日配额
        if user_id:
            limit = self._limits["user_daily_limit"]
            entry = self._bucket("users", user_id)
            if limit != UNLIMITED and entry["day"] >= limit:
                return Decision(False, "你今天的点歌次数已用完")

        # 群日配额
        if group_id:
            limit = self._limits["group_daily_limit"]
            entry = self._bucket("groups", group_id)
            if limit != UNLIMITED and entry["day"] >= limit:
                return Decision(False, "本群今天的点歌次数已用完")

        return Decision(True)

    def commit(self, group_id: str, user_id: str) -> None:
        """任务真正开始执行时计数（被拒绝的请求不计数）。"""
        self._ensure_today()
        now = time.time()
        if user_id:
            entry = self._bucket("users", user_id)
            entry["day"] = int(entry.get("day", 0)) + 1
            entry["last_ts"] = now
        if group_id:
            entry = self._bucket("groups", group_id)
            entry["day"] = int(entry.get("day", 0)) + 1
            entry["last_ts"] = now
        minute = self._data["global"].setdefault("minute", [])
        minute.append(now)
        self._data["global"]["minute"] = [ts for ts in minute if now - ts < 60]
        self._save()

    # ------------------------------------------------------------- 队列

    def checking_queue(self) -> bool:
        if self._queue_max == UNLIMITED:
            return False
        return self._waiting >= self._queue_max

    @asynccontextmanager
    async def slot(self, group_id: str):
        """获取执行槽位：全局并发受限 + 群内串行（实现跨群公平轮转）。"""
        if self.checking_queue():
            raise QueueFull("当前排队人数已达上限")

        self._waiting += 1
        try:
            lock = self._group_locks.setdefault(group_id or "__private__", asyncio.Lock())
            async with lock:
                if self._semaphore is None:
                    yield
                else:
                    async with self._semaphore:
                        yield
        finally:
            self._waiting -= 1
            if group_id and self._waiting <= 0:
                # 空闲时清理群锁，避免群号长期累积
                self._group_locks.clear()

    @property
    def waiting(self) -> int:
        return self._waiting

    @property
    def queue_max(self) -> int:
        return self._queue_max

    # ------------------------------------------------------------- 去重

    def is_recent(self, key: str) -> bool:
        """这个 key 是否还在去重窗口内（此时可以走缓存，不占配额不发请求）。"""
        if not key:
            return False
        window = self._int(self._section("limit"), "dedup_window_seconds", 300)
        if window <= 0:
            return False
        stamp = self._recent.get(key)
        if not stamp:
            return False
        if time.time() - stamp > window:
            self._recent.pop(key, None)
            return False
        return True

    def mark_sent(self, key: str) -> None:
        if not key:
            return
        now = time.time()
        self._recent[key] = now
        window = self._int(self._section("limit"), "dedup_window_seconds", 300)
        if window > 0:
            for old_key in [k for k, ts in self._recent.items() if now - ts > window]:
                self._recent.pop(old_key, None)

    # ------------------------------------------------------------- 配额管理

    def snapshot(self) -> dict:
        """给 WebUI 的配额快照。"""
        self._ensure_today()
        groups = []
        group_limit = self._limits.get("group_daily_limit", 100)
        for key, entry in sorted(self._data.get("groups", {}).items()):
            if not isinstance(entry, dict):
                continue
            groups.append(
                {
                    "id": key,
                    "used": int(entry.get("day", 0)),
                    "limit": group_limit,
                }
            )
        users = []
        user_limit = self._limits.get("user_daily_limit", 20)
        for key, entry in sorted(self._data.get("users", {}).items()):
            if not isinstance(entry, dict):
                continue
            users.append(
                {
                    "id": key,
                    "used": int(entry.get("day", 0)),
                    "limit": user_limit,
                }
            )
        return {
            "date": self._data.get("date", ""),
            "groups": groups,
            "users": users,
            "waiting": self._waiting,
            "queue_max": self._queue_max,
            "circuit_remaining": self.circuit_remaining(),
            "circuit_reason": self._circuit_reason,
        }

    def reset(self, group_ids: list[str] | None = None, user_ids: list[str] | None = None) -> int:
        """重置配额计数。

        ⚠️ 只清计数，**不动熔断状态** —— 熔断反映的是上游风控信号，
        不该被手动抹掉。
        """
        cleared = 0
        for key in group_ids or []:
            if self._data.get("groups", {}).pop(key, None) is not None:
                cleared += 1
        for key in user_ids or []:
            if self._data.get("users", {}).pop(key, None) is not None:
                cleared += 1
        if cleared:
            self._save()
        return cleared

    def reset_all(self) -> int:
        """重置全部配额。"""
        cleared = len(self._data.get("groups", {})) + len(self._data.get("users", {}))
        self._data["groups"] = {}
        self._data["users"] = {}
        self._data["global"] = {"minute": []}
        self._save()
        return cleared

    # ------------------------------------------------------------- 统计

    def stats(self) -> dict:
        self._ensure_today()
        return {
            "date": self._data.get("date", ""),
            "group_count": len(self._data.get("groups", {})),
            "user_count": len(self._data.get("users", {})),
            "waiting": self._waiting,
            "queue_max": self._queue_max,
            "circuit_remaining": self.circuit_remaining(),
        }
