# 模块功能设计（关键词搜索 + 可选内容过滤）
# 2.1 关键词管理
# - 支持从配置文件批量读取关键词 ./config/keywords.yaml
# - 每个关键词可独立配置是否启用 (enabled: true/false)
# - 支持手动输入关键词（--keyword 追加）；一旦指定手动关键词，
#   配置文件中的关键词即被完全丢弃，不再合并
# 2.2 YouTube 搜索
# - 使用 yt-dlp 执行搜索：ytsearch{limit}:{keyword}（extract_flat 平扫）
# - 仅返回列表级元数据（title, url, video_id, channel, upload_date 等基础字段）
# - 不逐条抓取单视频详情/字幕（需要时用 src.yt_video_detail_fetcher 补抓）
# - 设置最大尝试次数限制（重试机制由 yt-dlp retries 配置承担）
# 2.3 内容过滤（可选）
# - 配置文件可全局开关内容过滤功能 (app_config.yaml: filter.enabled)
# - 基于 Rubric 评估方法，对视频的 title 和 description 进行多维度评分
#   （内置 keyword_rubric 引擎，本地规则实现；预留本地模型引擎接口）
# - 最终输出匹配判定结果（matched / not_matched / needs_review）
# 2.4 结果输出
# - 所有搜索结果写入结构化输出文件（JSON 格式，pretty print）
# - 输出文件包含：原始视频信息、字幕内容、过滤评估结果、匹配状态
#   文件名 yt_search_YYYYMMDD_HHMMSS.json
#
# 用法：
#   python -m src.yt_scf search --config config/keywords.yaml
#   python -m src.yt_scf search --keyword "AI agent" --keyword "robotics"
#   python -m src.yt_scf search --keyword "AI agent" --limit 5 --no-filter --output-stdout
#
# 说明：搜索仅使用 ytsearch extract_flat 条目（单次请求，不逐条抓取详情/字幕），
# 需要完整元数据或字幕时请改用 src.yt_video_detail_fetcher。

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Protocol

from .config_loader import (
    AppConfig,
    KeywordConfig,
    load_app_config,
    load_keywords,
)
from .errors import LoginRequiredError, RateLimitedError
from .logger import get_logger
from .types import FilterVerdict, SearchItem, SearchOutput, YouTubeItem
from .ydl_base import YDLBase
from .yt_video_detail_fetcher import YTVideoDetailFetcher

logger = get_logger(__name__)


# ---------------------------------------------------------------------- 过滤引擎


class FilterEngine(Protocol):
    """内容过滤引擎接口。engine 名用于输出标识。"""

    name: str

    async def evaluate(self, keyword: str, item: YouTubeItem) -> FilterVerdict: ...


@dataclass
class RubricDimension:
    """Rubric 评分维度：维度名 + 命中关键词列表。"""

    dimension: str
    keywords: list[str] = field(default_factory=list)


class KeywordRubricFilter:
    """内置 Rubric 引擎（本地规则，无外部依赖）。

    每个维度在 title+description 中命中任一关键词即得 1 分；
    score = 命中维度数 / 总维度数，按阈值输出 matched / needs_review / not_matched。
    """

    name = "keyword_rubric"

    def __init__(
        self,
        dimensions: list[RubricDimension],
        match_threshold: float = 0.6,
        needs_review_threshold: float = 0.3,
    ) -> None:
        self.dimensions = dimensions
        self.match_threshold = match_threshold
        self.needs_review_threshold = needs_review_threshold

    @classmethod
    def from_config(cls, filter_cfg: dict[str, Any]) -> "KeywordRubricFilter":
        dimensions = [
            RubricDimension(
                dimension=str(d.get("dimension", f"dim_{i}")),
                keywords=[str(k) for k in (d.get("keywords") or [])],
            )
            for i, d in enumerate(filter_cfg.get("rubric") or [])
            if isinstance(d, dict)
        ]
        return cls(
            dimensions,
            match_threshold=float(filter_cfg.get("match_threshold", 0.6)),
            needs_review_threshold=float(filter_cfg.get("needs_review_threshold", 0.3)),
        )

    async def evaluate(self, keyword: str, item: YouTubeItem) -> FilterVerdict:
        haystack = f"{item.title}\n{item.description}".lower()
        dimensions: dict[str, bool] = {}
        for dim in self.dimensions:
            dimensions[dim.dimension] = any(k.lower() in haystack for k in dim.keywords)
        if not dimensions:
            return FilterVerdict(
                engine=self.name, status="needs_review",
                reason="未配置 rubric 维度，无法评估", score=0.0,
            )
        score = sum(dimensions.values()) / len(dimensions)
        if score >= self.match_threshold:
            status, reason = "matched", "达到匹配阈值"
        elif score >= self.needs_review_threshold:
            status, reason = "needs_review", "介于匹配与待审核阈值之间"
        else:
            status, reason = "not_matched", "低于待审核阈值"
        return FilterVerdict(
            engine=self.name, status=status, score=round(score, 3),
            match_threshold=self.match_threshold,
            needs_review_threshold=self.needs_review_threshold,
            dimensions=dimensions, reason=reason,
        )


# ---------------------------------------------------------------------- 搜索器


class YTSearchContentFilter(YDLBase):
    """关键词搜索器：ytsearch + 元数据/字幕补全 + 可选过滤。"""

    def __init__(
        self,
        app_config: Optional[AppConfig] = None,
        filter_engine: Optional[FilterEngine] = None,
    ) -> None:
        super().__init__(app_config)
        self.fetcher = YTVideoDetailFetcher(self.app_config)
        self.filter_engine = filter_engine

    async def __aenter__(self) -> "YTSearchContentFilter":
        await self.fetcher.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.fetcher.close()

    # ------------------------------------------------------------------ 关键词

    @staticmethod
    def resolve_keywords(
        config_path: str,
        manual_keywords: Optional[list[str]] = None,
    ) -> list[str]:
        """解析最终生效的关键词列表。

        优先级：只要用户手动指定了关键词（--keyword），
        就完全丢弃配置文件中的关键词，仅使用手动指定的；
        否则全部来自配置文件（仅取 enabled 条目）。
        """
        if manual_keywords:
            keywords = list(manual_keywords)
        else:
            keywords = [k.keyword for k in load_keywords(config_path) if k.enabled]
        # 去重并保持顺序
        seen: set[str] = set()
        unique = [k for k in keywords if not (k in seen or seen.add(k))]
        return unique

    # ------------------------------------------------------------------ 搜索

    async def search(
        self,
        keyword: str,
        limit: int = 10,
    ) -> SearchOutput:
        """执行单次关键词搜索并返回结构化结果。

        仅使用 ytsearch extract_flat 条目（单次搜索请求，不逐条抓取
        单视频详情/字幕）。条目通常含 title/url/video_id/channel/duration
        等基础字段，可能缺少 description；字幕恒为 None，需要字幕或完整
        元数据时请改用 yt_video_detail_fetcher 补抓。
        """
        cookie_path = await self.fetcher._cookie()
        opts = await self._get_ydl_opts(cookie_path)
        opts["extract_flat"] = "in_playlist"
        target = f"ytsearch{limit}:{keyword}"
        info = await self._extract_info(target, opts, "search")
        entries = [e for e in (info or {}).get("entries", []) if e]

        output = SearchOutput(generated_at=datetime.now(), keyword=keyword)
        for entry in entries:
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

            item = YouTubeItem(
                url=entry_url,
                title=entry.get("title") or "",
                description=entry.get("description") or "",
                author=entry.get("channel") or entry.get("uploader") or "",
                published_at=published_at,
                thumbNail=entry.get("thumbnail") or "",
                video_id=entry.get("id") or "",
                channel_id=entry.get("channel_id") or "",
                channel_url=entry.get("channel_url") or "",
            )
            verdict = (
                await self.filter_engine.evaluate(keyword, item)
                if self.filter_engine is not None
                else None
            )
            output.items.append(SearchItem(video=item, subtitle=None, verdict=verdict))
            self.logger.info(
                "搜索命中: %s | %s%s",
                item.title[:60], item.url,
                f" | verdict={verdict.status}" if verdict else "",
            )
        output.total = len(output.items)
        self.logger.info("关键词 [%s] 搜索完成: %d/%d", keyword, output.total, len(entries))
        return output


# ---------------------------------------------------------------------- CLI


def _default_filter_engine(config: AppConfig, disabled: bool) -> Optional[FilterEngine]:
    if disabled or not config.filter.get("enabled"):
        return None
    engine = config.filter.get("engine", "keyword_rubric")
    if engine != "keyword_rubric":
        raise ValueError(f"未知过滤引擎: {engine}（引入本地模型引擎需先确认依赖）")
    return KeywordRubricFilter.from_config(config.filter)


def _search_output_path(config: AppConfig, keyword: str, explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit)
    safe = "".join(c if c.isalnum() else "_" for c in keyword)[:40]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return config.ensure_output_dir() / f"yt_search_{safe}_{stamp}.json"


async def _run(args: argparse.Namespace) -> None:
    config = load_app_config()
    keywords = YTSearchContentFilter.resolve_keywords(args.config, args.keyword)
    if not keywords:
        raise ValueError("无可用关键词（检查 config/keywords.yaml 或 --keyword）")

    filter_engine = _default_filter_engine(config, args.no_filter)
    async with YTSearchContentFilter(config, filter_engine) as searcher:
        for keyword in keywords:
            output = await searcher.search(keyword, limit=args.limit)
            if args.output_stdout:
                print(output.model_dump_json(indent=2))
            else:
                path = _search_output_path(config, keyword, args.output)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(output.model_dump_json(indent=2), encoding="utf-8")
                logger.info("结果已写入: %s", path)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="YouTube 关键词搜索 + 可选内容过滤")
    sub = parser.add_subparsers(dest="command", required=True)
    search_parser = sub.add_parser("search", help="执行关键词搜索")
    search_parser.add_argument("--config", default="config/keywords.yaml", help="关键词配置")
    search_parser.add_argument("--keyword", action="append", help="手动关键词（可多次指定）；指定后完全忽略配置文件中的关键词")
    search_parser.add_argument("--limit", type=int, help="每个关键词最大结果数")
    search_parser.add_argument("--no-filter", action="store_true", help="关闭内容过滤")
    search_parser.add_argument("--output", help="输出 JSON 路径")
    search_parser.add_argument("--output-stdout", action="store_true", help="JSON 直出 stdout（Agent 捕获）")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (LoginRequiredError, RateLimitedError) as error:
        parser.exit(2, f"致命错误: {error}\n")


if __name__ == "__main__":
    main()
