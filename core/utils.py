"""通用工具：日志、ffmpeg 线程池调用、时间与体积格式化。"""

from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import datetime
from pathlib import Path

try:  # AstrBot 运行时走官方 logger；脱离 AstrBot 跑验证脚本时退化为标准 logging。
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 仅在本地独立验证时命中
    import logging

    logger = logging.getLogger("astrbot_plugin_hachimitsu_music")


def plugin_data_dir() -> Path:
    """插件数据目录：``data/plugin_data/<plugin_name>/``。

    官方要求持久化数据写 ``data/`` 而不是插件自身目录 —— 后者在插件
    更新或重装时会被覆盖。
    """
    from .constants import PLUGIN_NAME

    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = Path(get_astrbot_data_path())
    except Exception:  # noqa: BLE001 - 脱离 AstrBot 独立运行时退回工作目录
        base = Path.cwd() / "data"
    path = base / "plugin_data" / PLUGIN_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_error(scene: str, detail: str) -> None:
    """按方案 §12.3 的模板输出 error 日志。

    error 级别只留给「真正需要人工关注的失败」。额度用尽、冷却等
    属于正常产品行为，请用 debug/info，不要走这里，否则会污染日志、误导排查。
    """
    from .constants import ISSUE_URL, PLUGIN_VERSION

    now = datetime.now()
    logger.error(
        "%s：%s\n"
        "　报错时间：%s（本机时区）\n"
        "\u3000如何看日志：AstrBot WebUI \u2192 数据与日志 \u2192 日志（实时日志），或直接看启动控制台/标准输出\n"
        "\u3000文件日志：AstrBot v4 默认不写日志文件（cmd_config.json 的 log_file_enable=false）；\n"
        "\u3000\u3000开启后才会写入 log_file_path（默认 data/logs/astrbot.log），开启前该文件不存在/不更新\n"
        "　Docker 部署可改用：docker logs <容器名> --since %s\n"
        "\u3000若开启文件日志且遇卡顿/CPU 异常，另附 data/logs/event_loop_watchdog.log\n"
        "　插件版本：%s\n"
        "　如需反馈，请到 %s 提交 issue，并附上以上报错信息与对应时段的日志。",
        scene,
        detail,
        now.strftime("%Y-%m-%d %H:%M:%S"),
        now.strftime("%Y-%m-%dT%H:%M:%S"),
        PLUGIN_VERSION,
        ISSUE_URL,
    )


async def run_ffmpeg(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """在线程池里执行 ffmpeg。

    AstrBot 是单事件循环框架，直接调用 ``subprocess.run`` 会阻塞整个事件循环 ——
    阻塞期间机器人对所有群、所有用户都不响应，多人同时点歌还会线性叠加。
    官方诊断文档把 ``subprocess.run()`` 与同步网络请求、``time.sleep()``
    并列为事件循环卡顿的可疑线索，所以这里统一走 ``asyncio.to_thread``。
    """
    return await asyncio.to_thread(
        subprocess.run, args, capture_output=True, text=True, timeout=timeout
    )


def humanize_ago(timestamp: float | None) -> str:
    """把时间戳变成「2 小时前」这类人读文本。"""
    if not timestamp:
        return "从未"
    delta = time.time() - timestamp
    if delta < 0:
        return "刚刚"
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // 86400)} 天前"


def humanize_size(num_bytes: float) -> str:
    """把字节数变成人类可读体积。"""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} GB"


def parse_id_list(raw) -> list[str]:
    """把配置里的列表/逗号串统一成干净的字符串 ID 列表。

    配置项支持两种写法：列表（WebUI 的 list 类型）和手输的逗号/换行分隔串。
    """
    if raw is None:
        return []
    items: list[str] = []
    if isinstance(raw, (list, tuple, set)):
        for entry in raw:
            items.extend(str(entry).replace("，", ",").split(","))
    else:
        items.extend(str(raw).replace("，", ",").split(","))
    result: list[str] = []
    for item in items:
        cleaned = item.strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result
