"""CLI 入口：只做参数装配，不含业务逻辑。

典型工作流：
  1. podcast_tts script.md --dry-run          # 只解析、打印片段清单，不加载模型
  2. podcast_tts script.md -o out.mp3 --limit 3   # 试听音色
  3. podcast_tts script.md -o out.mp3             # 全量跑，中断重跑自动续
  4. podcast_tts script.md -o out.mp3 --redo 12   # Voice Design 音色不满意，单片段重抽
  5. podcast_tts script.md -o out.mp3 --fresh     # 弃用全部缓存，整期重新生成
"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import AppConfig, load_config
from .exporter import build_manifest, export
from .models import PodcastTTSError
from .parser import parse_script
from .postprocess import assemble, normalize_peak
from .synth import load_engine
from .synth.scheduler import run as scheduler_run
from .voices import build_profiles

logger = logging.getLogger("podcast_tts")

# 零配置可用的默认说话人映射（yaml/CLI 可覆盖；圆桌 3+ 人只需加映射）
DEFAULT_SPEAKERS = {"小硕": "A", "小丽": "B", "旁白": "N"}


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="podcast_tts",
        description="Markdown 对话脚本 → 播客 mp3（VoxCPM2）",
    )
    p.add_argument("script", help="双人对话 Markdown 脚本（**名字**：台词）")
    p.add_argument("-o", "--output", required=True, help="输出 mp3 路径")
    p.add_argument("--config", help="podcast.yaml 配置路径（默认自动探测项目 config/podcast.yaml）")
    p.add_argument("--speaker", action="append", metavar="名字=ID",
                   help="说话人映射，可重复（--speaker 老王=A）")
    p.add_argument("--ref-A", "--ref-a", dest="ref_A", help="A 参考音频（克隆）")
    p.add_argument("--ref-B", "--ref-b", dest="ref_B", help="B 参考音频（克隆）")
    p.add_argument("--ref-N", "--ref-n", dest="ref_N", help="N 旁白参考音频（克隆）")
    p.add_argument("--narrate-sections", action="store_true",
                   help="## 【章节】由 N 旁白口播（默认跳过）")
    p.add_argument("--gap", type=float, help="对话留白秒数（默认 0.35）")
    p.add_argument("--section-gap", type=float, help="章节留白秒数（默认 1.0）")
    p.add_argument("--dry-run", action="store_true",
                   help="只解析并打印片段清单，不加载模型")
    p.add_argument("--limit", type=int, metavar="N", help="只合成前 N 段（试听音色）")
    regen = p.add_mutually_exclusive_group()
    regen.add_argument("--redo", type=int, action="append", metavar="SEQ",
                       help="片段级重生成（可重复），Voice Design 音色抽签不满意时用")
    regen.add_argument("--fresh", action="store_true",
                       help="忽略所有缓存，全部片段重新生成（Voice Design 音色重新抽签）")
    p.add_argument("--cache-dir", help="分片缓存目录（默认 .cache/podcast_tts）")
    p.add_argument("--device", help="cuda | mps | cpu（默认 auto）")
    p.add_argument("--model", help="本地 VoxCPM2 模型目录（默认项目内 models/VoxCPM2）")
    p.add_argument("--bitrate", help="mp3 码率（默认 192k）")
    p.add_argument("-v", "--verbose", action="store_true", help="debug 日志")
    return p


def _print_utterances(utterances, cfg: AppConfig) -> None:
    print(f"\n共 {len(utterances)} 个片段（max_seg_chars={cfg.max_seg_chars}）：")
    for u in utterances:
        marker = {"dialog": "  ", "section": "##", "note": "♪ "}[u.kind]
        text = u.text if len(u.text) <= 40 else u.text[:40] + "…"
        print(f"  [{u.seq:04d}] {marker} {u.speaker}｜{text}")
    print("\n--dry-run：未加载模型。确认无误后去掉 --dry-run 跑 --limit 3 试听音色。")


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        cfg = load_config(args)
        if not cfg.speakers:
            logger.warning("未配置 speaker 映射，使用内置默认：%s", DEFAULT_SPEAKERS)
            cfg.speakers = dict(DEFAULT_SPEAKERS)
        if args.bitrate:
            cfg.bitrate = args.bitrate

        # ---- 解析层 ----
        utterances = parse_script(
            cfg.script, cfg.speakers,
            narrate_sections=cfg.narrate_sections,
            max_seg_chars=cfg.max_seg_chars,
        )
        if args.dry_run:
            _print_utterances(utterances, cfg)
            return 0

        targets = utterances[: args.limit] if args.limit else utterances

        # ---- 音色层 + 合成层 ----
        profiles = build_profiles(cfg.voices)
        engine = load_engine(
            "voxcpm2",
            model_id=cfg.tts.model,
            device=cfg.tts.device,
            cfg_value=cfg.tts.cfg_value,
            inference_timesteps=cfg.tts.inference_timesteps,
            load_denoiser=cfg.tts.load_denoiser,
            optimize=cfg.tts.optimize,
        )
        try:
            engine.warmup()
            sample_rate = engine.sample_rate  # close() 后可能取不到，先固化
            # --fresh：全量重生成（force 且不限定片段）；--redo：单片段重抽
            only_seqs = None if args.fresh else args.redo
            result = scheduler_run(
                targets, profiles, engine, cfg.cache_dir,
                force=bool(args.redo) or args.fresh,
                only_seqs=only_seqs,
            )
        finally:
            engine.close()

        # ---- 后处理层 + 输出层 ----
        wav = assemble(result.segment_paths, targets, cfg.rhythm, sample_rate)
        wav = normalize_peak(wav, peak=0.92)
        manifest = build_manifest(targets, result.segment_paths,
                                  sample_rate, cfg.rhythm)
        out = export(wav, sample_rate, cfg.output, manifest,
                     bitrate=cfg.bitrate, title=cfg.title, artist=cfg.artist)

        # ---- 汇总 ----
        print(f"\n完成：{out}（{manifest.total_s / 60:.1f} 分钟，{len(result.segment_paths)} 段）")
        if result.skipped:
            print(f"⚠ {len(result.skipped)} 个片段失败待处理（--redo 重跑）：")
            for err in result.skipped:
                print(f"  --redo {err.utterance_seq}    {err}")
            return 2
        return 0

    except PodcastTTSError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断：缓存已落盘，重跑同一命令即自动续跑。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
