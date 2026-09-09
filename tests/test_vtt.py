"""VTT 字幕解析测试（纯函数，离线）。"""
from __future__ import annotations

import unittest

from src.yt_video_detail_fetcher import _parse_timestamp, parse_vtt

SAMPLE_VTT = """\
WEBVTT
Kind: captions
Language: zh-CN

00:00:01.000 --> 00:00:03.500
<c>你好</c>，世界

00:00:03.500 --> 00:00:06.000
你好，世界

00:00:06.000 --> 00:00:09.000
3
重复行
重复行

00:00:09.000 --> 00:00:12.000
&nbsp;含空格&nbsp;

"""


class TestParseTimestamp(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(_parse_timestamp("00:00:01.000"), 1.0)
        self.assertEqual(_parse_timestamp("01:02:03.500"), 3723.5)


class TestParseVtt(unittest.TestCase):
    def test_basic_segments(self):
        segments = parse_vtt(SAMPLE_VTT)
        self.assertEqual(len(segments), 4)
        self.assertEqual(segments[0].start, 1.0)
        self.assertEqual(segments[0].end, 3.5)
        # <c> 标签被清理
        self.assertEqual(segments[0].text, "你好，世界")

    def test_consecutive_duplicate_lines_merged(self):
        segments = parse_vtt(SAMPLE_VTT)
        # 第三段两行重复文本只保留一次
        self.assertEqual(segments[2].text, "重复行")

    def test_nbsp_replaced(self):
        segments = parse_vtt(SAMPLE_VTT)
        self.assertEqual(segments[3].text, "含空格")

    def test_bom_and_cue_identifier(self):
        vtt = "\ufeffWEBVTT\n\n1\n00:00:00.500 --> 00:00:02.000\nidentifier 行外文本\n"
        segments = parse_vtt(vtt)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].start, 0.5)
        # 纯数字 cue 序号行被剔除
        self.assertNotIn("1", segments[0].text.split())

    def test_empty_cue_skipped(self):
        vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n\n00:00:01.000 --> 00:00:02.000\n有内容\n"
        segments = parse_vtt(vtt)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].text, "有内容")

    def test_header_only_returns_empty(self):
        self.assertEqual(parse_vtt("WEBVTT\n"), [])


if __name__ == "__main__":
    unittest.main()
