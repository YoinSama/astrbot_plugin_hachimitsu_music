"""音频下载与 LRU 缓存。

- **Range 续传**：中断后保留 ``.part`` 文件，下次带着 ``Range`` 头接着下。
  实测 B站 CDN 支持 ``206 Partial Content``。
- **LRU 缓存**：按目录总大小淘汰最久未使用的文件（默认上限 2GB）。
  缓存命中即零请求、零转码，这也是最有效的风控手段 —— 不发请求就不会被风控。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from .constants import LOG_PREFIX
from .utils import logger, plugin_data_dir


class AudioDownloadError(RuntimeError):
    """音频下载失败。"""


def _write_bytes(path: Path, data: bytes, append: bool) -> None:
    with open(path, "ab" if append else "wb") as handle:
        handle.write(data)


class AudioFetcher:
    """音频文件下载与磁盘缓存。"""

    def __init__(
        self,
        cache_dir: Path | None = None,
        max_mb: int = 2048,
        timeout: float = 60.0,
    ) -> None:
        self._cache_dir = cache_dir or (plugin_data_dir() / "audio")
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._max_bytes = max(64, int(max_mb)) * 1024 * 1024
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------- 路径

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    def src_path(self, bv: str, quality: str) -> Path:
        """源音轨缓存路径。"""
        return self._cache_dir / f"{bv}_{quality}.m4a"

    def mp3_path(self, bv: str, preset: str) -> Path:
        """成品 mp3 缓存路径。"""
        return self._cache_dir / f"{bv}_{preset}.mp3"

    # ------------------------------------------------------------- 容量

    def size_bytes(self) -> int:
        total = 0
        try:
            for item in self._cache_dir.iterdir():
                if item.is_file():
                    total += item.stat().st_size
        except OSError:
            return 0
        return total

    def touch(self, path: Path) -> None:
        """把文件的 mtime 顶到最新，表示「刚被用过」。"""
        try:
            path.touch(exist_ok=True)
        except OSError:
            pass

    def _evict(self) -> None:
        """超出上限时按最久未使用淘汰。"""
        try:
            files = [item for item in self._cache_dir.iterdir() if item.is_file()]
        except OSError:
            return
        total = 0
        sizes: dict[Path, int] = {}
        for item in files:
            try:
                size = item.stat().st_size
            except OSError:
                continue
            sizes[item] = size
            total += size
        if total <= self._max_bytes:
            return

        removed = 0
        for victim in sorted(sizes, key=lambda p: p.stat().st_mtime):
            if total <= self._max_bytes:
                break
            try:
                size = sizes[victim]
                victim.unlink()
                total -= size
                removed += 1
            except OSError:
                continue
        if removed:
            logger.debug("%s 缓存超出上限，已淘汰 %d 个文件", LOG_PREFIX, removed)

    def clear(self) -> tuple[int, int]:
        """清空缓存，返回 ``(文件数, 释放字节数)``。"""
        count = 0
        freed = 0
        try:
            items = list(self._cache_dir.iterdir())
        except OSError:
            return 0, 0
        for item in items:
            if not item.is_file():
                continue
            try:
                size = item.stat().st_size
                item.unlink()
                count += 1
                freed += size
            except OSError:
                continue
        return count, freed

    # ------------------------------------------------------------- 下载

    async def _download_once(
        self, urls: list[str], dest: Path, headers: dict[str, str]
    ) -> Path:
        part = dest.with_name(dest.name + ".part")
        existing = part.stat().st_size if part.exists() else 0

        last_error: Exception | None = None
        for url in urls:
            if not url:
                continue
            request_headers = dict(headers)
            if existing > 0:
                request_headers["Range"] = f"bytes={existing}-"
            try:
                resp = await self._client.get(url, headers=request_headers)
            except Exception as exc:  # noqa: BLE001 - 换下一个候选地址
                last_error = exc
                continue

            if resp.status_code == 416 and existing > 0:
                # 本地分片已经不小于远端长度，直接收尾
                await asyncio.to_thread(part.replace, dest)
                return dest

            if resp.status_code >= 400:
                last_error = AudioDownloadError(f"HTTP {resp.status_code}")
                continue

            append = existing > 0 and resp.status_code == 206
            if existing > 0 and not append:
                logger.debug("%s 服务端未返回 206，改为整段重新下载", LOG_PREFIX)

            await asyncio.to_thread(_write_bytes, part, resp.content, append)
            await asyncio.to_thread(part.replace, dest)
            return dest

        raise AudioDownloadError(f"下载失败：{last_error}")

    async def download(
        self,
        urls: list[str] | str,
        dest: Path,
        headers: dict[str, str],
        retries: int = 3,
    ) -> Path:
        """下载到 ``dest``，带候选地址轮换与重试。

        ``urls`` 可以是单个地址，也可以是「主地址 + 备用地址」的列表。
        """
        logger.info("%s 正在下载音频（%s）…", LOG_PREFIX, dest.stem)
        candidates = [urls] if isinstance(urls, str) else list(urls)
        candidates = [url for url in candidates if url]
        if not candidates:
            raise AudioDownloadError("没有可用的下载地址")

        last_error: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                path = await self._download_once(candidates, dest, headers)
                self._evict()
                return path
            except Exception as exc:  # noqa: BLE001 - 统一重试
                last_error = exc
                if attempt < retries - 1:
                    delay = 0.6 * (attempt + 1)
                    logger.debug(
                        "%s 音频下载失败（%s），%.1f 秒后重试第 %d 次",
                        LOG_PREFIX,
                        exc,
                        delay,
                        attempt + 1,
                    )
                    await asyncio.sleep(delay)

        raise AudioDownloadError(f"连续 {retries} 次下载仍未成功：{last_error}")
