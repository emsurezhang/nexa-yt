"""scanner CLI 参数解析测试：--channel-urls 在 scan/agent 两个子命令均可用。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.yt_channel_scanner import _run
from src.types import ChannelScanOutput, ChannelResult
from datetime import datetime


def make_args(**over):
    base = dict(
        command="scan", config="config/channels.yaml", max_results=20,
        since=None, channel_url=None, channel_urls=None,
        output=None, output_stdout=True,
    )
    base.update(over)
    return type("Args", (), base)()


def make_output() -> ChannelScanOutput:
    return ChannelScanOutput(generated_at=datetime.now(), channels=[])


class TestScanChannelUrls(unittest.IsolatedAsyncioTestCase):
    async def _run_with_mock(self, args, captured: dict):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("src.yt_channel_scanner.load_app_config") as mcfg, \
             patch("src.yt_channel_scanner.YTChannelScanner") as mscanner_cls, \
             patch("src.yt_channel_scanner.load_channels", return_value=[]):
            from src.config_loader import AppConfig
            mcfg.return_value = AppConfig(output_dir=Path(tmp), proxy={"enabled": False})
            scanner = mscanner_cls.return_value
            scanner.scan_channels = AsyncMock(return_value=make_output())
            scanner.__aenter__ = AsyncMock(return_value=scanner)
            scanner.__aexit__ = AsyncMock(return_value=False)
            await _run(args)
            captured.update(scanner.scan_channels.call_args.kwargs)

    async def test_scan_with_channel_urls_json(self):
        captured: dict = {}
        await self._run_with_mock(
            make_args(channel_urls='["https://www.youtube.com/@a", "https://www.youtube.com/@b"]'),
            captured,
        )
        self.assertEqual(
            captured["channel_urls"],
            ["https://www.youtube.com/@a", "https://www.youtube.com/@b"],
        )
        # 指定了 --channel-urls 后不再加载配置文件频道
        self.assertEqual(captured["channels"], [])

    async def test_scan_channel_urls_priority_over_channel_url(self):
        captured: dict = {}
        await self._run_with_mock(
            make_args(channel_url="https://www.youtube.com/@single",
                      channel_urls='["https://www.youtube.com/@multi"]'),
            captured,
        )
        self.assertEqual(captured["channel_urls"], ["https://www.youtube.com/@multi"])

    async def test_scan_single_channel_url_fallback(self):
        captured: dict = {}
        await self._run_with_mock(
            make_args(channel_url="https://www.youtube.com/@single"),
            captured,
        )
        self.assertEqual(captured["channel_urls"], ["https://www.youtube.com/@single"])

    async def test_agent_subcommand_urls(self):
        captured: dict = {}
        await self._run_with_mock(
            make_args(command="agent", action="scan",
                      channel_urls='["https://www.youtube.com/@a"]'),
            captured,
        )
        self.assertEqual(captured["channel_urls"], ["https://www.youtube.com/@a"])
        self.assertFalse(captured["use_state"])


if __name__ == "__main__":
    unittest.main()
