# 1. 解析输入：url 判断是频道视频列表还是单个视频
# 2. 加载配置文件（app_config.yaml），构建 yt-dlp 选项（含 proxy、cookiesfrombrowser）
# 3. 调用 yt-dlp: extract_info(video_url, download=False)
#   - 获取完整 info_dict（含 metadata、subtitles、automatic_captions）
# 4. 使用 Pydantic VideoDetail 校验并转换元数据
# 5. 如未指定 --no-subtitle：
#   a. 从 info_dict 提取 subtitles（手动）和 automatic_captions（自动）
#   b. 按 --lang 优先级匹配语言
#   c. 下载指定字幕的 VTT 内容
#   d. 解析 VTT → SubtitleSegment 列表
#   e. 清理时间戳和重复行，并转换为连续文本
#   f. 组装 SubtitlesContainer
# 6. 组装 FetchMeta + VideoDetail → VideoFetchOutput
# 7. 序列化为 JSON 输出
#
# 基础用法：获取单个视频
#   python -m src.yt_video_detail_fetcher fetch --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --output ./data/video.json
# 使用视频 ID
#   python -m src.yt_video_detail_fetcher fetch --video-id dQw4w9WgXcQ --output ./data/video.json
# 指定字幕语言优先级
#   python -m src.yt_video_detail_fetcher fetch --video-id dQw4w9WgXcQ --lang zh-CN,en --output ./data/video.json
# 仅获取元数据（跳过字幕）
#   python -m src.yt_video_detail_fetcher fetch --video-id dQw4w9WgXcQ --no-subtitle --output ./data/video.json

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from datetime import datetime

import yt_dlp
from pathlib import Path
from typing import Any, Optional

from .config_loader import AppConfig, load_app_config
from .errors import LoginRequiredError, RateLimitedError
from .logger import get_logger
from .types import (
    FetchMeta,
    SubtitleSegment,
    SubtitlesContainer,
    VideoDetail,
    VideoFetchOutput,
    YouTubeItem,
    json_default,
)
from .ydl_base import YDLBase

logger = get_logger(__name__)

_VTT_TS = re.compile(r"(?P<start>\d{2}:\d{2}:\d{2}\.\d{3})\s+-->\s+(?P<end>\d{2}:\d{2}:\d{2}\.\d{3})")
_TAG = re.compile(r"<[^>]+>")
_LANG_ALIASES = {"zh": ("zh-CN", "zh-Hans", "zh-Hant", "zh-TW", "zh-HK")}


def _parse_timestamp(value: str) -> float:
    hh, mm, rest = value.split(":")
    return int(hh) * 3600 + int(mm) * 60 + float(rest)


def _clean_text(lines: list[str]) -> str:
    """去除 VTT 标签、时间戳与连续重复行。"""
    cleaned: list[str] = []
    for line in lines:
        line = _TAG.sub("", line).replace("&nbsp;", " ").strip()
        if line and (not cleaned or line != cleaned[-1]):
            cleaned.append(line)
    return " ".join(cleaned)


def parse_vtt(content: str) -> list[SubtitleSegment]:
    """解析 WebVTT 内容为 SubtitleSegment 列表。"""
    segments: list[SubtitleSegment] = []
    block: list[str] = []
    for raw in content.splitlines() + [""]:
        line = raw.strip("\ufeff").rstrip("\n")
        if not line.strip():
            if block:
                match = next((m for m in (_VTT_TS.search(b) for b in block) if m), None)
                if match:
                    text = _clean_text([b for b in block if not _VTT_TS.search(b) and not b.strip().isdigit()])
                    if text:
                        segments.append(
                            SubtitleSegment(
                                start=_parse_timestamp(match.group("start")),
                                end=_parse_timestamp(match.group("end")),
                                text=text,
                            )
                        )
                block = []
            continue
        block.append(line.strip())
    return segments


def vtt_to_text(content: str) -> str:
    """将 WebVTT 转为连续文本，去除滚动字幕产生的前后重叠。"""
    text = ""
    for segment in parse_vtt(content):
        if not text:
            text = segment.text
            continue

        overlap = min(len(text), len(segment.text))
        while overlap and text[-overlap:] != segment.text[:overlap]:
            overlap -= 1
        text += segment.text[overlap:]
    return text


def _expand_langs(langs: list[str]) -> list[str]:
    """展开语言别名（zh → zh-CN, zh-Hans ...），保持优先级顺序。"""
    expanded: list[str] = []
    for lang in langs:
        expanded.extend(_LANG_ALIASES.get(lang, (lang,)))
    return expanded


class YTVideoDetailFetcher(YDLBase):
    """单视频详情 + 字幕抓取器。"""

    def __init__(self, app_config: Optional[AppConfig] = None) -> None:
        super().__init__(app_config)
        self._cookie_file = None  # 独立使用的 cookie 缓存，close() 时删除

    async def __aenter__(self) -> "YTVideoDetailFetcher":
        self._cookie_file = await self._get_cookie_file()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._cookie_file and self._cookie_file.exists():
            self._cookie_file.unlink()
            self._cookie_file = None

    # ------------------------------------------------------------------ 字幕

    def _pick_subtitle_formats(
        self, info: dict[str, Any], langs: list[str]
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """按语言优先级从 subtitles(手动)/automatic_captions(自动)中挑选 vtt 格式。

        返回 [(lang, kind, format_dict)]，同语言手动优先于自动。
        """
        candidates: list[tuple[str, str, dict[str, Any]]] = []
        for lang in _expand_langs(langs):
            for kind, source in (("manual", info.get("subtitles") or {}),
                                 ("auto", info.get("automatic_captions") or {})):
                for fmt in source.get(lang) or []:
                    if fmt.get("ext") == "vtt" and fmt.get("url"):
                        candidates.append((lang, kind, fmt))
                        break
                if any(c[0] == lang for c in candidates):
                    break
        return candidates

    def _download_text(self, url: str, opts: dict[str, Any]) -> str:
        """通过 yt-dlp 网络栈下载字幕 VTT。

        复用 impersonate chrome 指纹 / 代理 / cookie，避免裸 urllib 请求特征
        与主请求不一致而被字幕服务器限流（HTTP 429）。
        """
        with yt_dlp.YoutubeDL(opts) as ydl:
            with ydl.urlopen(url) as response:
                return response.read().decode("utf-8", errors="replace")

    async def _fetch_subtitles(
        self, info: dict[str, Any], langs: list[str]
    ) -> SubtitlesContainer:
        container = SubtitlesContainer()
        opts = await self._get_ydl_opts(await self._cookie())
        max_retry = self.app_config.subtitle_max_retry
        backoff = self.app_config.subtitle_retry_backoff
        for lang, kind, fmt in self._pick_subtitle_formats(info, langs):
            content: Optional[str] = None
            last_error: Optional[Exception] = None
            for attempt in range(max_retry + 1):
                try:
                    await self._rate_limiter.acquire()
                    content = await asyncio.to_thread(self._download_text, fmt["url"], opts)
                    break
                except Exception as error:  # noqa: BLE001
                    last_error = error
                    # 仅对限流（429）做退避重试，其余错误直接放弃本条字幕
                    if "429" in str(error) and attempt < max_retry:
                        wait = backoff * (2 ** attempt)
                        self.logger.warning(
                            "字幕下载触发限流(429) %s(%s)，%.0fs 后重试(%d/%d)",
                            lang, kind, wait, attempt + 1, max_retry,
                        )
                        await asyncio.sleep(wait)
                        continue
                    break
            if content is None:
                self.logger.warning("字幕下载失败 %s(%s): %s", lang, kind, last_error)
                continue
            container.text = vtt_to_text(content)
            self.logger.info("字幕获取完成: %s (%s), %d 字符", lang, kind, len(container.text))
            break
        if not container.text:
            self.logger.info("无可用字幕 (langs=%s)", langs)
        return container

    # ------------------------------------------------------------------ 元数据

    @staticmethod
    def _to_detail(info: dict[str, Any]) -> VideoDetail:
        upload_date = info.get("upload_date")
        published_at = None
        if upload_date:
            try:
                published_at = datetime.strptime(str(upload_date), "%Y%m%d")
            except ValueError:
                published_at = None
        return VideoDetail(
            video_id=info.get("id", ""),
            url=info.get("webpage_url") or info.get("url") or "",
            title=info.get("title", ""),
            description=info.get("description") or "",
            author=info.get("uploader") or info.get("channel") or "",
            channel_id=info.get("channel_id") or "",
            channel_url=info.get("channel_url") or "",
            channel_follower_count=info.get("channel_follower_count"),
            duration=info.get("duration"),
            view_count=info.get("view_count"),
            like_count=info.get("like_count"),
            upload_date=upload_date,
            published_at=published_at,
            thumbnail=(info.get("thumbnail") or ""),
            categories=list(info.get("categories") or []),
            tags=list(info.get("tags") or []),
        )

    @staticmethod
    def _to_item(info: dict[str, Any]) -> YouTubeItem:
        detail = YTVideoDetailFetcher._to_detail(info)
        return YouTubeItem(
            url=detail.url,
            title=detail.title,
            description=detail.description,
            author=detail.author,
            published_at=detail.published_at,
            thumbNail=detail.thumbnail,
            video_id=detail.video_id,
            categories=detail.categories,
            tags=detail.tags,
            channel_id=detail.channel_id,
            channel_url=detail.channel_url,
            channel_follower_count=detail.channel_follower_count,
            raw={},
        )

    async def _cookie(self) -> Optional[Path]:
        if self._cookie_file is None:
            self._cookie_file = await self._get_cookie_file()
        return self._cookie_file

    # ------------------------------------------------------------------ 公共接口

    async def fetch_video_item(
        self, url: str, cookie_file: Optional[Path] = None
    ) -> YouTubeItem:
        """仅抓取视频元数据并转换为 YouTubeItem（供频道扫描/搜索复用）。"""
        opts = await self._get_ydl_opts(cookie_file or await self._cookie())
        info = await self._extract_info(url, opts, "video metadata")
        if not info:
            raise ValueError(f"无法获取视频信息: {url}")
        return self._to_item(info)

    async def fetch(
        self,
        url: Optional[str] = None,
        video_id: Optional[str] = None,
        langs: Optional[list[str]] = None,
        no_subtitle: bool = False,
    ) -> VideoFetchOutput:
        """抓取单个视频的完整详情与字幕。

        url 与 video_id 至少提供一个；video_id 会自动拼接标准 watch URL。
        """
        target = url or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
        if not target:
            raise ValueError("必须提供 url 或 video_id")
        langs = langs or ["zh-CN", "en"]

        started = time.monotonic()
        cookie_path = await self._cookie()
        opts = await self._get_ydl_opts(cookie_path)
        info = await self._extract_info(target, opts, "video fetch")
        if not info:
            raise ValueError(f"无法获取视频信息: {target}")

        subtitles = SubtitlesContainer()
        if not no_subtitle:
            subtitles = await self._fetch_subtitles(info, langs)

        output = VideoFetchOutput(
            meta=FetchMeta(
                operation="video fetch",
                url=target,
                fetched_at=datetime.now(),
                elapsed_seconds=round(time.monotonic() - started, 3),
                proxy_used=self.app_config.proxy_url is not None,
                cookie_used=cookie_path is not None,
            ),
            detail=self._to_detail(info),
            subtitles=subtitles,
        )
        return output


# ---------------------------------------------------------------------- CLI


def _default_output_path(config: AppConfig, video_id: str) -> Path:
    return config.ensure_output_dir() / f"yt_video_{video_id or 'unknown'}.json"


def _dump(output: VideoFetchOutput, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        output.model_dump_json(indent=2, by_alias=False),
        encoding="utf-8",
    )
    logger.info("结果已写入: %s", path)


async def _run(args: argparse.Namespace) -> None:
    config = load_app_config()
    async with YTVideoDetailFetcher(config) as fetcher:
        output = await fetcher.fetch(
            url=args.url,
            video_id=args.video_id,
            langs=[s.strip() for s in (args.lang or "zh-CN,en").split(",") if s.strip()],
            no_subtitle=args.no_subtitle,
        )
    if args.output_stdout:
        print(output.model_dump_json(indent=2))
    else:
        path = Path(args.output) if args.output else _default_output_path(config, output.detail.video_id)
        _dump(output, path)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="YouTube 单视频详情 + 字幕抓取")
    sub = parser.add_subparsers(dest="command", required=True)
    fetch_parser = sub.add_parser("fetch", help="抓取单个视频")
    fetch_parser.add_argument("--url", help="视频 URL")
    fetch_parser.add_argument("--video-id", dest="video_id", help="视频 ID（与 --url 二选一）")
    fetch_parser.add_argument("--lang", default="zh-CN,en", help="字幕语言优先级，逗号分隔")
    fetch_parser.add_argument("--no-subtitle", action="store_true", help="跳过字幕抓取")
    fetch_parser.add_argument("--output", help="输出 JSON 路径（默认 ./data/yt_video_<id>.json）")
    fetch_parser.add_argument("--output-stdout", action="store_true", help="JSON 直出 stdout（Agent 捕获）")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (LoginRequiredError, RateLimitedError) as error:
        parser.exit(2, f"致命错误: {error}\n")


if __name__ == "__main__":
    main()
