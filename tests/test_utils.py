"""工具函数测试：URL 归一化、日期解析、状态游标、错误分类。"""
from __future__ import annotations

import unittest

from src.errors import LoginRequiredError, RateLimitedError
from src.yt_channel_scanner import (
    YTChannelScanner,
    _parse_since,
)
from src.yt_video_detail_fetcher import _expand_langs
from src.ydl_base import YDLBase


class TestVideosTabUrl(unittest.TestCase):
    def test_bare_handle_gets_videos_tab(self):
        self.assertEqual(
            YTChannelScanner._videos_tab_url("https://www.youtube.com/@LinusTechTips"),
            "https://www.youtube.com/@LinusTechTips/videos",
        )

    def test_already_videos_tab_unchanged(self):
        url = "https://www.youtube.com/@LinusTechTips/videos"
        self.assertEqual(YTChannelScanner._videos_tab_url(url), url)

    def test_watch_url_unchanged(self):
        url = "https://www.youtube.com/watch?v=abc"
        self.assertEqual(YTChannelScanner._videos_tab_url(url), url)

    def test_non_youtube_unchanged(self):
        url = "https://example.com/@someone"
        self.assertEqual(YTChannelScanner._videos_tab_url(url), url)


class TestParseSince(unittest.TestCase):
    def test_supported_formats(self):
        self.assertEqual(_parse_since("20260901"), "20260901")
        self.assertEqual(_parse_since("2026-09-01"), "20260901")
        self.assertEqual(_parse_since("2026-09-01T00:00:00Z"), "20260901")

    def test_none_and_empty(self):
        self.assertIsNone(_parse_since(None))
        self.assertIsNone(_parse_since(""))

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            _parse_since("not-a-date")


class TestStateSince(unittest.TestCase):
    def test_extracts_dateafter(self):
        state = {"channels": {"u": {"last_scan": "2026-09-01T12:00:00+08:00"}}}
        self.assertEqual(YTChannelScanner._state_since(state, "u"), "20260901")

    def test_z_suffix(self):
        state = {"channels": {"u": {"last_scan": "2026-09-01T00:00:00Z"}}}
        self.assertEqual(YTChannelScanner._state_since(state, "u"), "20260901")

    def test_missing_or_invalid(self):
        self.assertIsNone(YTChannelScanner._state_since({"channels": {}}, "u"))
        self.assertIsNone(
            YTChannelScanner._state_since(
                {"channels": {"u": {"last_scan": "bad"}}}, "u"
            )
        )


class TestClassifyExtractionError(unittest.TestCase):
    def setUp(self):
        # YDLBase 需要 AppConfig，这里仅借用实例方法
        from src.config_loader import AppConfig

        self.base = YDLBase(AppConfig(proxy={"enabled": False}, yt_dlp={}))

    def test_login_required(self):
        err = self.base._classify_extraction_error(Exception("Login required to view"))
        self.assertIsInstance(err, LoginRequiredError)

    def test_rate_limited(self):
        err = self.base._classify_extraction_error(Exception("HTTP Error 429: Too Many Requests"))
        self.assertIsInstance(err, RateLimitedError)

    def test_other_error_passes_through(self):
        original = ValueError("something else")
        self.assertIs(self.base._classify_extraction_error(original), original)


class TestExpandLangs(unittest.TestCase):
    def test_zh_alias_expansion(self):
        self.assertEqual(
            _expand_langs(["zh", "en"]),
            ["zh-CN", "zh-Hans", "zh-Hant", "zh-TW", "zh-HK", "en"],
        )

    def test_plain_passthrough(self):
        self.assertEqual(_expand_langs(["en", "ja"]), ["en", "ja"])


if __name__ == "__main__":
    unittest.main()
