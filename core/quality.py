"""音轨择优：按目标档位从 DASH 响应里挑一条音频轨，并处理会员门控与降级。

两个实测坑都发生在这里，而且后果是「点歌直接失败」而不是「优雅降级」——
关键在于异常的位置：**择优在降级之前**，择优里抛异常，降级根本没机会执行。

1. 未登录时 nav 不返回 ``vipStatus`` → 直接索引会 KeyError（见 auth.py）
2. ``dash.dolby`` 对象存在但 ``audio`` 为 ``None``（实测 4/4 首如此）
   → 取值必须 ``(dash.get("dolby") or {}).get("audio") or []``，少一个 ``or []``
     就会在 ``max()`` 上炸 TypeError

所以除了逐个 ``.get()`` 之外，整个择优还包了一层 try/except 兜底：即使 B站
将来再改响应形状，最差也只是音质降级，而不是点歌失败。
"""

from __future__ import annotations

from typing import Any

from .constants import AUDIO_TRACK_ORDER, AUDIO_TRACK_QUALITY, LOG_PREFIX
from .utils import logger


class NoAudioTrack(RuntimeError):
    """该稿件没有任何可用音频轨。"""


def quality_of(track: dict) -> str:
    """音轨的档位名。

    遇到没见过的 quality id 时带上 id 一起返回，这样档位名仍然唯一，
    缓存文件名不会互相覆盖。
    """
    name = AUDIO_TRACK_QUALITY.get(track.get("id"))
    if name:
        return name
    track_id = track.get("id")
    return f"未知({track_id})" if track_id else "未知"


def track_url(track: dict) -> str:
    """音轨的播放地址（DASH 用 baseUrl，旧版可能是 base_url）。"""
    return str(track.get("baseUrl") or track.get("base_url") or "")


def track_size(track: dict) -> int:
    """音轨字节数，拿不到时返回 0。"""
    try:
        return int(track.get("size") or 0)
    except (TypeError, ValueError):
        return 0


def _normal_map(dash: dict) -> dict[str, dict]:
    """把 ``dash.audio`` 整理成 ``{档位名: 音轨}``。"""
    result: dict[str, dict] = {}
    for item in dash.get("audio") or []:
        if not isinstance(item, dict):
            continue
        name = AUDIO_TRACK_QUALITY.get(item.get("id"))
        if name in ("192k", "132k", "64k"):
            result.setdefault(name, item)
    return result


def _best_normal(dash: dict, want: str) -> dict | None:
    """按 ``want`` 的降级顺序找第一条存在的普通音轨。"""
    tracks = _normal_map(dash)
    for candidate in AUDIO_TRACK_ORDER.get(want, ["192k", "132k", "64k"]):
        if candidate in tracks:
            return tracks[candidate]

    # 兜底：目标档位一条都不匹配时，从 **全部** 带可用地址的音轨里取带宽最高的。
    # 这一步要覆盖「没见过的 quality id」——B站 将来若新增档位，最差也只是
    # 音质档位名显示成「未知(xxx)」，而不该直接失败。
    fallback = [
        item
        for item in (dash.get("audio") or [])
        if isinstance(item, dict) and track_url(item)
    ]
    if not fallback:
        return None
    return max(fallback, key=lambda item: item.get("bandwidth") or 0)


def _flac_track(dash: dict) -> dict | None:
    """Hi-Res 无损轨（需大会员且该稿件有）。

    除了形状检查，还要求它带可用地址 —— 空壳对象（有键没 URL）不能当有效轨，
    否则会把「选中了」变成「下载失败」。
    """
    flac = dash.get("flac")
    if not isinstance(flac, dict):
        return None
    audio = flac.get("audio")
    if not isinstance(audio, dict):
        return None
    return audio if track_url(audio) else None


def _dolby_track(dash: dict) -> dict | None:
    """杜比全景声轨。

    🔴 ``dash["dolby"]`` 可能是个 ``{"type": 0, "audio": None}`` 的对象 ——
    对象存在但 audio 是 None，这是最容易漏掉的一种形状（实测匿名与登录状态
    下都是如此）。另外 ``dolby["type"]`` 为 0 时即使 audio 有对象也不代表可用，
    所以这里同样要求 URL 非空。
    """
    dolby = dash.get("dolby")
    if not isinstance(dolby, dict):
        return None
    audio = dolby.get("audio")
    candidates: list[dict] = []
    if isinstance(audio, dict):
        candidates = [audio]
    elif isinstance(audio, list):
        candidates = [item for item in audio if isinstance(item, dict)]
    candidates = [item for item in candidates if track_url(item)]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.get("bandwidth") or 0)


def _select(dash: dict, want: str, is_vip: bool) -> tuple[dict, str, str]:
    """真正的择优逻辑。返回 ``(音轨, 实际档位, 降级原因)``。"""
    if want not in AUDIO_TRACK_ORDER:
        want = "192k"

    if want == "flac":
        if not is_vip:
            track = _best_normal(dash, "192k")
            if track:
                return track, quality_of(track), "未检测到大会员，已从 Hi-Res 无损退回"
        else:
            flac = _flac_track(dash)
            if flac:
                return flac, "flac", ""
            dolby = _dolby_track(dash)
            if dolby:
                return dolby, "dolby", "该稿件没有 Hi-Res 无损音轨，已退回杜比全景声"
            track = _best_normal(dash, "192k")
            if track:
                return track, quality_of(track), "该稿件既无 Hi-Res 也无杜比音轨，已退回"
    else:
        track = _best_normal(dash, want)
        if track:
            actual = quality_of(track)
            reason = "" if actual == want else f"该稿件没有 {want} 音轨，已退回 {actual}"
            return track, actual, reason

    raise NoAudioTrack("该稿件没有任何可用音频轨")


def pick_audio_track(
    dash: dict, want: str = "192k", is_vip: bool = False
) -> tuple[dict, str, str]:
    """择优入口。

    返回 ``(音轨, 实际档位, 降级原因)``。降级原因非空时说明发生了降级
    （应记 warning 日志并在「状态」里显示），但**绝不因此中断点歌**。
    """
    if not isinstance(dash, dict):
        raise NoAudioTrack("DASH 响应结构异常")

    try:
        return _select(dash, want, is_vip)
    except NoAudioTrack:
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底：宁可降音质也不能让点歌失败
        logger.warning(
            "%s 音轨择优异常（%s），已回退到带宽最高的普通音轨", LOG_PREFIX, exc
        )
        tracks = sorted(
            (item for item in (dash.get("audio") or []) if isinstance(item, dict)),
            key=lambda item: item.get("bandwidth") or 0,
            reverse=True,
        )
        if not tracks:
            raise NoAudioTrack("该稿件没有任何可用音频轨") from exc
        return tracks[0], quality_of(tracks[0]), f"择优异常，已回退（{exc}）"
