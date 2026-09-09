"""解析层：Markdown 双人对话脚本 → [Utterance] 结构化台词序列。

职责边界：只负责"把脚本读成台词列表"——不碰模型、不感知音频格式。
清洗规则做成可插拔的替换表（TextReplacer），不同节目可换词表。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .models import ParseError, Utterance

logger = logging.getLogger(__name__)

# **名字**：台词  /  **名字**:台词
_SPEAKER_LINE_RE = re.compile(r"^\s*\*\*(.+?)\*\*\s*[:：]\s*(.+?)\s*$")
# ## 【章节】 或 ## 章节
_SECTION_RE = re.compile(r"^\s*##\s*【?(.+?)】?\s*$")
# 整句都是括号副语言 → kind=note（如 （笑）（笑声））
_NOTE_RE = re.compile(r"^(（[^（）]*）\s*)+$")
# 长句切分的断点：中文标点（保留标点在原分片末尾）
_SPLIT_RE = re.compile(r"(?<=[。！？；，、：])")

DEFAULT_MAX_SEG_CHARS = 220


class TextReplacer:
    """可插拔口播清洗接口。返回清洗后文本；规则由具体实现维护。"""

    def apply(self, text: str) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class _Rule:
    pattern: "re.Pattern[str]"
    repl: str


class RegexReplacer(TextReplacer):
    """基于正则替换表的清洗器；内置默认中文口播表见 default_replacer()。"""

    def __init__(self, rules: list[tuple[str, str]]):
        self._rules = [_Rule(re.compile(p), r) for p, r in rules]

    def apply(self, text: str) -> str:
        for rule in self._rules:
            text = rule.pattern.sub(rule.repl, text)
        return text.strip()


# ---------------------------------------------------------------- 默认替换表

def _int_to_chinese(n: int) -> str:
    """小整数转中文数字（金额替换用，支持 0–9999）。"""
    digits = "零一二三四五六七八九"
    if n < 10:
        return digits[n]
    if n < 100:
        tens, rest = divmod(n, 10)
        return ("" if tens == 1 else digits[tens]) + "十" + (digits[rest] if rest else "")
    if n < 1000:
        head, rest = divmod(n, 100)
        if rest == 0:
            return digits[head] + "百"
        return digits[head] + "百" + ("零" if rest < 10 else "") + _int_to_chinese(rest)
    head, rest = divmod(n, 1000)
    if rest == 0:
        return digits[head] + "千"
    return digits[head] + "千" + ("零" if rest < 100 else "") + _int_to_chinese(rest)


_MONEY_RE = re.compile(r"\$(\d+(?:\.\d+)?)\s*([kKmMbB])")
_MONEY_UNIT = {"k": ("千", 1_000), "m": ("百万", 1_000_000), "b": ("十亿", 1_000_000_000)}


def _money_repl(match: "re.Match[str]") -> str:
    num = float(match.group(1))
    unit_cn, scale = _MONEY_UNIT[match.group(2).lower()]
    if num == int(num):
        head = "两" if num == 2 else _int_to_chinese(int(num))  # 口语：两百万
        return head + unit_cn
    return str(int(num * scale))  # 小数金额保底：交给 TTS 读阿拉伯数字


class DefaultReplacer(TextReplacer):
    """内置中文口播表：去 ** 标记、破折号→停顿、$1M→一百万美元。

    不同节目可整体替换为自定义 TextReplacer（经 parse_script 注入）。
    """

    def __init__(self):
        self._regex = RegexReplacer([
            (r"\*\*", ""),        # 去 Markdown 加粗标记
            (r"——+|—", "，"),     # 破折号 → 停顿（TTS 对破折号处理不稳定）
        ])

    def apply(self, text: str) -> str:
        text = self._regex.apply(text)
        text = _MONEY_RE.sub(_money_repl, text)
        text = re.sub(r"(?<=[\u4e00-\u9fff]) (?=[\u4e00-\u9fff])", "", text)
        return text.strip()


def default_replacer() -> TextReplacer:
    return DefaultReplacer()


# ---------------------------------------------------------------- 长句切分

def _split_long_text(text: str, max_chars: int) -> list[str]:
    """按中文标点切成 ≤ max_chars 的分片；单句超长则硬切。"""
    pieces = [p for p in _SPLIT_RE.split(text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > max_chars:
            chunks.append(current.strip())
            current = piece
        else:
            current += piece
        while len(current) > max_chars:  # 单句超长：硬切
            chunks.append(current[:max_chars])
            current = current[max_chars:]
    if current.strip():
        chunks.append(current.strip())
    return chunks


# ---------------------------------------------------------------- 主入口

def parse_script(
    md_path: Path,
    speaker_map: dict[str, str],
    *,
    narrate_sections: bool = False,
    max_seg_chars: int = DEFAULT_MAX_SEG_CHARS,
    replacer: TextReplacer | None = None,
) -> list[Utterance]:
    """读取 Markdown，返回按 seq 升序的 Utterance 列表。

    - 识别 **名字**：台词 行；> 引用、# 标题、分隔线跳过。
    - ## 【章节】：narrate_sections=True 生成 N 旁白（"下面进入，XX。"），否则跳过。
    - 未知说话人 → 记 warning 并跳过该行（弱脚本也能跑完）。
    - 空结果 → 抛 ParseError（提示检查格式，而非产出静音文件）。
    """
    replacer = replacer or default_replacer()
    lines = Path(md_path).read_text(encoding="utf-8").splitlines()

    utterances: list[Utterance] = []
    seq = 0
    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue

        section = _SECTION_RE.match(line)
        if section and not line.startswith("###"):
            title = section.group(1).strip()
            if narrate_sections:
                utterances.append(Utterance(
                    seq=seq, speaker="N", kind="section",
                    text=f"下面进入，{title}。",
                ))
                seq += 1
            continue

        if line.startswith(("#", ">", "---", "***", "[!")):
            continue  # 标题/引用/分隔线/提示块

        m = _SPEAKER_LINE_RE.match(line)
        if not m:
            logger.debug("第 %d 行未识别，跳过：%s", lineno, line[:40])
            continue

        name = m.group(1).strip()
        speaker = speaker_map.get(name)
        if speaker is None:
            logger.warning("第 %d 行未知说话人「%s」，跳过", lineno, name)
            continue

        text = replacer.apply(m.group(2))
        if not text:
            continue

        kind = "note" if _NOTE_RE.match(text) else "dialog"
        for chunk in _split_long_text(text, max_seg_chars):
            utterances.append(Utterance(seq=seq, speaker=speaker, text=chunk, kind=kind))
            seq += 1

    if not utterances:
        raise ParseError(
            f"{md_path} 未解析出任何台词：请检查 **名字**：台词 格式与 speaker 映射"
        )
    return utterances
