"""搜索流程测试：mock yt-dlp 网络层，验证平扫输出结构与错误处理。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from src.config_loader import AppConfig
from src.errors import LoginRequiredError, RateLimitedError
from src.types import SearchOutput
from src.yt_scf import (
    KeywordRubricFilter,
    RubricDimension,
    YTSearchContentFilter,
    _search_output_path,
)

SEARCH_INFO = {
    "entries": [
        {"id": "v1", "url": "v1", "title": "AI news", "channel": "ch1", "upload_date": "20260901"},
        {"id": "v2", "url": "https://www.youtube.com/watch?v=v2", "title": "AI tutorial", "channel": "ch2"},
        {"id": "v3", "url": "v3", "title": "cooking", "channel": "ch3"},
        {"url": None},  # 无 URL 条目应被跳过
        None,           # 空条目应被过滤
    ]
}


def make_config(tmp: str) -> AppConfig:
    return AppConfig(
        output_dir=Path(tmp),
        proxy={"enabled": False},
        yt_dlp={"sleep_interval": 0, "cookies_from_browser": "", "impersonate": "chrome"},
        filter={"enabled": False},
    )


class TestSearchFlow(unittest.IsolatedAsyncioTestCase):
    async def _make_searcher(self, tmp: str, with_filter: bool = True) -> YTSearchContentFilter:
        config = make_config(tmp)
        engine = (
            KeywordRubricFilter([RubricDimension("AI 相关", ["ai"])], 0.5, 0.2)
            if with_filter else None
        )
        searcher = YTSearchContentFilter(config, engine)
        searcher._extract_info = AsyncMock(return_value=SEARCH_INFO)  # type: ignore[method-assign]
        searcher.fetcher.fetch_video_item = AsyncMock()  # type: ignore[method-assign]
        searcher.fetcher.fetch = AsyncMock()  # type: ignore[method-assign]
        return searcher

    async def test_search_flat_output_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            searcher = await self._make_searcher(tmp)
            output: SearchOutput = await searcher.search("AI", limit=5)

            # 无 URL 与空条目被跳过，仅 3 条有效
            self.assertEqual(output.total, 3)
            self.assertEqual(output.keyword, "AI")
            # 平扫恒无字幕
            for item in output.items:
                self.assertIsNone(item.subtitle)
            # 过滤判定：2 条命中 "ai"，1 条不命中
            statuses = [i.verdict.status for i in output.items]
            self.assertEqual(statuses, ["matched", "matched", "not_matched"])
            # 裸视频 ID 补全为 watch URL；upload_date 解析为 published_at
            self.assertEqual(output.items[0].video.url, "https://www.youtube.com/watch?v=v1")
            self.assertEqual(output.items[0].video.published_at.isoformat(), "2026-09-01T00:00:00")
            self.assertEqual(output.items[1].video.author, "ch2")
            # 输出可被 JSON 序列化（Agent 直出场景）
            parsed = json.loads(output.model_dump_json(indent=2))
            self.assertEqual(parsed["total"], 3)
            self.assertEqual(parsed["items"][0]["video"]["video_id"], "v1")

            # 关键断言：全程未发起单视频抓取/字幕请求
            searcher.fetcher.fetch_video_item.assert_not_called()
            searcher.fetcher.fetch.assert_not_called()

    async def test_search_no_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            searcher = await self._make_searcher(tmp, with_filter=False)
            output = await searcher.search("AI")
            self.assertEqual(output.total, 3)
            for item in output.items:
                self.assertIsNone(item.verdict)

    async def test_fatal_errors_propagate(self):
        with tempfile.TemporaryDirectory() as tmp:
            for exc in (LoginRequiredError("x"), RateLimitedError("y")):
                searcher = await self._make_searcher(tmp)
                searcher._extract_info = AsyncMock(side_effect=exc)  # type: ignore[method-assign]
                with self.assertRaises(type(exc)):
                    await searcher.search("AI")


class TestSearchOutputPath(unittest.TestCase):
    def test_explicit_path(self):
        config = AppConfig(output_dir=Path("/tmp/x"))
        self.assertEqual(_search_output_path(config, "AI 搜索", "/tmp/out.json"), Path("/tmp/out.json"))

    def test_generated_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(output_dir=Path(tmp))
            path = _search_output_path(config, "AI agent 框架!", None)
            self.assertEqual(path.parent, Path(tmp))
            # isalnum() 对 CJK 字符返回 True，"框架" 被保留；空格与 "!" 转为 "_"
            self.assertRegex(path.name, r"^yt_search_AI_agent_框架__\d{8}_\d{6}\.json$")


if __name__ == "__main__":
    unittest.main()
