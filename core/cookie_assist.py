"""管理员协助扫码登录（移植自 media_parser 的 cookie_assist）。

流程：

1. Cookie 校验失效 → 私聊管理员求确认（带冷却，默认 30 分钟一次）
2. 管理员回「确定」→ 请求 ``passport.bilibili.com/.../qrcode/generate``
3. **本地用 qrcode 库渲染 PNG**（登录 token 不交给任何第三方）
4. 发图 + 发链接；图片发送失败则回退纯链接
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
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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

    # ------------------------------------------------------------- 状态

    @property
    def login_in_progress(self) -> bool:
        return self._login_in_progress

    @property
    def pending(self) -> bool:
        return bool(self._pending_umo)

    def _is_admin(self, user_id: str) -> bool:
        return str(user_id) in (self._auth.admin_ids() or [])

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
        platform_id = self._platform_of(event)
        sent = False
        for admin_id in admins:
            umo = f"{platform_id}:private:{admin_id}"
            try:
                from astrbot.api.event import MessageChain

                chain = MessageChain().message(text)
                await self._send_to(umo, chain)
                self._pending_umo = umo
                sent = True
                logger.info("%s 已向管理员 %s 发起 Cookie 续期请求", LOG_PREFIX, admin_id)
            except Exception as exc:  # noqa: BLE001 - 逐个尝试
                logger.debug("%s 私聊管理员 %s 失败：%s", LOG_PREFIX, admin_id, exc)

        if not sent:
            # 私聊通道不可用时，退而求其次：在当前会话里提示管理员
            self._pending_umo = event.unified_msg_origin
            try:
                await event.send(event.plain_result(text))
                logger.info("%s 私聊通道不可用，已在当前会话提示管理员", LOG_PREFIX)
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s 提示管理员也失败：%s", LOG_PREFIX, exc)

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
        if not self._pending_umo:
            return False
        sender = str(event.get_sender_id())
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
            qr_path: Path | None = None
            try:
                qr_path = await asyncio.to_thread(self._render_qr, login_url)
                await self._send_qr(event, qr_path, login_url)
            except Exception as exc:  # noqa: BLE001 - 图片失败就回退链接
                logger.debug("%s 二维码图片发送失败（%s），回退纯链接", LOG_PREFIX, exc)
                await event.send(
                    event.plain_result(
                        "扫码登录（180 秒内有效），请在浏览器打开：\n"
                        f"{login_url}\n或用 B站 App 扫描上面链接生成的二维码。"
                    )
                )
            finally:
                if qr_path is not None:
                    try:
                        qr_path.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.debug("%s 清理临时二维码失败：%s", LOG_PREFIX, exc)

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

    @staticmethod
    def _render_qr(login_url: str) -> Path:
        import qrcode

        path = plugin_data_dir() / f"login_qr_{int(time.time())}.png"
        image = qrcode.make(login_url)
        image.save(path)
        return path

    async def _send_qr(self, event, qr_path: Path, login_url: str) -> None:
        import astrbot.api.message_components as Comp

        chain = [
            Comp.Plain("请用 B站 App 扫码完成登录（180 秒内有效）："),
            Comp.Image.fromFileSystem(str(qr_path)),
            Comp.Plain(f"或直接在浏览器打开：\n{login_url}"),
        ]
        await event.send(event.chain_result(chain))

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
                self._auth.save_credentials(credentials)
                self._auth.mark_invalid("扫码登录成功，下次校验将重新确认状态")
                return True, f"（账号 UID {credentials.get('DedeUserID') or '未知'}）"
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
