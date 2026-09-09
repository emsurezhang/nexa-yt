"""podcast_tts 共享数据契约（解析层与合成层之间的"通用货币"）。

不变量（调用方可以依赖）：
- Utterance.text 保证无 Markdown 残留、长度 ≤ max_seg_chars，合成器不再做清洗；
- SpeakerProfile 由 voices.py 构造时即完成校验，传给引擎时必然合法。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional


# ---------------------------------------------------------------- 台词单元

@dataclass(frozen=True)
class Utterance:
    seq: int                       # 全局序号，拼接顺序的唯一依据
    speaker: str                   # "A" | "B" | "N"，解析层已归一
    text: str                      # 已清洗纯文本，合成器的直接输入
    kind: Literal["dialog", "section", "note"]
                                   # dialog=普通对话；section=章节标题(更长留白)；
                                   # note=笑声/语气词(更短留白，可挂音效)


# ---------------------------------------------------------------- 音色档案

@dataclass(frozen=True)
class SpeakerProfile:
    speaker_id: str
    mode: Literal["reference", "voice_design"]
    design_text: Optional[str] = None   # mode=voice_design 时必填，含（）描述
    ref_audio: Optional[Path] = None    # mode=reference 时必填


# ---------------------------------------------------------------- 合成事件流

@dataclass(frozen=True)
class SynthEvent:
    """调度器向外广播的事件，CLI 打进度、未来 GUI 复用同一事件流。"""
    kind: Literal["started", "finished", "retried", "skipped", "cached"]
    seq: int
    speaker: str
    text: str
    attempt: int = 0
    duration_s: Optional[float] = None   # 片段音频时长（finished/cached 时有值）
    elapsed_s: Optional[float] = None    # 本次合成耗时
    message: str = ""


@dataclass
class SynthResult:
    segment_paths: dict[int, Path] = field(default_factory=dict)  # 成功片段
    skipped: list["SynthError"] = field(default_factory=list)     # 待处理清单


# ---------------------------------------------------------------- 清单

@dataclass(frozen=True)
class ManifestSegment:
    seq: int
    speaker: str
    kind: str
    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True)
class Manifest:
    segments: list[ManifestSegment]
    total_s: float
    sample_rate: int


# ---------------------------------------------------------------- 异常模型

class PodcastTTSError(Exception):
    """组件基类异常。"""


class ParseError(PodcastTTSError):
    """脚本解析失败（如零台词）。"""


class VoiceConfigError(PodcastTTSError):
    """音色定义非法（缺字段、参考音频不达标）。"""


class AssembleError(PodcastTTSError):
    """拼接阶段缺段，宁可报错也不默默产出缺段节目。"""


class EngineNotFoundError(PodcastTTSError):
    """未知引擎名。"""


class SynthError(PodcastTTSError):
    """单片段合成失败，调度器据此重试/跳过。

    引擎内部不做重试——重试归调度器管。
    """

    def __init__(self, message: str, *, utterance_seq: int, attempts: int = 0):
        super().__init__(message)
        self.utterance_seq = utterance_seq
        self.attempts = attempts
