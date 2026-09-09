"""输出层：mp3 写出（soundfile 直写 │ ffmpeg 回退）+ manifest.json。

合规：按 Apache-2.0 要求在元数据里写入 "AI synthesized" 标注。
manifest 提供每段起止时间戳——以后做 "Shownotes 跳转链接" 直接复用。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path

import numpy as np
import soundfile as sf

from .config import RhythmConfig
from .models import Manifest, ManifestSegment, Utterance

logger = logging.getLogger(__name__)

AI_NOTICE = "AI synthesized speech (VoxCPM2, Apache-2.0). Not voiced by humans."


def build_manifest(
    utterances: list[Utterance],
    segment_paths: dict[int, Path],
    sample_rate: int,
    rhythm: RhythmConfig,
) -> Manifest:
    """产出 {segments: [{seq, speaker, kind, text, start_s, end_s}], total_s}。"""
    segments: list[ManifestSegment] = []
    cursor = 0.0
    for u in sorted(utterances, key=lambda x: x.seq):
        path = segment_paths[u.seq]
        duration = sf.info(path).duration
        if segments:  # 片段前留白（首段不留）
            cursor += rhythm.gap_for(u.kind)
        start = cursor
        cursor += duration
        segments.append(ManifestSegment(
            seq=u.seq, speaker=u.speaker, kind=u.kind, text=u.text,
            start_s=round(start, 3), end_s=round(cursor, 3),
        ))
    return Manifest(segments=segments, total_s=round(cursor, 3), sample_rate=sample_rate)


def _ffprobe() -> str | None:
    return shutil.which("ffmpeg")


def _ffmpeg_write(wav: np.ndarray, sample_rate: int, out_path: Path,
                  bitrate: str, metadata: dict[str, str]) -> None:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_wav = Path(tmp.name)
    try:
        sf.write(tmp_wav, wav, sample_rate, subtype="PCM_16")
        cmd = [_ffprobe(), "-y", "-i", str(tmp_wav), "-codec:a", "libmp3lame",
               "-b:a", bitrate]
        for k, v in metadata.items():
            cmd += ["-metadata", f"{k}={v}"]
        cmd += [str(out_path)]
        subprocess.run(cmd, check=True, capture_output=True)
    finally:
        tmp_wav.unlink(missing_ok=True)


def _inject_id3(path: Path, metadata: dict[str, str]) -> None:
    """soundfile 通道补上 ID3 标签（尽力而为，失败仅告警）。"""
    if _ffprobe() is None:
        logger.warning("无 ffmpeg，mp3 未写入 ID3 元数据")
        return
    tmp = path.with_suffix(".tagged.mp3")
    cmd = [_ffprobe(), "-y", "-i", str(path), "-codec", "copy", "-id3v2_version", "3"]
    for k, v in metadata.items():
        cmd += ["-metadata", f"{k}={v}"]
    cmd += [str(tmp)]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        tmp.replace(path)
    except subprocess.CalledProcessError as exc:
        logger.warning("ID3 标签写入失败（保留无声卡数据文件）：%s", exc)


def export(
    wav: np.ndarray,
    sample_rate: int,
    out_path: Path,
    manifest: Manifest,
    *,
    bitrate: str = "192k",
    title: str | None = None,
    artist: str | None = None,
) -> Path:
    """mp3 双通道：soundfile 直写，失败回退 ffmpeg，再失败改产 wav 并 warn。

    同时在 out_path 旁写出 manifest.json。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {"title": title or out_path.stem,
                "artist": artist or "podcast_tts",
                "comment": AI_NOTICE}

    # manifest 永远落盘（即使音频导出失败也可复用时间戳）
    manifest_path = out_path.with_suffix(".manifest.json")
    manifest_path.write_text(
        json.dumps(asdict(manifest), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    try:  # 通道一：soundfile 直写（需要 libsndfile ≥ 1.1 的 mp3 支持）
        # soundfile 用 compression_level（0=高码率…1=低码率）而非 bitrate 参数
        try:
            kbps = int(bitrate.rstrip("kK"))
            level = max(0.0, min(1.0, 1.0 - kbps / 320.0))
        except ValueError:
            level = 0.4  # 解析不了就按 192k 档
        sf.write(out_path, wav, sample_rate, format="MP3", compression_level=level)
        _inject_id3(out_path, metadata)
        logger.info("已导出 %s（soundfile 通道）", out_path)
        return out_path
    except Exception as exc:
        logger.warning("soundfile mp3 写出失败（%s），回退 ffmpeg", exc)

    if _ffprobe() is not None:
        try:  # 通道二：ffmpeg
            _ffmpeg_write(wav, sample_rate, out_path, bitrate, metadata)
            logger.info("已导出 %s（ffmpeg 通道）", out_path)
            return out_path
        except Exception as exc:
            logger.warning("ffmpeg mp3 写出失败（%s），改产 wav", exc)

    wav_path = out_path.with_suffix(".wav")  # 兜底：wav 保底
    sf.write(wav_path, wav, sample_rate, subtype="PCM_16")
    logger.warning("mp3 双通道均失败，已改产 %s（可用 ffmpeg 手动转码）", wav_path)
    return wav_path
