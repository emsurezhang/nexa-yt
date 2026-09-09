"""字幕语言挑选逻辑测试（离线，喂伪造 info_dict）。"""
from __future__ import annotations

import unittest
from pathlib import Path

from src.config_loader import AppConfig
from src.yt_video_detail_fetcher import YTVideoDetailFetcher


def make_fetcher() -> YTVideoDetailFetcher:
    cfg = AppConfig(proxy={"enabled": False}, yt_dlp={"cookies_from_browser": ""})
    return YTVideoDetailFetcher(cfg)


def fmt(url: str, ext: str = "vtt") -> dict:
    return {"ext": ext, "url": url}


INFO = {
    "subtitles": {
        "zh-CN": [fmt("http://manual-zh.vtt")],
        "en": [fmt("http://manual-en.vtt")],
    },
    "automatic_captions": {
        "zh-CN": [fmt("http://auto-zh.vtt")],
        "en": [fmt("http://auto-en.vtt")],
        "ja": [fmt("http://auto-ja.vtt")],
    },
}


class TestPickSubtitleFormats(unittest.TestCase):
    def test_manual_preferred_over_auto(self):
        fetcher = make_fetcher()
        picked = fetcher._pick_subtitle_formats(INFO, ["zh-CN", "en"])
        self.assertEqual(
            [(lang, kind) for lang, kind, _ in picked],
            [("zh-CN", "manual"), ("en", "manual")],
        )

    def test_auto_fallback_when_no_manual(self):
        fetcher = make_fetcher()
        picked = fetcher._pick_subtitle_formats(INFO, ["ja", "en"])
        self.assertEqual(picked[0][0:2], ("ja", "auto"))
        self.assertEqual(picked[1][0:2], ("en", "manual"))

    def test_language_priority_order(self):
        fetcher = make_fetcher()
        picked = fetcher._pick_subtitle_formats(INFO, ["en", "zh-CN"])
        self.assertEqual([p[0] for p in picked], ["en", "zh-CN"])

    def test_vtt_only_and_url_required(self):
        info = {
            "subtitles": {
                "en": [fmt("", ext="vtt"), {"ext": "srv1", "url": "http://x"}, fmt("http://ok.vtt")],
            }
        }
        picked = make_fetcher()._pick_subtitle_formats(info, ["en"])
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0][2]["url"], "http://ok.vtt")

    def test_no_match_returns_empty(self):
        self.assertEqual(make_fetcher()._pick_subtitle_formats({}, ["fr"]), [])


if __name__ == "__main__":
    unittest.main()
