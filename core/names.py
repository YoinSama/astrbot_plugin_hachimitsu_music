"""群名 / QQ 昵称的记录与查询。

设计要点
--------

1. **被动优先**：点歌记账时就能拿到昵称（``event.get_sender_name()``），
   零请求、零延迟，先记下来。
2. **按需补齐**：群名只能主动查（OneBot 消息里通常不带 ``group_name``），
   走 ``get_group_info``；昵称查不到时，用「这个人最近在哪个群点过歌」
   走 ``get_group_member_info``。
3. **一切失败都要静默**：协议端不在、被风控、群已退 —— 都不该让 WebUI
   报错或变慢，取不到名字就退回显示 ID。
4. **日志只写 debug**：这类缺失是常态，不是异常。

全部函数都不依赖 AstrBot 的任何 import，方便离线单测。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from .constants import LOG_PREFIX
from .utils import logger

# 名字有效期：过期才会重新查（群改名 / 换名片不会立刻生效，但也没必要高频校准）
NAME_TTL = 6 * 3600
# 查失败后多久内不再重试，避免每次开页面都去打扰协议端
FAIL_TTL = 3600
# 单次补齐的上限：剩下的下一轮再来，避免打开页面瞬间涌出一堆 API
MAX_PER_ROUND = 15
# 单个 API 调用的超时（秒）
CALL_TIMEOUT = 5.0


def _clean(value) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return "" if text.lower() in ("unknown", "null", "n/a", "nan") else text


class NameBook:
    """群名 / 昵称的持久化字典。"""

    FILE_NAME = "names.json"

    def __init__(self, data_dir: Path | None = None) -> None:
        from .utils import plugin_data_dir

        self._path = (data_dir or plugin_data_dir()) / self.FILE_NAME
        # gid -> {"name": str, "ts": float}
        self.groups: dict[str, dict] = {}
        # uid -> {"name": str, "ts": float}
        self.users: dict[str, dict] = {}
        # uid -> gid：最近一次点歌发生在哪个群（用来补昵称）
        self.pairs: dict[str, str] = {}
        # "g:123" / "u:456" -> 失败时的时间戳
        self._failed: dict[str, float] = {}
        self._dirty = False
        self._load()

    # ---------------------------------------------------------------- 读写

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 - 缓存坏了不该影响插件启动
            logger.debug("%s 名称缓存读取失败（%s），按空处理", LOG_PREFIX, exc)
            return
        if not isinstance(raw, dict):
            return
        for key, target in (("groups", self.groups), ("users", self.users)):
            value = raw.get(key)
            if isinstance(value, dict):
                target.update({str(k): v for k, v in value.items() if isinstance(v, dict)})
        pairs = raw.get("pairs")
        if isinstance(pairs, dict):
            self.pairs.update({str(k): str(v) for k, v in pairs.items()})
        failed = raw.get("failed")
        if isinstance(failed, dict):
            self._failed.update({str(k): float(v) for k, v in failed.items() if _is_num(v)})

    def save(self) -> None:
        if not self._dirty:
            return
        payload = {
            "groups": self.groups,
            "users": self.users,
            "pairs": self.pairs,
            "failed": self._failed,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._dirty = False
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s 名称缓存写入失败：%s", LOG_PREFIX, exc)

    # ---------------------------------------------------------------- 记录

    def remember_group(self, group_id, name) -> None:
        if not group_id or not _clean(name):
            return
        gid = str(group_id)
        entry = self.groups.get(gid)
        if entry and entry.get("name") == name:
            return
        self.groups[gid] = {"name": name, "ts": time.time()}
        self._failed.pop(f"g:{gid}", None)
        self._dirty = True
        self.save()

    def remember_user(self, user_id, name, group_id=None) -> None:
        uid = str(user_id) if user_id else ""
        if group_id:
            self.pairs[uid] = str(group_id)
            self._dirty = True
        if not uid or not _clean(name):
            self.save()
            return
        entry = self.users.get(uid)
        if entry and entry.get("name") == name:
            self.save()
            return
        self.users[uid] = {"name": name, "ts": time.time()}
        self._failed.pop(f"u:{uid}", None)
        self._dirty = True
        self.save()

    # ---------------------------------------------------------------- 查询

    def group_name(self, group_id) -> str:
        entry = self.groups.get(str(group_id)) if group_id else None
        return entry.get("name", "") if isinstance(entry, dict) else ""

    def user_name(self, user_id) -> str:
        entry = self.users.get(str(user_id)) if user_id else None
        return entry.get("name", "") if isinstance(entry, dict) else ""

    def group_of(self, user_id) -> str:
        return self.pairs.get(str(user_id), "") if user_id else ""

    def missing(self, group_ids, user_ids) -> tuple[list[str], list[str]]:
        """挑出「还没有名字且最近没失败过」的 ID。"""
        now = time.time()
        out_g, out_u = [], []
        for gid in group_ids:
            key = str(gid)
            if self.group_name(key):
                continue
            if now - self._failed.get(f"g:{key}", 0) < FAIL_TTL:
                continue
            out_g.append(key)
        for uid in user_ids:
            key = str(uid)
            if self.user_name(key):
                continue
            if now - self._failed.get(f"u:{key}", 0) < FAIL_TTL:
                continue
            out_u.append(key)
        return out_g, out_u

    # ---------------------------------------------------------------- 补齐

    async def fill(
        self,
        context,
        group_ids,
        user_ids,
        *,
        force: bool = False,
        groups_hint: list[str] | None = None,
    ) -> int:
        """补齐缺名字的 ID，返回本次成功补到的数量。

        ``groups_hint``：当某个用户没记过所在群（老数据常见）时，用这些群
        依次试 ``get_group_member_info``，命中就记下昵称。
        """
        """补齐缺名字的 ID，返回本次成功补到的数量。

        ``context`` 是 AstrBot 的 ``Context``；拿不到协议端 bot 时安静返回 0。
        """
        if force:
            self._failed.clear()
        need_g, need_u = self.missing(group_ids, user_ids)
        if not (need_g or need_u):
            return 0

        need_g = need_g[:MAX_PER_ROUND]
        need_u = need_u[:MAX_PER_ROUND]
        bot = _find_bot(context)
        if bot is None:
            # 协议端没连上，别白试；标脏等下一轮
            self._mark_failed(need_g, need_u)
            return 0

        filled = 0
        for gid in need_g:
            try:
                info = await asyncio.wait_for(
                    bot.call_action("get_group_info", group_id=_as_int(gid)),
                    timeout=CALL_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s 查群名失败 %s：%s", LOG_PREFIX, gid, exc)
                self._failed[f"g:{gid}"] = time.time()
                continue
            name = _clean(info.get("group_name")) if isinstance(info, dict) else ""
            if name:
                self.remember_group(gid, name)
                filled += 1
            else:
                self._failed[f"g:{gid}"] = time.time()

        hints = [str(g) for g in (groups_hint or []) if g][:3]
        known = [str(g) for g in group_ids if g][:3]

        for uid in need_u:
            gid = self.group_of(uid)
            # 没记过所在群（老数据）时，拿已知的群依次试
            candidates = [gid] if gid else list(dict.fromkeys(hints + known))
            name = ""
            used_gid = ""
            for cand in candidates:
                try:
                    member = await asyncio.wait_for(
                        bot.call_action(
                            "get_group_member_info",
                            group_id=_as_int(cand),
                            user_id=_as_int(uid),
                        ),
                        timeout=CALL_TIMEOUT,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("%s 查昵称失败 %s@%s：%s", LOG_PREFIX, uid, cand, exc)
                    continue
                if isinstance(member, dict):
                    name = _clean(member.get("card")) or _clean(member.get("nickname"))
                if name:
                    used_gid = cand
                    break
            if name:
                self.remember_user(uid, name, used_gid or gid)
                filled += 1
            else:
                self._failed[f"u:{uid}"] = time.time()

        self.save()
        if filled:
            logger.info("%s 配额名称补齐 %d 项", LOG_PREFIX, filled)
        return filled

    def _mark_failed(self, group_ids, user_ids) -> None:
        now = time.time()
        for gid in group_ids:
            self._failed[f"g:{gid}"] = now
        for uid in user_ids:
            self._failed[f"u:{uid}"] = now
        self._dirty = True


# ---------------------------------------------------------------- 辅助


def _is_num(value) -> bool:
    return isinstance(value, (int, float))


def _as_int(value) -> int:
    text = str(value)
    return int(text) if text.isdigit() else 0


def _find_bot(context):
    """从 AstrBot 的平台实例里找出 aiocqhttp 的 bot。

    没有平台实例（协议端没连上）时返回 None，由调用方安静处理。
    """
    try:
        manager = getattr(context, "platform_manager", None)
        instances = manager.get_insts() if manager else []
    except Exception:  # noqa: BLE001
        return None
    for inst in instances or []:
        bot = getattr(inst, "bot", None)
        if bot is not None and hasattr(bot, "call_action"):
            return bot
    return None
