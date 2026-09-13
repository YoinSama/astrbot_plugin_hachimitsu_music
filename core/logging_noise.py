"""消除上游噪音日志。

httpx 库会在 **INFO 级**打印每一条请求，形如：

```text
[Core] [INFO] [httpx._client:1740]: HTTP Request: GET
  https://api.bilibili.com/x/player/wbi/playurl?...&w_rid=xxxx "HTTP/1.1 200 OK"
```

两个问题：**刷屏**，以及把带签名的完整 URL 全抖出来（`upsig` / `deadline` /
`trid` / `buvid` 之类都在里面）。这些日志对排查没有价值 —— 真正需要的进度
由插件自己用中文打出来。

**不动 AstrBot / 协议端任何源码**的做法：给 ``httpx`` 这个模块级 logger
挂一个 ``Filter``，**只**过滤掉我们发起的上游请求。其他插件用 httpx 的日志
照常输出，不受影响。
"""

from __future__ import annotations

import logging
from typing import Iterable

# 我们发起请求的域名。只屏蔽这些，避免误伤别处。
SILENT_HOSTS: tuple[str, ...] = (
    "api.bilibili.com",
    "passport.bilibili.com",
    "bilivideo.com",
    "hdslb.com",
    "notion.so",
    "notion.com",
)


class UpstreamNoiseFilter(logging.Filter):
    """屏蔽针对上游（B站 / Notion）的 httpx 请求日志。"""

    def __init__(self, hosts: Iterable[str] = SILENT_HOSTS) -> None:
        super().__init__()
        self._hosts = tuple(hosts)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 取不到消息就放行，不做假设
            return True
        return not any(host in message for host in self._hosts)


_INSTALLED = False


def install_noise_filter() -> bool:
    """给 httpx / httpcore 挂上过滤器。返回是否是本次新装上的。"""
    global _INSTALLED
    if _INSTALLED:
        return False

    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, UpstreamNoiseFilter) for item in logger.filters):
            logger.addFilter(UpstreamNoiseFilter())

    _INSTALLED = True
    return True
