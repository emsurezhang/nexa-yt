"""VoxCPM2 适配器——整个组件里唯一 import voxcpm 的模块。

换引擎只动 engine.py 与本文件的同类文件，其余各层无感知。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np

from ..config import DEFAULT_MODEL
from ..models import SpeakerProfile, SynthError
from ..voices import design_prompt
from .engine import TTSEngine  # noqa: F401  （类型契约声明）

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = 48_000


class VoxCPM2Engine:
    """VoxCPM2 引擎适配器。

    reference 模式：传 reference_wav_path=profile.ref_audio（音色克隆）；
    voice_design 模式：传 design_prompt(profile, text) 的结果（凭空造声）。
    """

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL,
        device: Optional[str] = None,        # None/"auto" → 自动选 cuda > mps > cpu
        cfg_value: float = 2.0,
        inference_timesteps: int = 10,
        load_denoiser: bool = False,
        optimize: bool = True,
    ):
        self._model_id = model_id
        self._device = None if device in (None, "", "auto") else device
        self._cfg_value = cfg_value
        self._inference_timesteps = inference_timesteps
        self._load_denoiser = load_denoiser
        self._optimize = optimize
        self._model = None
        self._sample_rate = DEFAULT_SAMPLE_RATE

    @property
    def sample_rate(self) -> int:
        """warmup 之前返回 VoxCPM2 标称 48k；加载后取模型真实值。"""
        if self._model is not None:
            return int(self._model.tts_model.sample_rate)
        return self._sample_rate

    def warmup(self) -> None:
        """加载模型权重（含 optimize 预热），耗时操作集中于此。"""
        if self._model is not None:
            return
        from voxcpm import VoxCPM  # 延迟到 warmup：--dry-run 路径绝不触碰模型

        model_path = Path(self._model_id).expanduser()
        if not model_path.is_dir():
            raise FileNotFoundError(
                f"VoxCPM2 本地模型目录不存在：{model_path}；"
                "请将 tts.model 配置为已下载的模型目录"
            )

        logger.info("加载本地 VoxCPM2：%s（device=%s）", model_path, self._device or "auto")
        self._model = VoxCPM(
            voxcpm_model_path=str(model_path),
            enable_denoiser=self._load_denoiser,
            device=self._device,
            optimize=self._optimize,
        )
        self._sample_rate = int(self._model.tts_model.sample_rate)

    def synth(self, text: str, profile: SpeakerProfile) -> np.ndarray:
        if self._model is None:
            raise SynthError("引擎未 warmup", utterance_seq=-1)

        kwargs: dict = dict(
            text=design_prompt(profile, text),
            cfg_value=self._cfg_value,
            inference_timesteps=self._inference_timesteps,
            normalize=False,
            denoise=self._load_denoiser,
        )
        if profile.mode == "reference":
            kwargs["reference_wav_path"] = str(profile.ref_audio)
        # voice_design：描述词已拼进 text，无需额外参数
        logger.debug("VoxCPM2 generate 参数：%s", kwargs)

        try:
            wav = self._model.generate(**kwargs)
        except SynthError:
            raise
        except Exception as exc:
            raise SynthError(f"VoxCPM2 合成失败：{exc}", utterance_seq=-1) from exc

        wav = np.asarray(wav, dtype=np.float32).reshape(-1)
        if wav.size == 0:
            raise SynthError("VoxCPM2 返回空音频", utterance_seq=-1)
        return wav

    def close(self) -> None:
        self._model = None
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # 清理尽力而为
            pass
