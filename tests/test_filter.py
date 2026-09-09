"""Rubric 内容过滤引擎测试。"""
from __future__ import annotations

import unittest

from src.types import YouTubeItem
from src.yt_scf import KeywordRubricFilter, RubricDimension


def make_filter(**over) -> KeywordRubricFilter:
    base = dict(
        dimensions=[
            RubricDimension("主题相关", ["AI", "agent"]),
            RubricDimension("技术深度", ["tutorial", "deep dive"]),
            RubricDimension("时效性", ["2025", "latest"]),
        ],
        match_threshold=0.6,
        needs_review_threshold=0.3,
    )
    base.update(over)
    return KeywordRubricFilter(**base)


def make_item(title: str, description: str = "") -> YouTubeItem:
    return YouTubeItem(url="u", title=title, description=description)


class TestKeywordRubricFilter(unittest.IsolatedAsyncioTestCase):
    async def test_matched(self):
        verdict = await make_filter().evaluate(
            "AI", make_item("AI agent tutorial", "latest 2025")
        )
        self.assertEqual(verdict.status, "matched")
        self.assertEqual(verdict.score, 1.0)
        self.assertTrue(all(verdict.dimensions.values()))

    async def test_needs_review(self):
        # 1/3 维度命中 = 0.333，介于 0.3 ~ 0.6
        verdict = await make_filter().evaluate("AI", make_item("聊聊 AI 产品"))
        self.assertEqual(verdict.status, "needs_review")
        self.assertAlmostEqual(verdict.score, 1 / 3, places=3)

    async def test_not_matched(self):
        verdict = await make_filter().evaluate("AI", make_item("烹饪美食日常"))
        self.assertEqual(verdict.status, "not_matched")
        self.assertEqual(verdict.score, 0.0)

    async def test_case_insensitive(self):
        verdict = await make_filter().evaluate(
            "AI", make_item("AI AGENT TUTORIAL")
        )
        self.assertEqual(verdict.status, "matched")

    async def test_empty_dimensions_returns_needs_review(self):
        verdict = await KeywordRubricFilter([]).evaluate("k", make_item("t"))
        self.assertEqual(verdict.status, "needs_review")
        self.assertEqual(verdict.score, 0.0)
        self.assertIn("未配置", verdict.reason)

    async def test_from_config(self):
        cfg = {
            "match_threshold": 0.5,
            "needs_review_threshold": 0.2,
            "rubric": [
                {"dimension": "d1", "keywords": ["a", 1]},
                "not-a-dict",  # 应被忽略
                {"keywords": ["x"]},  # dimension 缺省，自动生成 dim_2
            ],
        }
        engine = KeywordRubricFilter.from_config(cfg)
        self.assertEqual(engine.match_threshold, 0.5)
        self.assertEqual(len(engine.dimensions), 2)
        self.assertEqual(engine.dimensions[0].keywords, ["a", "1"])
        self.assertEqual(engine.dimensions[1].dimension, "dim_2")


if __name__ == "__main__":
    unittest.main()
