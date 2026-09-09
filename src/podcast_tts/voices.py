"""音色层：谈话类节目的灵魂，单独一层而不是塞进合成层。

三种模式优先级：参考音频克隆 > Voice Design 兜底（片段级重生成补音色波动）。
已知风险：Voice Design 每次合成音色有波动，官方建议重生成 1–3 次——
合成层因此支持单片段重跑（--redo SEQ）。
"""
from __future__ import annotations

import logging
from pathlib import Path

import soundfile as sf

from .config import VoiceEntryConfig
from .models import SpeakerProfile, VoiceConfigError

logger = logging.getLogger(__name__)

# 内置 Voice Design 描述词库（A/B/N 各一条），可在 podcast.yaml 覆盖
DEFAULT_DESIGNS: dict[str, str] = {
    "A": "(热情洋溢的中年男性播音员，声音较为低沉，富有磁性与感染力，语速适中，适合深夜电台节目)",
    "B": "(清亮活泼的年轻女性，声音明快有感染力，语速稍快，反应灵动，表达自然)",
    "N": "(中性平稳的旁白员，吐字清晰，语气客观，不带明显感情色彩，适合历史与知识类叙述)",
}

# 参考音频门槛：克隆质量取决于此，不接受凑合
REF_MIN_SECONDS = 5.0
REF_MAX_SECONDS = 60.0
REF_MIN_SAMPLE_RATE = 16_000


def validate_ref_audio(path: Path) -> None:
    """参考音频校验：可读、时长 5–15s、采样率 ≥ 16k。

    不达标即抛 VoiceConfigError。非 wav 格式只要能被 soundfile 读取也接受，
    但会 warning（克隆音源以无损 wav 为佳）。
    """
    path = Path(path)
    if not path.exists():
        raise VoiceConfigError(f"参考音频不存在：{path}")
    if path.suffix.lower() != ".wav":
        logger.warning("参考音频 %s 非 wav 格式，建议改用无损 wav", path.name)
    try:
        info = sf.info(path)
    except Exception as exc:  # soundfile 抛多种底层异常
        raise VoiceConfigError(f"参考音频无法读取：{path}（{exc}）") from exc

    duration = info.duration
    if not (REF_MIN_SECONDS <= duration <= REF_MAX_SECONDS):
        raise VoiceConfigError(
            f"参考音频 {path.name} 时长 {duration:.1f}s，要求 {REF_MIN_SECONDS}–{REF_MAX_SECONDS}s"
        )
    if info.samplerate < REF_MIN_SAMPLE_RATE:
        raise VoiceConfigError(
            f"参考音频 {path.name} 采样率 {info.samplerate}Hz，要求 ≥ {REF_MIN_SAMPLE_RATE}Hz"
        )
    if info.channels > 1:
        logger.warning("参考音频 %s 为多声道，克隆建议使用单声道", path.name)


def build_profiles(
    cfg: dict[str, VoiceEntryConfig],
    defaults: dict[str, str] | None = None,
) -> dict[str, SpeakerProfile]:
    """构建全部说话人档案。校验失败抛 VoiceConfigError，信息指明是谁、缺什么。

    允许混合模式：A 用克隆、B 用 Design，逐人独立。
    缺少条目但有内置默认描述词的 ID，自动以 Voice Design 兜底并 warning。
    """
    defaults = defaults or DEFAULT_DESIGNS
    profiles: dict[str, SpeakerProfile] = {}

    for sid, entry in cfg.items():
        if entry.mode == "reference":
            if not entry.ref_audio:
                raise VoiceConfigError(f"voices.{sid} mode=reference 但未提供 ref_audio")
            ref = Path(entry.ref_audio)
            validate_ref_audio(ref)
            profiles[sid] = SpeakerProfile(speaker_id=sid, mode="reference", ref_audio=ref)
        elif entry.mode == "voice_design":
            design = entry.design or defaults.get(sid)
            if not design:
                raise VoiceConfigError(f"voices.{sid} mode=voice_design 且无内置默认描述词")
            if not (design.startswith("（") or design.startswith("(")):
                logger.warning("voices.%s design 建议用（）包裹描述词：%s", sid, design)
            profiles[sid] = SpeakerProfile(speaker_id=sid, mode="voice_design", design_text=design)
        else:
            raise VoiceConfigError(f"voices.{sid} mode 非法：{entry.mode}（应为 reference / voice_design）")

    return profiles


def ensure_profile(profiles: dict[str, SpeakerProfile], speaker: str) -> SpeakerProfile:
    """取说话人档案；缺档时按内置默认以 Voice Design 兜底（warning 留痕）。"""
    profile = profiles.get(speaker)
    if profile is None:
        design = DEFAULT_DESIGNS.get(speaker)
        if design is None:
            raise VoiceConfigError(
                f"说话人 {speaker} 无音色档且无内置默认（内置仅有：{sorted(DEFAULT_DESIGNS)}）"
            )
        logger.warning("说话人 %s 未配置音色，使用内置 Voice Design 兜底：%s", speaker, design)
        profile = SpeakerProfile(speaker_id=speaker, mode="voice_design", design_text=design)
        profiles[speaker] = profile
    return profile


def design_prompt(profile: SpeakerProfile, text: str) -> str:
    """组装最终送 TTS 的文本。

    Design 模式拼 "(描述)+正文"；VoxCPM2 用 ASCII 括号识别描述词，
    reference 模式原样返回正文。
    引擎层调用，调用方不需要懂拼接规则。
    """
    if profile.mode == "voice_design":
        design = profile.design_text or ""
        if design.startswith("（"):
            design = "(" + design[1:]
        if design.endswith("）"):
            design = design[:-1] + ")"
        return f"{design}{text}"
    return text
