"""调度器：遍历、缓存判断、失败重试、断点续跑——本程序的"成本中心"。

缓存 key = 序号 + 说话人 + 文本哈希：改一个词只重合成一个词，这是成本核心。
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

import soundfile as sf

from ..models import SpeakerProfile, SynthError, SynthEvent, SynthResult, Utterance
from ..voices import ensure_profile
from .engine import TTSEngine

logger = logging.getLogger(__name__)


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def cache_path(cache_dir: Path, u: Utterance) -> Path:
    """命名契约：{seq:04d}_{speaker}_{hash8}.wav

    seq 保证顺序、speaker 保证换音色必重合成、hash8 保证改词必重合成。
    """
    return Path(cache_dir) / f"{u.seq:04d}_{u.speaker}_{text_hash(u.text)[:8]}.wav"


def print_event(event: SynthEvent) -> None:
    """默认事件回调：终端进度。"""
    if event.kind == "started":
        print(f"  ▶ [{event.seq:04d}] {event.speaker} {event.text[:28]}…", flush=True)
    elif event.kind == "finished":
        print(f"  ✓ [{event.seq:04d}] {event.duration_s:.1f}s（{event.elapsed_s:.0f}s）", flush=True)
    elif event.kind == "cached":
        print(f"  ○ [{event.seq:04d}] 命中缓存", flush=True)
    elif event.kind == "retried":
        print(f"  ↻ [{event.seq:04d}] 第 {event.attempt} 次重试：{event.message}", flush=True)
    elif event.kind == "skipped":
        print(f"  ✗ [{event.seq:04d}] 已跳过：{event.message}", flush=True)


def _wav_readable(path: Path, sample_rate: int) -> bool:
    try:
        info = sf.info(path)
    except Exception:
        return False
    return info.frames > 0 and info.samplerate == sample_rate


def run(
    utterances: list[Utterance],
    profiles: dict[str, SpeakerProfile],
    engine: TTSEngine,
    cache_dir: Path,
    *,
    max_retries: int = 2,
    on_event: Callable[[SynthEvent], None] = print_event,
    force: bool = False,
    only_seqs: Optional[Iterable[int]] = None,
) -> SynthResult:
    """遍历 utterances：命中缓存→校验；缺片→合成；失败→重试→跳过。

    - 单片段失败重试 max_retries 次后标记跳过并记录清单，绝不中断整期节目；
    - only_seqs：只处理这些 seq（--redo 片段级重生成），其余 seq 的已有缓存
      仍会被收集进结果，供后续整段拼接；
    - force：忽略缓存命中，强制重新合成。配合 only_seqs 为片段级重抽（--redo），
      不限定片段为全量重生成（--fresh，弃用所有缓存）。
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    only = set(only_seqs) if only_seqs is not None else None

    result = SynthResult()
    for u in utterances:
        path = cache_path(cache_dir, u)

        if only is not None and u.seq not in only:
            if path.exists():  # 重跑模式下，未涉及片段沿用缓存
                result.segment_paths[u.seq] = path
            continue

        if not force and path.exists() and _wav_readable(path, engine.sample_rate):
            info = sf.info(path)
            on_event(SynthEvent("cached", u.seq, u.speaker, u.text,
                                duration_s=info.duration))
            result.segment_paths[u.seq] = path
            continue

        profile = ensure_profile(profiles, u.speaker)
        error: Optional[SynthError] = None
        for attempt in range(1, max_retries + 2):  # 首次 + max_retries 次重试
            on_event(SynthEvent("started", u.seq, u.speaker, u.text, attempt=attempt))
            t0 = time.monotonic()
            try:
                wav = engine.synth(u.text, profile)
            except SynthError as exc:
                error = SynthError(str(exc), utterance_seq=u.seq, attempts=attempt)
                on_event(SynthEvent("retried", u.seq, u.speaker, u.text,
                                    attempt=attempt, message=str(exc)))
                continue
            elapsed = time.monotonic() - t0
            sf.write(path, wav, engine.sample_rate, subtype="FLOAT")
            on_event(SynthEvent("finished", u.seq, u.speaker, u.text,
                                attempt=attempt, duration_s=len(wav) / engine.sample_rate,
                                elapsed_s=elapsed))
            result.segment_paths[u.seq] = path
            error = None
            break

        if error is not None:
            on_event(SynthEvent("skipped", u.seq, u.speaker, u.text,
                                attempt=max_retries + 1, message=str(error)))
            result.skipped.append(error)

    return result
