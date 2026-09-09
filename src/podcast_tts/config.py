"""配置加载与合并：CLI 参数 > podcast.yaml > 代码默认值。

voices 段全空时抛 VoiceConfigError——没有音色定义的节目不产出。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import yaml

from .models import VoiceConfigError

logger = logging.getLogger(__name__)

DEFAULT_MODEL = str(Path(__file__).resolve().parents[2] / "models" / "VoxCPM2")
DEFAULT_CACHE_DIR = Path(".cache/podcast_tts")
# 未传 --config 时的自动探测路径（与仓库 config_loader 的约定一致：项目 config/ 目录）
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "podcast.yaml"


@dataclass(frozen=True)
class VoiceEntryConfig:
    """单个说话人的 yaml 配置（未校验的原始形态，校验在 voices.build_profiles）。"""
    mode: str = "voice_design"          # voice_design | reference
    design: Optional[str] = None        # Voice Design 括号描述词
    ref_audio: Optional[str] = None     # 参考音频路径（相对 yaml 所在目录解析）


@dataclass(frozen=True)
class RhythmConfig:
    """按 kind 取留白；可整体调成"快剪"或"慢聊"风格。"""
    dialog_gap: float = 0.35
    section_gap: float = 1.0
    note_gap: float = 0.15

    def gap_for(self, kind: str) -> float:
        return {"dialog": self.dialog_gap, "section": self.section_gap,
                "note": self.note_gap}.get(kind, self.dialog_gap)


@dataclass(frozen=True)
class TTSConfig:
    model: str = DEFAULT_MODEL
    device: str = "auto"                # auto | cuda | mps | cpu
    cfg_value: float = 2.0
    inference_timesteps: int = 10
    load_denoiser: bool = False
    optimize: bool = True


@dataclass
class AppConfig:
    script: Path
    output: Path
    speakers: dict[str, str] = field(default_factory=dict)      # 小名 → A/B/N
    voices: dict[str, VoiceEntryConfig] = field(default_factory=dict)
    rhythm: RhythmConfig = field(default_factory=RhythmConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    cache_dir: Path = DEFAULT_CACHE_DIR
    narrate_sections: bool = False
    max_seg_chars: int = 220
    bitrate: str = "192k"
    title: Optional[str] = None         # mp3 元数据
    artist: Optional[str] = None


def _parse_speaker_pair(pair: str) -> tuple[str, str]:
    """--speaker 老王=A → ("老王", "A")。"""
    if "=" not in pair:
        raise VoiceConfigError(f"--speaker 格式应为 名字=ID，收到：{pair}")
    name, sid = pair.split("=", 1)
    name, sid = name.strip(), sid.strip()
    if not name or not sid:
        raise VoiceConfigError(f"--speaker 格式应为 名字=ID，收到：{pair}")
    return name, sid


def _load_yaml(config_path: Optional[Path]) -> dict[str, Any]:
    if config_path is None:
        return {}
    path = Path(config_path)
    if not path.exists():
        logger.warning("配置文件 %s 不存在，使用默认值与 CLI 参数", path)
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise VoiceConfigError(f"配置文件 {path} 顶层必须是 mapping")
    data["__base_dir"] = path.resolve().parent  # 相对路径锚点
    return data


def load_config(args: SimpleNamespace) -> AppConfig:
    """三级合并：CLI > podcast.yaml > 内置默认。

    --config 未指定时自动探测项目根 config/podcast.yaml（与仓库其它模块约定一致）；
    显式指定的文件缺失不报错（warning，全靠默认值+CLI）；voices 段为空时不报错
    （由 voices 层对未配置 ID 用内置 Voice Design 兜底，warning 留痕）。
    """
    config_path = getattr(args, "config", None)
    if not config_path and DEFAULT_CONFIG.exists():
        logger.info("未指定 --config，自动加载 %s", DEFAULT_CONFIG)
        config_path = DEFAULT_CONFIG
    y = _load_yaml(config_path)
    base_dir: Path = y.get("__base_dir", Path.cwd())

    # ---- speakers ----
    speakers: dict[str, str] = {}
    for name, sid in (y.get("speakers") or {}).items():
        speakers[str(name)] = str(sid)
    for pair in getattr(args, "speaker", None) or []:
        name, sid = _parse_speaker_pair(pair)
        speakers[name] = sid

    # ---- voices ----
    voices: dict[str, VoiceEntryConfig] = {}
    for sid, v in (y.get("voices") or {}).items():
        if not isinstance(v, dict):
            raise VoiceConfigError(f"voices.{sid} 必须是 mapping，收到：{v!r}")
        ref = v.get("ref_audio")
        voices[str(sid)] = VoiceEntryConfig(
            mode=str(v.get("mode", "voice_design")),
            design=v.get("design"),
            ref_audio=str(base_dir / ref) if ref else None,
        )
    # CLI 快捷克隆：--ref-A f.wav 等价于 yaml 写 voices.A
    for sid in ("A", "B", "N"):
        ref = getattr(args, f"ref_{sid}", None)
        if ref:
            voices[sid] = VoiceEntryConfig(mode="reference", ref_audio=str(Path(ref).resolve()))

    # ---- tts ----
    y_tts = y.get("tts") or {}
    tts = TTSConfig(
        model=str(getattr(args, "model", None) or y_tts.get("model") or DEFAULT_MODEL),
        device=str(getattr(args, "device", None) or y_tts.get("device") or "auto"),
        cfg_value=float(y_tts.get("cfg", y_tts.get("cfg_value", 2.0))),
        inference_timesteps=int(y_tts.get("inference_timesteps", 10)),
        load_denoiser=bool(y_tts.get("load_denoiser", False)),
        optimize=bool(y_tts.get("optimize", True)),
    )

    # ---- rhythm ----
    y_rhythm = y.get("rhythm") or {}
    rhythm = RhythmConfig(
        dialog_gap=float(getattr(args, "gap", None) or y_rhythm.get("dialog_gap", 0.35)),
        section_gap=float(getattr(args, "section_gap", None) or y_rhythm.get("section_gap", 1.0)),
        note_gap=float(y_rhythm.get("note_gap", 0.15)),
    )

    cache_dir = Path(getattr(args, "cache_dir", None) or y.get("cache_dir") or DEFAULT_CACHE_DIR)

    return AppConfig(
        script=Path(args.script),
        output=Path(args.output),
        speakers=speakers,
        voices=voices,
        rhythm=rhythm,
        tts=tts,
        cache_dir=cache_dir,
        narrate_sections=bool(getattr(args, "narrate_sections", False) or y.get("narrate_sections", False)),
        max_seg_chars=int(y.get("max_seg_chars", 220)),
        bitrate=str(y.get("bitrate", "192k")),
        title=y.get("title"),
        artist=y.get("artist"),
    )
