"""types.py 数据模型与序列化测试。"""
from __future__ import annotations

import json
import unittest
from datetime import datetime

from src.types import (
    ChannelResult,
    ChannelScanOutput,
    FetchMeta,
    SearchItem,
    SearchOutput,
    SubtitlesContainer,
    VideoDetail,
    VideoFetchOutput,
    YouTubeItem,
    json_default,
)


def make_item(**over) -> YouTubeItem:
    base = dict(
        url="https://www.youtube.com/watch?v=abc123",
        title="测试视频",
        description="desc",
        author="author",
        published_at=datetime(2025, 1, 2, 3, 4, 5),
        video_id="abc123",
    )
    base.update(over)
    return YouTubeItem(**base)


class TestYouTubeItem(unittest.TestCase):
    def test_to_dict_and_json_roundtrip(self):
        item = make_item()
        data = item.to_dict()
        # datetime 字段在 asdict 后仍为 datetime，需经 json_default 序列化
        self.assertIsInstance(data["published_at"], datetime)
        text = json.dumps(data, default=json_default, ensure_ascii=False)
        loaded = json.loads(text)
        self.assertEqual(loaded["published_at"], "2025-01-02T03:04:05")
        self.assertEqual(loaded["video_id"], "abc123")
        self.assertEqual(loaded["raw"], {})

    def test_defaults(self):
        item = YouTubeItem(url="u", title="t")
        self.assertEqual(item.categories, [])
        self.assertEqual(item.tags, [])
        self.assertIsNone(item.subtitle)
        self.assertIsNone(item.channel_follower_count)


class TestPydanticOutputs(unittest.TestCase):
    def test_video_fetch_output_roundtrip(self):
        output = VideoFetchOutput(
            meta=FetchMeta(operation="video fetch", url="u", fetched_at=datetime.now()),
            detail=VideoDetail(video_id="v1", url="u", title="t"),
            subtitles=SubtitlesContainer(text="你好"),
        )
        text = output.model_dump_json(indent=2)
        loaded = json.loads(text)
        self.assertEqual(loaded["detail"]["video_id"], "v1")
        self.assertEqual(loaded["subtitles"]["text"], "你好")
        # datetime 序列化为 ISO 字符串
        self.assertIn("T", loaded["meta"]["fetched_at"])

    def test_search_output_with_verdict_none(self):
        output = SearchOutput(generated_at=datetime.now(), keyword="AI")
        output.items.append(SearchItem(video=make_item(), subtitle=None, verdict=None))
        output.total = len(output.items)
        loaded = json.loads(output.model_dump_json())
        self.assertEqual(loaded["total"], 1)
        self.assertIsNone(loaded["items"][0]["verdict"])
        self.assertIsNone(loaded["items"][0]["subtitle"])

    def test_channel_scan_output(self):
        output = ChannelScanOutput(
            generated_at=datetime.now(),
            channels=[ChannelResult(url="u", alias="a", items=[make_item()], failed_urls=[])],
        )
        loaded = json.loads(output.model_dump_json())
        self.assertEqual(len(loaded["channels"]), 1)
        self.assertEqual(loaded["channels"][0]["alias"], "a")
        self.assertEqual(loaded["channels"][0]["items"][0]["title"], "测试视频")


if __name__ == "__main__":
    unittest.main()
