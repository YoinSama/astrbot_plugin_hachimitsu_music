"""ffmpeg 转码：把源音频转成发给 QQ 的紧凑 mono mp3。

🔴 ffmpeg **必须在线程池里调用**。AstrBot 是单事件循环框架，
``subprocess.run`` 阻塞期间 loop 无法调度任何协程 → 机器人对所有群、
所有用户都不响应，多人同时点歌时阻塞还会线性叠加。
官方诊断文档把 ``subprocess.run()`` 与同步网络请求、``time.sleep()``
并列为事件循环卡顿的可疑线索。

转码失败不影响点歌：回退原文件即可（协议端 mp3 / m4a 都能吃，
只是体积大一些）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .constants import LOG_PREFIX, VOCAL_PRESETS
from .utils import logger, run_ffmpeg

_FFMPEG_PATH: str | None = None
_FFMPEG_PROBED = False

# ffmpeg 不一定在 PATH 里（Windows 上尤其常见），额外探几个惯用位置
_CANDIDATES = [
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    "/usr/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
]


def find_ffmpeg() -> str | None:
    """定位 ffmpeg，找不到返回 None（调用方应据此回退原文件）。"""
    global _FFMPEG_PATH, _FFMPEG_PROBED
    if _FFMPEG_PROBED:
        return _FFMPEG_PATH
    _FFMPEG_PROBED = True

    found = shutil.which("ffmpeg")
    if not found:
        for candidate in _CANDIDATES:
            if Path(candidate).exists():
                found = candidate
                break

    _FFMPEG_PATH = found
    if found:
        logger.debug("%s 已定位 ffmpeg：%s", LOG_PREFIX, found)
    else:
        logger.warning(
            "%s 未找到 ffmpeg，音频将以原始格式发送（体积偏大）", LOG_PREFIX
        )
    return _FFMPEG_PATH


def preset_params(preset: str) -> dict:
    """档位 → (采样率, 码率)。未知档位回退「标准」。"""
    return VOCAL_PRESETS.get(preset) or VOCAL_PRESETS["standard"]


async def transcode_to_mp3(
    src: Path, preset: str, out_path: Path, timeout: int = 60
) -> tuple[Path, str]:
    """把 ``src`` 转成 mono mp3。

    返回 ``(最终文件, 说明)``。任何失败都回退 ``src``，绝不抛异常中断点歌。
    """
    if not src.exists():
        return src, "源文件不存在"

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return src, "未安装 ffmpeg，已回退原始格式"

    params = preset_params(preset)
    args = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vn",  # 不要视频轨
        "-ac",
        "1",  # 单声道：同码率下 mono 把全部码率给一个声道，音质不降反升
        "-ar",
        str(params["sample_rate"]),
        "-b:a",
        params["bitrate"],
        "-f",
        "mp3",
        str(out_path),
    ]

    try:
        proc = await run_ffmpeg(args, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 超时/进程异常统一回退
        logger.warning("%s 转码异常（%s），已回退原文件", LOG_PREFIX, exc)
        return src, f"转码异常，已回退原文件（{exc}）"

    if proc.returncode != 0 or not out_path.exists():
        detail = (proc.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else f"退出码 {proc.returncode}"
        logger.warning("%s 转码失败（%s），已回退原文件", LOG_PREFIX, tail)
        return src, "转码失败，已回退原文件"

    return out_path, ""
