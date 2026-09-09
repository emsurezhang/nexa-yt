"""后处理层：分片加载 → 留白拼接 → 峰值归一。

播客的节奏感在这里产生：按 kind 取留白（对话 0.35s / 章节 1.0s / 笑声更短）。
BGM 混音位预留：本期不用，接口见 mix_bgm。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from .config import RhythmConfig
from .models import AssembleError, Utterance


def assemble(
    segment_paths: dict[int, Path],
    utterances: list[Utterance],
    rhythm: RhythmConfig,
    sample_rate: int,
) -> np.ndarray:
    """按 seq 拼接并在片段间插入留白。

    缺失 seq 抛 AssembleError（宁可报错也不默默产出缺段节目）——
    调用方应重跑 scheduler 补片。
    """
    ordered = sorted(utterances, key=lambda u: u.seq)
    missing = [u.seq for u in ordered if u.seq not in segment_paths]
    if missing:
        raise AssembleError(
            f"缺少 {len(missing)} 个片段（seq={missing[:8]}{'…' if len(missing) > 8 else ''}），"
            "请先重跑合成补片"
        )

    parts: list[np.ndarray] = []
    for i, u in enumerate(ordered):
        wav, sr = sf.read(segment_paths[u.seq], dtype="float32")
        if sr != sample_rate:
            raise AssembleError(
                f"片段 [{u.seq:04d}] 采样率 {sr} ≠ 引擎采样率 {sample_rate}，拒绝拼接"
            )
        wav = wav.reshape(-1)
        if i > 0:
            parts.append(np.zeros(int(rhythm.gap_for(u.kind) * sample_rate), dtype=np.float32))
        parts.append(wav)

    if not parts:
        raise AssembleError("没有可拼接的片段")
    return np.concatenate(parts)


def normalize_peak(wav: np.ndarray, peak: float = 0.92) -> np.ndarray:
    """峰值归一到 0.92，留出编码余量。"""
    maximum = float(np.max(np.abs(wav))) if wav.size else 0.0
    if maximum <= 0:
        return wav
    return (wav * (peak / maximum)).astype(np.float32)


def mix_bgm(wav: np.ndarray, bgm: np.ndarray, bgm_gain: float = -20.0) -> np.ndarray:
    """BGM 混音预留接口（本期不用）。

    bgm_gain 为 dB；bgm 短于主音频时循环铺底。调用前需自行对齐采样率。
    """
    gain = float(10 ** (bgm_gain / 20.0))
    bgm = bgm * gain
    reps = int(np.ceil(len(wav) / max(len(bgm), 1)))
    tiled = np.tile(bgm, reps)[: len(wav)]
    return wav + tiled
