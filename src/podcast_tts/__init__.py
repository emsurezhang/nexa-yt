"""podcast_tts：Markdown 对话脚本 → 48kHz 播客 mp3（VoxCPM2 引擎）。

六层流水线：解析 → 音色 → 合成（分片+断点续跑）→ 后处理 → 输出。
"""
from .models import (
    AssembleError,
    EngineNotFoundError,
    Manifest,
    ManifestSegment,
    ParseError,
    PodcastTTSError,
    SpeakerProfile,
    SynthError,
    SynthEvent,
    SynthResult,
    Utterance,
    VoiceConfigError,
)

__all__ = [
    "Utterance", "SpeakerProfile", "SynthEvent", "SynthResult",
    "Manifest", "ManifestSegment",
    "PodcastTTSError", "ParseError", "VoiceConfigError", "SynthError",
    "AssembleError", "EngineNotFoundError",
]

__version__ = "0.1.0"
