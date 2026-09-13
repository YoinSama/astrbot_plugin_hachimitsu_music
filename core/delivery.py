"""投递层：把音频作为语音消息发出去，带三级降级链。

三条硬经验（来自生产插件 qqmusic 的实战，我们照做）：

1. **不用 ``Comp.Record`` 做主通道** —— AstrBot 的
   ``Record.convert_to_file_path()`` 内部是 ``to_path(target_format="wav")``，
   会把音频强制转成 WAV。133 秒的歌 → 23.4MB → base64 后 31MB → WebSocket 超时。
   所以主通道走底层 ``call_action`` 直发 record 消息段。

2. **用 ``base64://`` 而不是 ``file://``** —— 跨容器或跨机时，协议端看不到
   AstrBot 这边的文件路径（``realpath ENOENT``）。base64 内联彻底绕开，
   AstrBot 与协议端不必同机。

3. **不自己造 silk** —— 协议端对 silk 输入按魔数透传，非标准码流会导致手机
   播不出来、时长被钳到 1 秒。给标准 mp3 就行，协议端自带 ffmpeg 会转码。

降级链：base64 直发 → ``Comp.Record`` 组件（会强转 WAV，仅兜底）→ 仅发文字。
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from .constants import LOG_PREFIX
from .utils import logger

# base64 内联直发的体积上限。读盘 + 编码（体积 +33%）都在内存里发生。
# 我们的成品 mp3 只有 1~3MB，这个值只是防御异常情况。
BASE64_INLINE_MAX_BYTES = 64 * 1024 * 1024


class DeliveryError(RuntimeError):
    """投递失败。"""


def _file_to_base64(path: Path) -> str:
    size = path.stat().st_size
    if size > BASE64_INLINE_MAX_BYTES:
        raise DeliveryError(
            f"文件 {size / 1024 / 1024:.0f}MB 超过 base64 直发上限"
            f"（{BASE64_INLINE_MAX_BYTES // 1024 // 1024}MB）"
        )
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode("ascii")


def _get_bot(event):
    """拿到 aiocqhttp 的 bot 实例。"""
    bot = getattr(event, "bot", None)
    if bot is None:
        bot = getattr(getattr(event, "platform", None), "bot", None)
    if bot is None:
        raise DeliveryError("无法获取 aiocqhttp bot 实例")
    return bot


def is_aiocqhttp(event) -> bool:
    try:
        return event.get_platform_name() == "aiocqhttp"
    except Exception:  # noqa: BLE001
        return False


async def _send_base64_record(event, text: str, audio_path: Path) -> None:
    """主通道：**先发文本、再发语音（两条独立消息）**。

    ⚠️ 不要把文本段和 record 段塞进同一条消息链 —— 实测这样发出去**只会收到语音**，
    文本节点会被协议端丢掉。原因：语音在 NTQQ 侧走的是 Highway 独立上传通道，
    构造的是专门的语音消息元素（``commonElem { serviceType: 48, businessType: 22 }``），
    和普通文本段放在一起时文本不生效。

    所以这里显式拆成两条：第一条是「标题 / 作者 / 视频链接」，第二条才是语音。
    文本发送失败**不会**阻断语音 —— 语音才是主体。
    """
    bot = _get_bot(event)
    group_id = event.get_group_id()
    action = "send_group_msg" if group_id else "send_private_msg"
    target = (
        {"group_id": int(group_id)}
        if group_id
        else {"user_id": int(event.get_sender_id())}
    )

    # ① 文本节点
    if text:
        try:
            await bot.call_action(
                action, message=[{"type": "text", "data": {"text": text}}], **target
            )
        except Exception as exc:  # noqa: BLE001 - 文本失败不影响语音
            logger.warning("%s 文本节点发送失败（%s），继续发送语音", LOG_PREFIX, exc)

    # ② 语音节点
    payload = await asyncio.to_thread(_file_to_base64, audio_path)
    await bot.call_action(
        action,
        message=[{"type": "record", "data": {"file": f"base64://{payload}"}}],
        **target,
    )


async def _send_record_component(event, text: str, audio_path: Path) -> None:
    """兜底通道：标准 Record 组件（AstrBot 会强制转 WAV，载荷会大很多）。

    同样拆成两条发送，理由与主通道一致。
    """
    import astrbot.api.message_components as Comp

    if text:
        try:
            await event.send(event.chain_result([Comp.Plain(text)]))
        except Exception as exc:  # noqa: BLE001 - 文本失败不影响语音
            logger.warning("%s 文本节点发送失败（%s），继续发送语音", LOG_PREFIX, exc)

    await event.send(
        event.chain_result([Comp.Record(file=str(audio_path), url=str(audio_path))])
    )


async def send_voice(
    event, text: str, audio_path: Path
) -> tuple[bool, str, str]:
    """按降级链把音频发出去。

    返回 ``(是否成功, 使用的通道, 失败原因)``。
    最后一级是仅发文字 —— 即便音频完全发不出去，用户至少还能拿到链接。
    """
    if audio_path is None or not Path(audio_path).exists():
        await event.send(event.plain_result(text))
        return False, "仅文字", "音频文件不存在"

    errors: list[str] = []

    # ① base64 直发（仅 aiocqhttp 支持）
    if is_aiocqhttp(event):
        try:
            await _send_base64_record(event, text, Path(audio_path))
            return True, "base64 直发", ""
        except Exception as exc:  # noqa: BLE001 - 逐级降级
            errors.append(f"base64 直发失败（{type(exc).__name__}: {exc}）")
            logger.warning(
                "%s base64 直发语音失败（%s），尝试 Record 组件兜底",
                LOG_PREFIX,
                exc,
            )
    else:
        errors.append(f"平台 {event.get_platform_name()} 不支持 base64 直发")

    # ② 标准 Record 组件
    try:
        await _send_record_component(event, text, Path(audio_path))
        return True, "Record 组件", ""
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Record 组件失败（{type(exc).__name__}: {exc}）")
        logger.warning("%s Record 组件发送失败（%s），降级为仅发文字", LOG_PREFIX, exc)

    # ③ 仅文字
    try:
        await event.send(event.plain_result(text))
        return False, "仅文字", "；".join(errors)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"文字也发送失败（{type(exc).__name__}: {exc}）")

    return False, "全部失败", "；".join(errors)


def build_caption(row: dict, up_name: str, url: str) -> str:
    """按方案 §1 拼消息链的文本部分：标题 / 作者 / 链接。"""
    title = (row.get("title") or "").strip() or "未知作品"
    lines = [title]
    if up_name:
        lines.append(f"作者：{up_name}")
    if url:
        lines.append(url)
    return "\n".join(lines)
