# Agents.md — podcast_tts

本文件是给后续 AI Coding Agent 的操作手册。改动本模组前**必须先读完**。

## 1. 工程定位

`podcast_tts` 把 Markdown 双人对话脚本合成为单个 48kHz 播客 mp3（VoxCPM2 引擎）。
上游是 `podcast_script_writer` 生成的 `**名字**：台词` 格式脚本；输出供发布流程使用。

两个设计原点，任何改动不得违背：

1. **合成慢 → 必须分片 + 断点续跑**：缓存粒度宁可细不可粗。
2. **音色一致性是谈话类节目的成败核心**：音色层独立成层，不做进合成层。

## 2. 硬性约束（必须遵守）

1. **`voxcpm` 只允许出现在 `synth/voxcpm2.py`**，且必须延迟到 `warmup()` 内部 import。
   换引擎只新增一个适配器文件 + 改 `synth/engine.py` 的 `load_engine`，其余各层零改动。
   提交前自查：`grep -rn "import voxcpm" src/podcast_tts/` 只能有一行有效命中。
2. **`__main__.py` 只做参数装配**，不含业务逻辑；流程顺序固定为
   `load_config → parse_script → build_profiles → load_engine → warmup → scheduler.run
   → assemble → normalize_peak → build_manifest → export`。
3. **缓存命名契约**（`scheduler.cache_path`）：`{seq:04d}_{speaker}_{hash8}.wav`。
   seq 定顺序、speaker 保证换音色必重合成、hash8 保证改词必重合成。改 key 规则
   等于废弃所有既有缓存，属破坏性变更，必须在 README 同步说明。
4. **失败不中断整期节目**：单片段重试耗尽 → 记 `SynthResult.skipped` 继续跑；
   `assemble` 缺段 → 抛 `AssembleError`（宁可报错也不默默产出缺段节目）。
5. **依赖白名单**（跟随仓库根约束）：`numpy` / `soundfile` / `torch` / `voxcpm` / `PyYAML`。
   引入任何新依赖前先向用户确认。mp3 回退靠外部 `ffmpeg`（可选，不可用要降级不报错）。

## 3. 层间契约（invariants，调用方可以依赖）

- `Utterance.text`：无 Markdown 残留、长度 ≤ `max_seg_chars`，**合成器不再做清洗**。
- `SpeakerProfile`：经 `voices.build_profiles` 构造即已完成校验（reference 音频
  5–15s、≥16kHz），传给引擎时必然合法；引擎层不得再校验。
- `TTSEngine.synth` 返回 float32 单声道波形，采样率 = `engine.sample_rate`；
  任何失败抛 `SynthError`，**引擎内部不做重试**（重试归调度器）。
- 错误模型五异常：`ParseError`（解析层）/ `VoiceConfigError`（音色与配置）/
  `SynthError`（单片段）/ `AssembleError`（缺段）/ `EngineNotFoundError`（工厂）。
  语义见 `models.py`，不得新增平级异常体系。

## 4. 模块速查

| 文件 | 职责 | 不得做什么 |
|---|---|---|
| `models.py` | 数据契约 + 异常 | 不得 import 任何业务层 |
| `config.py` | CLI > yaml > 默认 三级合并；未传 `--config` 自动探测项目 `config/podcast.yaml` | 不校验音色（校验在 voices 层） |
| `parser.py` | Markdown → `[Utterance]`，纯函数可单测 | 不碰模型/音频格式 |
| `voices.py` | 音色档案 + 参考音频门槛 + `design_prompt` | 不做合成 |
| `synth/scheduler.py` | 缓存/重试/续跑/事件流 | 不 import voxcpm；不 catch 裸 `Exception` 吞掉 SynthError 以外的崩溃 |
| `synth/voxcpm2.py` | 唯一 voxcpm 接触点 | 不做重试、不做清洗 |
| `postprocess.py` | 留白拼接 / 峰值归一 / `mix_bgm`（预留） | 缺段不得自动补静音 |
| `exporter.py` | mp3 三通道 + ID3 + manifest | mp3 双通道失败必须降级 wav 而不是抛错 |

## 5. 编码规范

- `@dataclass(frozen=True)` 做数据契约；`asdict` 序列化（manifest 用）。
- 日志走 `logging.getLogger(__name__)`；CLI 进度走 `SynthEvent` 事件流
  （`scheduler.print_event` 是默认回调，GUI 可复用同一事件流）。
- 中文注释与 docstring，风格与仓库其余模块一致。
- 测试用 `unittest`（仓库约定），放 `tests/test_podcast_tts.py`，**桩引擎用
  `FakeEngine`（见该文件），测试不得加载真实模型、不得访问网络**。
- 提交前必跑：
  ```bash
  .venv/bin/python -m unittest tests.test_podcast_tts   # 20 个用例
  .venv/bin/python -m src.podcast_tts <任意脚本> -o /tmp/x.mp3 --dry-run
  ```

## 6. 常见任务的操作指引

| 任务 | 改哪里 |
|---|---|
| 忽略缓存全量重生成 | 零改动：CLI `--fresh`（force=True 且 only_seqs=None，与 `--redo` 互斥） |
| 接新引擎（CosyVoice 等） | 新增 `synth/xxx.py` 实现 `TTSEngine` 协议 + `engine.load_engine` 注册 |
| 换清洗词表 | 实现 `parser.TextReplacer`，经 `parse_script(replacer=...)` 注入，不改默认表 |
| 加副语言音效（笑声库） | `kind="note"` 已区分；在 `postprocess.assemble` 按 kind 分支挂音效 |
| 调节目节奏 | 只动 `RhythmConfig`（yaml `rhythm:` 或 CLI `--gap/--section-gap`） |
| BGM 混音 | `postprocess.mix_bgm` 已实现，CLI 加参数接线即可 |
| 缓存格式变更 | 同步改 `cache_path` + README 破坏性变更说明 + 提供旧缓存迁移或清理脚本 |

## 7. 已知风险（设计阶段已承认，别再"重新发现"）

- Voice Design 音色每次合成有波动 → 已有 `--redo SEQ`（片段级）与 `--fresh`（全量）对策，不要试图在引擎内自动重抽。
- 显存约 8GB → `--device cpu` 是支持路径不是 bug。
- soundfile 的 mp3 是 VBR 近似码率（`compression_level` 映射），要精确 CBR 走 ffmpeg 通道——这是有意取舍。
