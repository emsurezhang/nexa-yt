# podcast_tts

Markdown 双人对话脚本 → 单个 48kHz 播客 mp3（VoxCPM2 引擎）。

合成慢（GPU 每秒音频需数秒生成），所以整个组件围绕两件事设计：**分片 + 断点续跑**，以及**音色一致性**（谈话类节目的成败核心）。

## 六层流水线

```
script.md ──(CLI 参数、podcast.yaml)
        │
        ▼
┌─ 解析层    parser.py      Markdown → [Utterance]；说话人识别 / 清洗 / 长句切分
├─ 音色层    voices.py      说话人 ID → SpeakerProfile（克隆 │ Voice Design）
├─ 合成层    synth/         调度器（缓存/重试/续跑）+ 引擎适配器 → 分片 wav 落盘
├─ 后处理层  postprocess.py 留白拼接 → 峰值归一（节奏感在这里产生）
└─ 输出层    exporter.py    mp3 双通道写出 + manifest.json 时间戳清单
```

核心数据结构（`models.py`，层间唯一"通用货币"）：

- `Utterance(seq, speaker, text, kind)`：`kind` = `dialog`（对话）/ `section`（章节，更长留白）/ `note`（笑声等副语言，更短留白）
- `SpeakerProfile(speaker_id, mode, design_text, ref_audio)`：`mode` = `reference`（参考音频克隆）/ `voice_design`（凭空造声）

## 安装与依赖

```bash
pip install voxcpm soundfile numpy torch   # voxcpm 会带入 torch 等
ffmpeg                                     # 可选：mp3 回退通道 + ID3 标签
```

- mp3 写出优先走 soundfile（需 libsndfile ≥ 1.1），失败自动回退 ffmpeg，再失败改产 wav 并告警
- VoxCPM2 约 8GB 显存；低显存机器用 `--device cpu`（慢，但可用）

## 快速开始

```bash
# 0. 看一眼脚本会被切成哪些片段（不加载模型，秒出）
python -m src.podcast_tts script.md -o out.mp3 --dry-run

# 1. 试听音色：只合成前 3 段
python -m src.podcast_tts script.md -o out.mp3 --limit 3

# 2. 满意后全量跑（中断后重跑同一命令，自动从缓存续跑）
python -m src.podcast_tts script.md -o out.mp3

# 3. Voice Design 音色抽签不满意？单片段重抽后整段重拼
python -m src.podcast_tts script.md -o out.mp3 --redo 12

# 4. 换参考音频 / 改描述词后想整期重录？弃用全部缓存，从零生成
python -m src.podcast_tts script.md -o out.mp3 --fresh
```

输出：`out.mp3` + `out.manifest.json`（每段起止时间戳，供 Shownotes 跳转复用）。

## CLI

```
python -m src.podcast_tts script.md -o out.mp3 [选项]

  --config podcast.yaml        配置文件（CLI > yaml > 默认值；缺省自动探测项目 config/podcast.yaml）
  --speaker 名字=ID            说话人映射，可重复（如 --speaker 老王=A）
  --ref-A f.wav / --ref-B / --ref-N   快捷指定克隆参考音频
  --narrate-sections           ## 【章节】由 N 旁白口播（默认跳过）
  --gap 0.4 / --section-gap 1.0       节奏留白覆盖
  --dry-run                    只解析打印片段清单，不加载模型
  --limit N                    只合成前 N 段（试听）
  --redo SEQ                   片段级重生成，可重复（与 --fresh 互斥）
  --fresh                      忽略所有缓存，全部片段重新生成
  --cache-dir / --device / --model / --bitrate
```

未提供 speaker 映射时内置默认 `小硕→A、小丽→B、旁白→N`；未配置音色的 ID 自动用内置 Voice Design 描述词兜底（warning 留痕）。

## 配置文件

未传 `--config` 时自动探测项目根目录的 `config/podcast.yaml`（与仓库其它模块约定一致）；
显式传入 `--config` 则以传入为准。要点：

```yaml
speakers: { 小硕: A, 小丽: B }          # 脚本小名 → ID，改人名不用改代码
voices:
  A: { mode: reference, ref_audio: samples/male.wav }   # 克隆（音色一致性最佳）
  B: { mode: voice_design, design: （清亮活泼的年轻女声） }  # 凭空造声
rhythm: { dialog_gap: 0.35, section_gap: 1.0, note_gap: 0.15 }
tts: { model: openbmb/VoxCPM2, device: auto, cfg: 2.0, inference_timesteps: 10 }
```

- `ref_audio` 相对 yaml 所在目录解析；门槛：5–15s、采样率 ≥16k（克隆质量取决于此，不达标直接报错）
- 允许混合模式：A 克隆 + B Design 逐人独立
- 清洗规则（`--`、金额 `$2M→两百万` 等）在 `parser.py` 的 `TextReplacer` 里，换节目可整体替换

## 缓存与断点续跑（成本核心）

- 缓存命名：`{seq:04d}_{speaker}_{hash8}.wav`——**改一个词只重合成一个词，换音色必全部重合成**
- 命中缓存只校验可读性，不重跑；`--redo SEQ` 强制单片段重生成；`--fresh` 强制全量重生成（换参考音频 / 改描述词后用）
- 单片段失败重试 2 次后**跳过并继续**，跑完统一报告 `--redo` 清单，不让一个片段卡死整期节目

## 合规

mp3 元数据自动写入 `AI synthesized speech (VoxCPM2, Apache-2.0). Not voiced by humans.`。
使用参考音频克隆时，请确保参考音频中人物的声纹使用已获得授权。

## 已知风险与对策

| 风险 | 对策 |
|---|---|
| Voice Design 每次合成音色有波动 | 官方建议重生成 1–3 次 → 用 `--redo SEQ` 单片段重抽 |
| 合成耗时（20 分钟节目约 90 片） | 缓存粒度宁可细不可粗；中断重跑自动续 |
| 显存不足（约 8GB） | `--device cpu` 回退 |
| libsndfile 无 mp3 支持 | 自动回退 ffmpeg → 再失败产 wav |

## 扩展点（已预留接口）

- **多说话人**：speaker 映射表天然支持 3+ 人圆桌
- **多引擎**：实现 `synth/engine.py` 的 `TTSEngine` 协议即可（如 CosyVoice 做 A/B）
- **BGM 混音**：`postprocess.mix_bgm` 预留
- **Shownotes 联动**：`manifest.json` 时间戳即取即用
