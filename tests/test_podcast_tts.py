"""podcast_tts 单元测试：解析 / 配置 / 调度缓存 / 拼接 / 清单。

不加载 TTS 模型（引擎适配器为集成测试范畴）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf

from src.podcast_tts.config import DEFAULT_CONFIG, RhythmConfig, load_config
from src.podcast_tts.exporter import build_manifest
from src.podcast_tts.models import AssembleError, ParseError, Utterance
from src.podcast_tts.parser import default_replacer, parse_script
from src.podcast_tts.postprocess import assemble, normalize_peak
from src.podcast_tts.synth.scheduler import cache_path, run, text_hash
from src.podcast_tts.voices import design_prompt, DEFAULT_DESIGNS, SpeakerProfile

SPEAKERS = {"小硕": "A", "小丽": "B", "旁白": "N"}

SCRIPT = """# 标题（跳过）

> 引用（跳过）

## 【开场】

**小硕**：大家好，这期聊聊 $2M 的融资——以及背后的技术。

**小丽**：（笑）

---

**老王**：未知说话人，跳过。
"""


class ParserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.md = Path(self.tmp.name) / "script.md"
        self.md.write_text(SCRIPT, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_basic(self):
        utts = parse_script(self.md, SPEAKERS, narrate_sections=True)
        speakers = [u.speaker for u in utts]
        self.assertEqual(speakers, ["N", "A", "B"])          # 章节旁白 + 两句对话
        self.assertEqual(utts[0].kind, "section")
        self.assertEqual(utts[2].kind, "note")               # （笑）→ note
        self.assertNotIn("**", utts[1].text)                 # 清洗无 Markdown 残留
        self.assertIn("两百万", utts[1].text)                # $2M → 两百万
        self.assertNotIn("——", utts[1].text)                 # 破折号 → 停顿

    def test_parse_skip_sections(self):
        utts = parse_script(self.md, SPEAKERS, narrate_sections=False)
        self.assertEqual([u.speaker for u in utts], ["A", "B"])

    def test_parse_unknown_speaker_skipped(self):
        utts = parse_script(self.md, {"小硕": "A"}, narrate_sections=False)
        self.assertEqual([u.speaker for u in utts], ["A"])

    def test_parse_empty_raises(self):
        empty = Path(self.tmp.name) / "empty.md"
        empty.write_text("# 只有标题\n", encoding="utf-8")
        with self.assertRaises(ParseError):
            parse_script(empty, SPEAKERS)

    def test_long_text_split(self):
        long_md = Path(self.tmp.name) / "long.md"
        long_md.write_text("**小硕**：" + "第一句话，第二句话。" * 40 + "\n",
                           encoding="utf-8")
        utts = parse_script(long_md, SPEAKERS, max_seg_chars=100)
        self.assertGreater(len(utts), 1)
        self.assertTrue(all(len(u.text) <= 100 for u in utts))

    def test_replacer_custom(self):
        from src.podcast_tts.parser import RegexReplacer
        replacer = RegexReplacer([(r"融资", "天使轮融资")])
        utts = parse_script(self.md, SPEAKERS, replacer=replacer)
        self.assertTrue(any("天使轮" in u.text for u in utts))


class ConfigTest(unittest.TestCase):
    def _args(self, **kw):
        base = dict(script="s.md", output="o.mp3", config=None, speaker=None,
                    ref_A=None, ref_B=None, ref_N=None, gap=None,
                    section_gap=None, cache_dir=None, device=None, model=None,
                    bitrate=None, narrate_sections=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_cli_overrides_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = Path(tmp) / "podcast.yaml"
            yaml_path.write_text(
                "speakers: {小硕: A}\n"
                "rhythm: {dialog_gap: 0.5}\n"
                "tts: {model: some/model, device: cuda}\n",
                encoding="utf-8")
            cfg = load_config(self._args(config=yaml_path, device="cpu"))
            self.assertEqual(cfg.speakers, {"小硕": "A"})
            self.assertEqual(cfg.tts.model, "some/model")   # yaml 生效
            self.assertEqual(cfg.tts.device, "cpu")         # CLI 覆盖 yaml
            self.assertEqual(cfg.rhythm.dialog_gap, 0.5)

    def test_speaker_pair(self):
        # CLI --speaker 并入 yaml speakers（未指定 --config 时自动探测项目默认配置）
        cfg = load_config(self._args(speaker=["老王=A"]))
        self.assertEqual(cfg.speakers["老王"], "A")
        yaml_names = {"小硕", "小丽"} & set(cfg.speakers)   # yaml 映射仍在
        self.assertTrue(yaml_names or not DEFAULT_CONFIG.exists())

    def test_default_config_autodiscovery(self):
        # --config 未指定 → 自动探测项目 config/podcast.yaml（若仓库存在该文件）
        from src.podcast_tts.config import DEFAULT_CONFIG
        if not DEFAULT_CONFIG.exists():
            self.skipTest("仓库无 config/podcast.yaml，跳过自动探测用例")
        cfg = load_config(self._args())
        self.assertEqual(cfg.speakers.get("小硕"), "A")   # yaml 的 speakers 生效
        self.assertIn("A", cfg.voices)                    # yaml 的 voices 生效
        self.assertEqual(cfg.voices["A"].mode, "reference")

    def test_missing_yaml_ok(self):
        cfg = load_config(self._args(config=Path("/nonexistent/x.yaml")))
        self.assertTrue(cfg.tts.model.endswith("models/VoxCPM2"))


class FakeEngine:
    """记录调用、返回短静音的桩引擎。"""
    sample_rate = 48000

    def __init__(self, fail_seqs=()):
        self.calls: list[tuple[str, str]] = []
        self._fail = set(fail_seqs)

    def warmup(self):
        pass

    def synth(self, text, profile):
        if text in self._fail:
            from src.podcast_tts.models import SynthError
            raise SynthError("模拟失败", utterance_seq=-1)
        self.calls.append((text, profile.speaker_id))
        return np.zeros(4800, dtype=np.float32)  # 0.1s 静音

    def close(self):
        pass


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "cache"
        self.profiles = {s: SpeakerProfile(s, "voice_design", design_text=DEFAULT_DESIGNS[s])
                         for s in ("A", "B", "N")}
        self.utts = [
            Utterance(0, "A", "第一句。", "dialog"),
            Utterance(1, "B", "第二句。", "dialog"),
            Utterance(2, "A", "第三句。", "dialog"),
        ]
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, engine, **kw):
        return run(self.utts, self.profiles, engine, self.cache,
                   on_event=self.events.append, **kw)

    def test_cache_key_stable_and_sensitive(self):
        u = self.utts[0]
        p1 = cache_path(self.cache, u)
        self.assertEqual(p1.name, f"0000_A_{text_hash(u.text)[:8]}.wav")
        u2 = Utterance(0, "A", "另一句。", "dialog")   # 改词 → hash 变
        self.assertNotEqual(cache_path(self.cache, u2), p1)
        u3 = Utterance(0, "B", u.text, "dialog")      # 换说话人 → 文件名变
        self.assertNotEqual(cache_path(self.cache, u3), p1)

    def test_caching_and_resume(self):
        engine = FakeEngine()
        r1 = self._run(engine)
        self.assertEqual(len(r1.segment_paths), 3)
        self.assertEqual(len(engine.calls), 3)

        engine2 = FakeEngine()                        # 重跑：全部命中缓存，零合成
        r2 = self._run(engine2)
        self.assertEqual(len(r2.segment_paths), 3)
        self.assertEqual(engine2.calls, [])
        kinds = [e.kind for e in self.events]
        self.assertEqual(kinds.count("cached"), 3)

    def test_retry_then_skip(self):
        engine = FakeEngine(fail_seqs={"第二句。"})
        r = self._run(engine, max_retries=1)
        self.assertEqual(len(r.skipped), 1)
        self.assertEqual(r.skipped[0].utterance_seq, 1)
        self.assertEqual(r.skipped[0].attempts, 2)    # 首次 + 1 次重试
        self.assertEqual(len(r.segment_paths), 2)     # 其余照常完成

    def test_fresh_regenerates_all(self):
        engine = FakeEngine()
        self._run(engine)
        engine2 = FakeEngine()
        r = self._run(engine2, force=True)            # --fresh：不依赖任何缓存
        self.assertEqual(len(engine2.calls), 3)       # 全部片段重新合成
        self.assertEqual(len(r.segment_paths), 3)

    def test_redo_only_seqs(self):
        engine = FakeEngine()
        self._run(engine)
        engine2 = FakeEngine()
        r = self._run(engine2, force=True, only_seqs=[1])
        self.assertEqual([c[0] for c in engine2.calls], ["第二句。"])
        self.assertEqual(len(r.segment_paths), 3)     # 其余片段沿用缓存


class PostprocessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.rhythm = RhythmConfig(dialog_gap=0.35, section_gap=1.0, note_gap=0.15)
        self.utts = [
            Utterance(0, "A", "一", "dialog"),
            Utterance(1, "B", "二", "section"),
            Utterance(2, "A", "（笑）", "note"),
        ]
        self.paths = {}
        for u in self.utts:
            p = self.dir / f"{u.seq}.wav"
            sf.write(p, np.full(4800, 0.5, dtype=np.float32), 48000)
            self.paths[u.seq] = p

    def tearDown(self):
        self.tmp.cleanup()

    def test_assemble_gaps(self):
        wav = assemble(self.paths, self.utts, self.rhythm, 48000)
        expected = 3 * 4800 + int(1.0 * 48000) + int(0.15 * 48000)
        self.assertEqual(len(wav), expected)

    def test_assemble_missing_raises(self):
        del self.paths[1]
        with self.assertRaises(AssembleError):
            assemble(self.paths, self.utts, self.rhythm, 48000)

    def test_normalize_peak(self):
        wav = np.array([0.5, -1.0, 0.25], dtype=np.float32)
        out = normalize_peak(wav, peak=0.92)
        self.assertAlmostEqual(float(np.max(np.abs(out))), 0.92, places=5)

    def test_manifest_timestamps(self):
        wav = assemble(self.paths, self.utts, self.rhythm, 48000)
        m = build_manifest(self.utts, self.paths, 48000, self.rhythm)
        self.assertAlmostEqual(m.total_s, len(wav) / 48000, places=3)
        self.assertEqual(m.segments[0].start_s, 0.0)
        self.assertAlmostEqual(m.segments[1].start_s, 0.1 + 1.0, places=3)  # 首段 0.1s + section 留白


class VoicesTest(unittest.TestCase):
    def test_design_prompt(self):
        p = SpeakerProfile("B", "voice_design", design_text="（清亮女声）")
        self.assertEqual(design_prompt(p, "你好"), "(清亮女声)你好")
        p3 = SpeakerProfile("A", "voice_design", design_text="(warm voice)")
        self.assertEqual(design_prompt(p3, "你好"), "(warm voice)你好")
        p2 = SpeakerProfile("A", "reference", ref_audio=Path("/x.wav"))
        self.assertEqual(design_prompt(p2, "你好"), "你好")


if __name__ == "__main__":
    unittest.main()
