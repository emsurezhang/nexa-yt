"""引擎抽象协议（适配器接口）——未来换 EdgeTTS / CosyVoice 只实现这一个协议。

铁律：本模块不得 import voxcpm；唯一允许 import voxcpm 的是 voxcpm2.py。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

from ..models import EngineNotFoundError, SpeakerProfile


@runtime_checkable
class TTSEngine(Protocol):
    """单片段合成接口：synth(text, profile) → wav。

    任何失败抛 SynthError；引擎内部不做重试（重试归调度器管）。
    """

    @property
    def sample_rate(self) -> int: ...

    def warmup(self) -> None:
        """加载模型权重。耗时操作集中于此，调度器只调一次。"""
        ...

    def synth(self, text: str, profile: SpeakerProfile) -> np.ndarray:
        """返回 float32 单声道波形。"""
        ...

    def close(self) -> None:
        """释放资源；无资源可释放时为空操作。调度器保证调用（有则调）。"""
        ...


def load_engine(name: str = "voxcpm2", **kwargs) -> TTSEngine:
    """工厂：按名字返回适配器。未知名字抛 EngineNotFoundError。"""
    if name == "voxcpm2":
        from .voxcpm2 import VoxCPM2Engine

        return VoxCPM2Engine(**kwargs)
    raise EngineNotFoundError(
        f"未知 TTS 引擎：{name}（当前可用：voxcpm2）"
    )
