"""config_loader 与关键词解析测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.config_loader import (
    AppConfig,
    load_app_config,
    load_channels,
    load_keywords,
)
from src.yt_scf import YTSearchContentFilter


class TestAppConfig(unittest.TestCase):
    def test_proxy_url_enabled(self):
        cfg = AppConfig(proxy={"enabled": True, "host": "127.0.0.1", "port": 7897})
        self.assertEqual(cfg.proxy_url, "http://127.0.0.1:7897")

    def test_proxy_url_disabled_or_invalid(self):
        self.assertIsNone(AppConfig(proxy={"enabled": False}).proxy_url)
        self.assertIsNone(AppConfig(proxy={"enabled": True, "host": "", "port": 7897}).proxy_url)
        self.assertIsNone(AppConfig(proxy={"enabled": True, "host": "h", "port": "bad"}).proxy_url)

    def test_missing_config_uses_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_app_config(Path(tmp) / "nonexistent.yaml")
            self.assertEqual(cfg.output_dir, Path("./data"))
            self.assertFalse(cfg.proxy.get("enabled"))

    def test_request_interval_seconds(self):
        cfg = AppConfig(yt_dlp={"sleep_interval": 3})
        self.assertEqual(cfg.request_interval_seconds, 3.0)
        self.assertEqual(AppConfig().request_interval_seconds, 10.0)


class TestLoadChannelsKeywords(unittest.TestCase):
    def test_load_channels_filters_invalid_and_keeps_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "channels.yaml"
            path.write_text(
                "channels:\n"
                "  - url: https://www.youtube.com/@a/videos\n"
                "    alias: a\n"
                "    max_results: 5\n"
                "    enabled: false\n"
                "  - url: https://www.youtube.com/@b/videos\n"
                "  - alias: no-url\n",  # 无 url，应被过滤
                encoding="utf-8",
            )
            channels = load_channels(path)
            self.assertEqual(len(channels), 2)
            self.assertEqual(channels[0].alias, "a")
            self.assertFalse(channels[0].enabled)
            self.assertEqual(channels[0].max_results, 5)
            self.assertTrue(channels[1].enabled)  # 默认 enabled=True
            self.assertEqual(channels[1].max_results, 10)  # 默认 max_results

    def test_load_keywords_enabled_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keywords.yaml"
            path.write_text(
                "keywords:\n"
                "  - keyword: k1\n"
                "    enabled: true\n"
                "  - keyword: k2\n"
                "    enabled: false\n"
                "  - enabled: true\n",  # 无 keyword，应被过滤
                encoding="utf-8",
            )
            keywords = load_keywords(path)
            self.assertEqual([k.keyword for k in keywords], ["k1", "k2"])
            self.assertEqual([k.enabled for k in keywords], [True, False])


class TestResolveKeywords(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmp.name) / "keywords.yaml")
        Path(self.path).write_text(
            "keywords:\n"
            "  - keyword: cfg_a\n"
            "    enabled: true\n"
            "  - keyword: cfg_off\n"
            "    enabled: false\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_config_only_enabled(self):
        self.assertEqual(
            YTSearchContentFilter.resolve_keywords(self.path), ["cfg_a"]
        )

    def test_manual_appended_and_dedup(self):
        result = YTSearchContentFilter.resolve_keywords(
            self.path, manual_keywords=["cfg_a", "manual_b"]
        )
        self.assertEqual(result, ["cfg_a", "manual_b"])

    def test_override_ignores_config(self):
        result = YTSearchContentFilter.resolve_keywords(
            self.path, manual_keywords=["only"], override=True
        )
        self.assertEqual(result, ["only"])
        self.assertEqual(
            YTSearchContentFilter.resolve_keywords(self.path, override=True), []
        )


if __name__ == "__main__":
    unittest.main()
