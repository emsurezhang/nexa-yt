"""频道扫描流程测试：mock 网络层，验证增量状态、输出结构与 pretty JSON。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from src.config_loader import AppConfig, ChannelConfig
from src.types import YouTubeItem
from src.yt_channel_scanner import (
    YTChannelScanner,
    _default_state_path,
    _dump,
    _scan_output_path,
)


def make_config(tmp: str) -> AppConfig:
    return AppConfig(
        output_dir=Path(tmp),
        proxy={"enabled": False},
        yt_dlp={"sleep_interval": 0, "cookies_from_browser": "", "impersonate": "chrome"},
        filter={"enabled": False},
    )


def agen_recording(records: list, items: list[YouTubeItem], fail: bool = False):
    """构造记录调用参数的假异步生成器。"""
    async def gen(url, limit=None, since=None):
        records.append({"url": url, "limit": limit, "since": since})
        for item in items:
            yield item
        if fail:
            raise PluginError("partial failure")
    return gen


class TestScanChannelsFlow(unittest.IsolatedAsyncioTestCase):
    async def test_incremental_since_and_state_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            scanner = YTChannelScanner(config)
            scanner.channel_scan = agen_recording([], [YouTubeItem(url="u1", title="t1")])  # type: ignore[method-assign]

            channels = [
                ChannelConfig(url="https://www.youtube.com/@a/videos", alias="a", max_results=3, enabled=True),
                ChannelConfig(url="https://www.youtube.com/@off/videos", alias="off", enabled=False),
            ]
            output = await scanner.scan_channels(channels=channels)

            # 禁用频道被跳过
            self.assertEqual(len(output.channels), 1)
            result = output.channels[0]
            self.assertEqual(result.alias, "a")
            self.assertEqual(len(result.items), 1)
            self.assertEqual(result.items[0].url, "u1")

            # channel_state.json 已写入（pretty print）
            state_path = _default_state_path(config)
            self.assertTrue(state_path.exists())
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("https://www.youtube.com/@a/videos", state["channels"])
            self.assertIn("last_scan", state["channels"]["https://www.youtube.com/@a/videos"])
            self.assertIn("\n  ", state_path.read_text(encoding="utf-8"))  # pretty

            # 第二次扫描：since 取自上次扫描时间（YYYYMMDD）
            records: list[dict] = []
            scanner2 = YTChannelScanner(config)
            scanner2.channel_scan = agen_recording(records, [])  # type: ignore[method-assign]
            await scanner2.scan_channels(channels=channels)
            since = records[0]["since"]
            self.assertRegex(since, r"^\d{8}$")

    async def test_use_state_false_skips_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            scanner = YTChannelScanner(config)
            scanner.channel_scan = agen_recording([], [])  # type: ignore[method-assign]
            await scanner.scan_channels(
                channel_urls=["https://www.youtube.com/@a/videos"],
                max_results=2,
                use_state=False,
            )
            self.assertFalse(_default_state_path(config).exists())

    async def test_channel_scan_uses_flat_entries_only(self):
        """平扫直接用 extract_flat 条目组装 YouTubeItem，不逐条抓取单视频详情。"""
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            scanner = YTChannelScanner(config)
            scanner._extract_info = AsyncMock(return_value={  # type: ignore[method-assign]
                "channel": "chan",
                "channel_id": "ch1",
                "channel_url": "https://www.youtube.com/@c",
                "channel_follower_count": 1234,
                "entries": [
                    {"id": "v1", "url": "v1", "title": "t1", "channel": "chan",
                     "channel_id": "ch1", "upload_date": "20260901"},
                    {"id": "v2", "url": "https://www.youtube.com/watch?v=v2", "title": "t2"},
                    {"url": None},   # 无 URL 条目跳过
                    None,            # 空条目过滤
                ],
            })
            scanner.fetcher.fetch_video_item = AsyncMock()  # type: ignore[method-assign]

            items = [i async for i in scanner.channel_scan("https://www.youtube.com/@c/videos", limit=10)]

            self.assertEqual(len(items), 2)
            # 裸视频 ID 自动补全为 watch URL
            self.assertEqual(items[0].url, "https://www.youtube.com/watch?v=v1")
            self.assertEqual(items[0].video_id, "v1")
            self.assertEqual(items[0].author, "chan")
            self.assertEqual(items[0].channel_follower_count, 1234)
            self.assertEqual(items[0].published_at.isoformat(), "2026-09-01T00:00:00")
            # 关键断言：全程未发起单视频抓取
            scanner.fetcher.fetch_video_item.assert_not_called()


class TestDumpAndPaths(unittest.TestCase):
    def test_scan_output_path_naming(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            path = _scan_output_path(config, None)
            self.assertRegex(path.name, r"^yt_channel_scan_\d{8}_\d{6}\.json$")
            self.assertEqual(path.parent, Path(tmp))

    def test_dump_pretty_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            scanner = YTChannelScanner(config)
            scanner.channel_scan = agen_recording([], [YouTubeItem(url="u1", title="标题")])  # type: ignore[method-assign]
            import asyncio
            output = asyncio.run(scanner.scan_channels(
                channel_urls=["u"], use_state=False
            ))
            path = Path(tmp) / "out.json"
            _dump(output, path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("\n  ", text)  # pretty print
            parsed = json.loads(text)
            self.assertEqual(parsed["channels"][0]["items"][0]["title"], "标题")


if __name__ == "__main__":
    unittest.main()
