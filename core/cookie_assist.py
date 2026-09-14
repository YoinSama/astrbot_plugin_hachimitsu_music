"""管理员协助扫码登录（移植自 media_parser 的 cookie_assist）。

流程：

1. Cookie 校验失效 → 私聊管理员求确认（带冷却，默认 30 分钟一次）
2. 管理员回「确定」→ 请求 ``passport.bilibili.com/.../qrcode/generate``
3. 管理员回「确定」→ 请求 ``passport.bilibili.com/.../qrcode/generate`` 拿到 ``login_url`` + ``qrcode_key``
4. 发两条链接：① 高清二维码（``api.qrserver.com`` 渲染，含 ``qrcode_key``，已告知风险）② 账号密码登录页（``login_url``）
5. 每 2 秒轮询 ``.../qrcode/poll``
   ``code``：0=成功 / 86090=已扫码待确认 / 86101=未扫码 / 86038=过期（TTL 180 秒）
6. 从响应头 ``Set-Cookie`` + 回调 URL 的 query 里提取
   ``SESSDATA`` / ``bili_jct`` / ``DedeUserID`` / ``DedeUserID__ckMd5``
7. 原子落盘 credentials.json

健壮性取舍：
- 一轮未完成时拒绝重复发起
- 图片发送失败回退纯链接
- **管理员不协助则回退无 Cookie 模式继续跑**（功能不中断，只是音质降级）
- 临时二维码文件在 ``finally`` 里删除
"""

from __future__ import annotations

import asyncio
import json
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlparse, quote

from .auth import COOKIE_KEYS, build_cookie_header
from .constants import LOG_PREFIX
from .utils import logger, plugin_data_dir

QRCODE_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QRCODE_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

QR_TTL_SECONDS = 180
POLL_INTERVAL_SECONDS = 2

CONFIRM_WORDS = {"确定", "确认", "好", "ok", "yes", "y"}
CANCEL_WORDS = {"取消", "否", "不用", "no", "n"}


class CookieAssistError(RuntimeError):
    """协助登录流程失败。"""


class CookieAssistant:
    """Cookie 失效时的管理员协助登录。"""

    def __init__(self, auth, client, data_dir: Path | None = None) -> None:
        self._auth = auth
        self._client = client
        self._dir = data_dir or plugin_data_dir()
        self._context = None
        self._pending_umo: str = ""
        self._pending_reason: str = ""
        self._last_request_ts = 0.0
        self._login_in_progress = False
        self._login_success_count = 0  # 累计「新一次扫码成功」次数（WebUI 用于判断本轮是否真的扫到了）
        self._admin_private_origin: str = ""  # 管理员私聊会话（WebUI 主动发起登录请求用）
        self._load_admin_session()             # 重载后从磁盘恢复，避免缓存丢失

    # ------------------------------------------------------------- 状态

    @property
    def login_in_progress(self) -> bool:
        return self._login_in_progress

    @property
    def login_success_count(self) -> int:
        """累计扫码成功次数。WebUI 以「本轮开始后的增量」判定本次扫码是否真的完成。"""
        return self._login_success_count

    @property
    def pending(self) -> bool:
        return bool(self._pending_umo)

    def _is_admin(self, user_id: str) -> bool:
        """管理员判定：容忍平台前缀（如 ``aiocqhttp:2353449879`` 也能匹配纯数字配置）。"""
        target = str(user_id)
        for aid in (self._auth.admin_ids() or []):
            if target == str(aid):
                return True
            # OneBot 适配器常在 sender_id 前加平台前缀，取最后一段再比
            if target.rsplit(":", 1)[-1] == str(aid):
                return True
        return False

    # ------------------------------------------------------------- 管理员会话持久化

    def _admin_session_path(self) -> Path:
        return self._dir / "admin_session.json"

    def _load_admin_session(self) -> None:
        """从磁盘恢复管理员私聊会话与平台，避免插件重载后缓存丢失。"""
        try:
            data = json.loads(self._admin_session_path().read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return
        self._admin_private_origin = str(data.get("origin") or "")
        if self._admin_private_origin:
            logger.debug("%s 已从磁盘恢复管理员会话缓存", LOG_PREFIX)

    def _save_admin_session(self) -> None:
        try:
            self._admin_session_path().write_text(
                json.dumps({"origin": self._admin_private_origin}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:  # noqa: BLE001
            logger.debug("%s 保存管理员会话缓存失败：%s", LOG_PREFIX, exc)

    # ------------------------------------------------------------- 第 1 步：求确认

    def trigger(self, event, reason: str) -> None:
        """Cookie 失效时调用。不阻塞调用方，实际发送放到后台任务。"""
        if not self._auth.assist_enabled():
            logger.debug("%s 未启用管理员协助，跳过", LOG_PREFIX)
            return
        admins = self._auth.admin_ids()
        if not admins:
            logger.debug("%s 未配置 admin_ids，跳过管理员协助", LOG_PREFIX)
            return
        if self._login_in_progress:
            logger.debug("%s 已有一轮扫码登录在进行，跳过重复发起", LOG_PREFIX)
            return

        cooldown = self._auth.assist_cooldown_minutes() * 60
        if cooldown > 0 and (time.time() - self._last_request_ts) < cooldown:
            logger.debug("%s 协助请求仍在冷却期内，跳过", LOG_PREFIX)
            return

        self._last_request_ts = time.time()
        self._pending_reason = reason
        try:
            asyncio.get_running_loop().create_task(self._ask_admins(event, admins, reason))
        except RuntimeError:
            logger.debug("%s 当前没有事件循环，无法发起协助请求", LOG_PREFIX)

    async def _ask_admins(self, event, admins: list[str], reason: str) -> None:
        text = (
            "B站 Cookie 已失效，音频将临时降级为匿名模式（上限 192K）。\n"
            f"原因：{reason}\n"
            "回复「确定」我给你发登录二维码，扫码即可恢复。回复「取消」忽略。"
        )
        # 必须用管理员私聊时真实产生的 unified_msg_origin（见 try_handle 缓存）。
        # 不能手拼「平台:private:QQ号」—— AstrBot 的 MessageType 不认 "private" 这个令牌，
        # 手拼的 UMO 会被 send_message 拒绝（'private' is not a valid MessageType）。
        origin = self._admin_private_origin
        if not origin:
            logger.debug(
                "%s 尚未记录管理员私聊会话，跳过主动协助（让管理员先给机器人发一条私聊）",
                LOG_PREFIX,
            )
            return
        try:
            from astrbot.api.event import MessageChain

            chain = MessageChain().message(text)
            await self._send_to(origin, chain)
            self._pending_umo = origin
            logger.info("%s 已向管理员发起 Cookie 续期请求", LOG_PREFIX)
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s 私聊管理员失败：%s", LOG_PREFIX, exc)

    async def _send_to(self, umo: str, chain) -> None:
        """通过 AstrBot Context 主动发消息（需要 main.py 先注入 context）。"""
        if self._context is None:
            raise CookieAssistError("未注入 AstrBot Context，无法主动发消息")
        await self._context.send_message(umo, chain)

    def bind_context(self, context) -> None:
        """由插件在初始化时注入 AstrBot Context。"""
        self._context = context

    @staticmethod
    def _platform_of(event) -> str:
        try:
            return event.get_platform_id()
        except Exception:  # noqa: BLE001
            return str(event.unified_msg_origin).split(":")[0]

    # ------------------------------------------------------------- 第 2 步：响应确认

    def try_handle(self, event) -> bool:
        """尝试把这条消息当成协助流程的回复。返回 True 表示已消费。"""
        sender = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "")
        origin = str(event.unified_msg_origin or "")

        if self._is_admin(sender):
            # 记住管理员的私聊会话，供 WebUI「向管理员发起登录请求」复用。
            # 与 media_parser 及官方 9.2 一致：直接存 event.unified_msg_origin 原样复用。
            #
            # 🔴 私聊判定必须用 group_id 是否为空（官方 5.1：group_id 私聊则为空），
            # 绝不能匹配 UMO 里的 ":private:" —— AstrBot 的 MessageType 没有 "private"
            # 成员（手拼会报 "'private' is not a valid MessageType"），真实 UMO 里也
            # 不会出现该串，用 ":private:" 判断会让私聊会话永远记录不下来。
            if not group_id and origin and origin != self._admin_private_origin:
                self._admin_private_origin = origin
                self._save_admin_session()
                logger.info("%s 已记录管理员私聊会话（跨重启保留）：%s", LOG_PREFIX, origin)
        if not self._pending_umo:
            return False
        if not self._is_admin(sender):
            return False

        text = (event.message_str or "").strip()
        lowered = text.lower()
        if text in CONFIRM_WORDS or lowered in CONFIRM_WORDS:
            self._pending_umo = ""
            asyncio.get_running_loop().create_task(self.run_login(event))
            return True
        if text in CANCEL_WORDS or lowered in CANCEL_WORDS:
            self._pending_umo = ""
            logger.info("%s 管理员取消了 Cookie 续期请求", LOG_PREFIX)
            asyncio.get_running_loop().create_task(
                event.send(event.plain_result("好的，已取消。将继续以匿名模式运行。"))
            )
            return True
        return False

    # ------------------------------------------------------------- WebUI 主动发起

    async def request_from_webui(self) -> None:
        """WebUI「向管理员发起登录请求」按钮调用：私聊管理员求确认。

        复用 try_handle 缓存的管理员真实私聊会话（``unified_msg_origin``）。
        该 origin 来自管理员实际发来的私聊事件，AstrBot 能直接解析；
        不能手拼「平台:private:QQ号」（MessageType 不认 "private" 令牌）。
        """
        if not self._auth.assist_enabled():
            raise CookieAssistError("未启用管理员协助，无法发起登录请求")
        if self._login_in_progress:
            logger.debug("%s 已有一轮扫码登录在进行，跳过 WebUI 重复发起", LOG_PREFIX)
            return
        if not self._admin_private_origin:
            raise CookieAssistError(
                "尚未记录管理员私聊会话：请先用管理员 QQ 给机器人发一条 1 对 1 私聊消息"
                "（任意内容均可），插件会记住该会话；之后即可从 WebUI 发起登录请求。"
                "该会话已持久化，重启插件后无需重复发送。"
            )

        text = (
            "收到 WebUI 的 B站 登录请求。\n"
            "回复「确定」我给你发登录二维码与账号密码登录链接，其他回复视为取消。"
        )
        try:
            from astrbot.api.event import MessageChain

            chain = MessageChain().message(text)
            await self._send_to(self._admin_private_origin, chain)
            self._pending_umo = self._admin_private_origin
            logger.info("%s WebUI 已向管理员发起 B站 登录请求", LOG_PREFIX)
        except Exception as exc:  # noqa: BLE001
            self._pending_umo = ""
            raise CookieAssistError(f"私聊管理员失败：{exc}")

    # ------------------------------------------------------------- 第 3 步：扫码流程

    async def run_login(self, event) -> None:
        if self._login_in_progress:
            await event.send(
                event.plain_result("已有一轮扫码登录正在进行，请先完成或等待其结束。")
            )
            return

        self._login_in_progress = True
        try:
            login_url, qrcode_key = await self._generate()
            qr_link = (
                "https://api.qrserver.com/v1/create-qr-code/?size=400x400&data="
                + quote(login_url)
            )
            text = (
                "请使用以下任一方式完成 B站 登录（180 秒内有效）：\n"
                f"① 扫码登录（高清二维码）：{qr_link}\n"
                f"② 账号密码登录页：{login_url}\n"
                "登录成功后 Cookie 将自动更新并落盘。"
            )
            await event.send(event.plain_result(text))

            ok, detail = await self._poll(qrcode_key)
            if ok:
                await event.send(
                    event.plain_result(f"B站 登录成功，Cookie 已更新并落盘。{detail}")
                )
            else:
                await event.send(event.plain_result(f"扫码登录未完成：{detail}"))
        except Exception as exc:  # noqa: BLE001 - 不中断插件运行
            logger.warning("%s 扫码登录流程失败：%s", LOG_PREFIX, exc)
            try:
                await event.send(event.plain_result("扫码登录失败，请稍后重试。"))
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._login_in_progress = False

    # ------------------------------------------------------------- 内部

    async def _generate(self) -> tuple[str, str]:
        logger.info("%s 正在申请 B站 登录二维码…", LOG_PREFIX)
        resp = await self._client.raw_get(QRCODE_GENERATE_URL)
        payload = resp.json()
        if payload.get("code") != 0:
            raise CookieAssistError(
                f"申请二维码失败：{payload.get('code')} {payload.get('message')}"
            )
        data = payload.get("data") or {}
        login_url = str(data.get("url") or "").strip()
        qrcode_key = str(data.get("qrcode_key") or "").strip()
        if not login_url or not qrcode_key:
            raise CookieAssistError("申请二维码失败：返回内容为空")
        return login_url, qrcode_key

    async def _poll(self, qrcode_key: str) -> tuple[bool, str]:
        logger.info("%s 正在等待扫码确认（最长 180 秒）…", LOG_PREFIX)
        deadline = time.time() + QR_TTL_SECONDS
        while time.time() < deadline:
            resp = await self._client.raw_get(QRCODE_POLL_URL, {"qrcode_key": qrcode_key})
            payload = resp.json()
            data = payload.get("data") or {}
            code = data.get("code")

            if code == 0:
                credentials = self._extract(resp, data)
                cookie_header = str(credentials.get("cookie_header") or "").strip()
                self._auth.save_credentials(credentials)
                # 把扫码 Cookie 回填进配置，使其成为唯一来源；清空配置即登出
                self._auth.set_config_cookie(cookie_header)
                # 🔴 关键两步，缺一不可（否则配置落盘了，WebUI 仍显示「未登录」）：
                #   ① 先把新 Cookie 喂给 BiliClient —— 不喂的话 refresh() 会拿旧（空）
                #      Cookie 去调 nav，校验结果必然还是「未登录」；
                #   ② 再立刻强制校验一次 —— mark_invalid() 只是把状态标脏
                #      （checked_at=0），它自己不会重新校验；而 WebUI 的
                #      api_status 读的是 state_snapshot 快照，不刷新就一直停在旧值。
                self._client.set_cookie(cookie_header)
                uname = ""
                try:
                    state = await self._auth.refresh(self._client, force=True)
                    uname = state.uname or ""
                    if not state.logged_in:
                        logger.warning(
                            "%s 扫码已成功但 Cookie 校验未通过：%s",
                            LOG_PREFIX,
                            state.reason or "未知原因",
                        )
                except Exception as exc:  # noqa: BLE001 - 校验失败不该让登录流程失败
                    logger.warning("%s 扫码后 Cookie 校验异常（%s）", LOG_PREFIX, exc)
                self._login_success_count += 1
                who = uname or f"UID {credentials.get('DedeUserID') or '未知'}"
                return True, f"（账号 {who}）"
            if code == 86038:
                return False, "二维码已过期，请重新发起"
            # 86090 = 已扫码待确认；86101 = 未扫码 → 继续轮询
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        return False, "二维码已过期，请重新发起"

    @staticmethod
    def _extract(resp, data: dict) -> dict:
        """从响应头 Set-Cookie 与回调 URL 的 query 里提取凭据。"""
        cookies: dict[str, str] = {}

        try:
            set_cookie_headers = resp.headers.get_list("set-cookie")
        except AttributeError:  # pragma: no cover - 兼容不同 httpx 版本
            single = resp.headers.get("set-cookie", "")
            set_cookie_headers = [single] if single else []

        for raw in set_cookie_headers:
            jar = SimpleCookie()
            try:
                jar.load(raw)
            except Exception:  # noqa: BLE001 - 单个 cookie 解析失败不影响整体
                continue
            for key, morsel in jar.items():
                cookies[key] = morsel.value

        callback = str(data.get("url") or "").strip()
        if callback:
            parsed = urlparse(callback)
            for key, values in parse_qs(parsed.query, keep_blank_values=False).items():
                if key in COOKIE_KEYS and key not in cookies and values:
                    value = str(values[0]).strip()
                    if value:
                        cookies[key] = value

        if not cookies.get("SESSDATA"):
            raise CookieAssistError("扫码成功，但响应里没有有效的 Cookie")

        credentials = {key: cookies.get(key, "") for key in COOKIE_KEYS}
        credentials["cookie_header"] = build_cookie_header(credentials)
        credentials["refresh_token"] = str(data.get("refresh_token") or "")
        credentials["login_time"] = int(time.time())
        return credentials
