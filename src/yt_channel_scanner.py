# 1. 加载并校验配置文件
#   a. app_config.yaml（全局配置），包括网络代理、输出目录
#   b. channels.yaml（频道列表配置），包括频道 URL、别名、最大结果数、启用状态
# 2. 构建 yt-dlp（含 proxy、cookiesfrombrowser 从浏览器获取 cookies、sleep_interval 默认 10s）
# 3. 加载本地状态 channel_state.json（记录各频道上次扫描时间）
# 4. 对每个启用的频道：
#   a. 构建频道视频列表 URL（如 https://www.youtube.com/@handle/videos）
#   b. 使用 extract_flat=True 快速获取视频列表
#      - ydl_opts: {'extract_flat': True, 'playlistend': max_results, 'dateafter': since}
#   c. 组装 ChannelResult
# 5. 合并所有频道结果，使用 ChannelScanOutput 根模型序列化
# 6. 写入 JSON 文件（pretty json 必须）
#    文件名 yt_channel_scan_YYYYMMDD_HHMMSS.json，YYYYMMDD 是扫描日期，HHMMSS 是扫描时间
# 7. 更新 state.json
#
# 基础用法：按配置文件执行扫描
#   python -m src.yt_channel_scanner scan --config config/channels.yaml
#   python -m src.yt_channel_scanner scan --config config/channels.yaml \
#     --max-results 100 --since 2026-09-01 \
#     --channel-url "https://www.youtube.com/@channel/videos" \
#     --output ./data/custom.json
# 作为 Agent 工具调用（暴露标准接口，JSON 直出 stdout）：
#   python -m src.yt_channel_scanner agent --action scan \
#     --channel-urls '["https://www.youtube.com/@channel/videos"]' \
#     --max-results 50 --since 2026-09-01T00:00:00Z --output-stdout
# 同时方法也可被其他模块调用（channel_scan / scan_channels 均为公共接口）

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from .config_loader import (
    AppConfig,
    ChannelConfig,
    load_app_config,
    load_channels,
)
from .errors import LoginRequiredError, RateLimitedError
from .logger import get_logger
from .types import ChannelResult, ChannelScanOutput, YouTubeItem
from .ydl_base import YDLBase
from .yt_video_detail_fetcher import YTVideoDetailFetcher

logger = get_logger(__name__)

STATE_FILENAME = "channel_state.json"


def _default_state_path(config: AppConfig) -> Path:
    return config.ensure_output_dir() / STATE_FILENAME


class YTChannelScanner(YDLBase):
    """频道扫描器：extract_flat 快扫列表 + 逐条 full info。"""

    def __init__(
        self,
        app_config: Optional[AppConfig] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> None:
        super().__init__(app_config)
        self.fetcher = YTVideoDetailFetcher(self.app_config)
        self.progress_callback = progress_callback
        self.state_path = _default_state_path(self.app_config)

    async def __aenter__(self) -> "YTChannelScanner":
        await self.fetcher.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.fetcher.close()

    # ------------------------------------------------------------------ 状态

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"channels": {}}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            self.logger.warning("状态文件损坏，重置: %s (%s)", self.state_path, error)
            return {"channels": {}}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _state_since(state: dict[str, Any], url: str) -> Optional[str]:
        """从状态中提取某频道的增量游标（dateafter: YYYYMMDD）。"""
        entry = (state.get("channels") or {}).get(url) or {}
        last_scan = entry.get("last_scan")
        if not last_scan:
            return None
        try:
            return datetime.fromisoformat(str(last_scan).replace("Z", "+00:00")).strftime("%Y%m%d")
        except ValueError:
            return None

    # ------------------------------------------------------------------ 扫描

    @staticmethod
    def _videos_tab_url(target: str) -> str:
        """将裸 YouTube handle URL 限定到其 videos 标签页。"""
        parsed = urlsplit(target)
        if parsed.netloc.lower() not in {"youtube.com", "www.youtube.com"}:
            return target
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) != 1 or not path_parts[0].startswith("@"):
            return target
        return urlunsplit((parsed.scheme, parsed.netloc, f"/{path_parts[0]}/videos", parsed.query, parsed.fragment))

    async def channel_scan(
        self,
        target: str,
        limit: Optional[int] = 10,
        since: Optional[str] = None,
    ) -> AsyncIterator[YouTubeItem]:
        """扫描单个频道，异步产出 YouTubeItem。since 格式 YYYYMMDD。"""
        self.logger.info("Fetching history for %s, Limit: %s", target, limit)
        if limit is not None and limit < 1:
            self.logger.info("History fetch completed for %s: limit is less than 1", target)
            return

        cookie_path = await self.fetcher._cookie()
        opts = await self._get_ydl_opts(cookie_path)
        # 使用 extract_flat 快速获取视频列表。
        opts["extract_flat"] = "in_playlist"
        if limit is not None:
            opts["playlistend"] = limit
        if since:
            opts["dateafter"] = since
        target = self._videos_tab_url(target)
        info = await self._extract_info(target, opts, "history listing")
        if not info or "entries" not in info:
            self.logger.info("History fetch completed for %s: no entries returned", target)
            return

        entries = [e for e in info["entries"] if e]
        count = 0

        # 仅使用 extract_flat 列表数据组装 YouTubeItem，
        # 不逐条抓取单视频详情（无 N+1 请求，无 sleep_interval 间隔）。
        # 注意：平扫条目通常缺少 description / upload_date 等完整字段。
        for entry in entries:
            if limit is not None and count >= limit:
                break
            entry_url = entry.get("url") or entry.get("webpage_url")
            if not entry_url:
                continue
            if not str(entry_url).startswith("http"):
                entry_url = f"https://www.youtube.com/watch?v={entry_url}"

            upload_date = entry.get("upload_date")
            published_at = None
            if upload_date:
                try:
                    published_at = datetime.strptime(str(upload_date), "%Y%m%d")
                except ValueError:
                    published_at = None

            yield YouTubeItem(
                url=entry_url,
                title=entry.get("title") or "",
                description=entry.get("description") or "",
                author=entry.get("channel") or info.get("channel") or "",
                published_at=published_at,
                thumbNail=entry.get("thumbnail") or "",
                video_id=entry.get("id") or "",
                channel_id=entry.get("channel_id") or info.get("channel_id") or "",
                channel_url=entry.get("channel_url") or info.get("channel_url") or "",
                channel_follower_count=info.get("channel_follower_count"),
            )
            count += 1
            if self.progress_callback is not None:
                self.progress_callback(count)

        self.logger.info(
            "History fetch completed for %s: listed=%d, yielded=%d, limit=%s",
            target, len(entries), count, limit,
        )

    async def scan_one(
        self, url: str, alias: str, limit: int, since: Optional[str]
    ) -> ChannelResult:
        items: list[YouTubeItem] = []
        async for item in self.channel_scan(url, limit=limit, since=since):
            items.append(item)
        # extract_flat 平扫为单次列表请求，无单条失败场景，failed_urls 恒为空。
        return ChannelResult(url=url, alias=alias, items=items, failed_urls=[])

    async def scan_channels(
        self,
        channels: Optional[list[ChannelConfig]] = None,
        channel_urls: Optional[list[str]] = None,
        max_results: Optional[int] = None,
        since: Optional[str] = None,
        use_state: bool = True,
    ) -> ChannelScanOutput:
        """扫描多个频道并返回根模型（不写文件，由调用方决定输出方式）。"""
        state = self._load_state() if use_state else {"channels": {}}
        if channel_urls:
            targets = [ChannelConfig(url=u, alias=u, max_results=max_results or 10, enabled=True) for u in channel_urls]
        else:
            targets = [c for c in (channels or []) if c.enabled]

        results: list[ChannelResult] = []
        for channel in targets:
            effective_since = since or (self._state_since(state, channel.url) if use_state else None)
            self.logger.info(
                "扫描频道 %s (alias=%s, limit=%s, since=%s)",
                channel.url, channel.alias, max_results or channel.max_results, effective_since,
            )
            result = await self.scan_one(
                channel.url,
                channel.alias,
                max_results or channel.max_results,
                effective_since,
            )
            results.append(result)
            state.setdefault("channels", {})[channel.url] = {
                "alias": channel.alias,
                "last_scan": datetime.now().isoformat(),
            }

        if use_state:
            self._save_state(state)
            self.logger.info("状态已更新: %s", self.state_path)

        return ChannelScanOutput(generated_at=datetime.now(), channels=results)


# ---------------------------------------------------------------------- CLI


def _scan_output_path(config: AppConfig, explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return config.ensure_output_dir() / f"yt_channel_scan_{stamp}.json"


def _dump(output: ChannelScanOutput, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(output.model_dump_json(indent=2), encoding="utf-8")
    logger.info("结果已写入: %s", path)


def _parse_since(value: Optional[str]) -> Optional[str]:
    """支持 YYYYMMDD / YYYY-MM-DD / ISO8601，统一输出 YYYYMMDD（yt-dlp dateafter）。"""
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:10] if fmt == "%Y-%m-%d" else value, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y%m%d")
    except ValueError:
        raise ValueError(f"无法解析 --since 日期: {value}")


async def _run(args: argparse.Namespace) -> None:
    config = load_app_config()
    async with YTChannelScanner(config) as scanner:
        if args.command == "agent":
            if args.action != "scan":
                raise ValueError(f"未知 action: {args.action}")
            channel_urls = json.loads(args.channel_urls or "[]")
            output = await scanner.scan_channels(
                channel_urls=channel_urls,
                max_results=args.max_results,
                since=_parse_since(args.since),
                use_state=False,
            )
        else:  # scan
            channels = load_channels(args.config)
            # --channel-urls（JSON 数组）优先于 --channel-url（单个 URL），与 agent 子命令对齐
            urls = json.loads(args.channel_urls) if getattr(args, "channel_urls", None) else None
            if not urls and args.channel_url:
                urls = [args.channel_url]
            output = await scanner.scan_channels(
                channels=channels,
                channel_urls=urls,
                max_results=args.max_results,
                since=_parse_since(args.since),
            )
    if getattr(args, "output_stdout", False):
        print(output.model_dump_json(indent=2))
    else:
        _dump(output, _scan_output_path(config, getattr(args, "output", None)))


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="YouTube 频道增量扫描器")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser("scan", help="按配置文件执行扫描")
    scan_parser.add_argument("--config", default="config/channels.yaml", help="频道列表配置")
    scan_parser.add_argument("--max-results", type=int, help="覆盖 max_results")
    scan_parser.add_argument("--since", help="覆盖增量游标，格式 YYYY-MM-DD / YYYYMMDD / ISO8601")
    scan_parser.add_argument("--channel-url", help="仅扫描指定频道（单个 URL）")
    scan_parser.add_argument("--channel-urls", help='仅扫描指定频道（JSON 数组，优先于 --channel-url），如 \'["https://..."]\'')
    scan_parser.add_argument("--output", help="覆盖输出路径")
    scan_parser.add_argument("--output-stdout", action="store_true", help="JSON 直出 stdout（Agent 捕获）")

    agent_parser = sub.add_parser("agent", help="Agent 工具接口")
    agent_parser.add_argument("--action", default="scan", help="动作（当前仅 scan）")
    agent_parser.add_argument("--channel-urls", help='JSON 数组，如 \'["https://..."]\'')
    agent_parser.add_argument("--max-results", type=int)
    agent_parser.add_argument("--since")
    agent_parser.add_argument("--output-stdout", action="store_true")

    args = parser.parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (LoginRequiredError, RateLimitedError) as error:
        parser.exit(2, f"致命错误: {error}\n")


if __name__ == "__main__":
    main()
