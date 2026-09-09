---
name: nexa-yt
description: YouTube 内容采集工具集（关键词搜索 / 频道增量扫描 / 单视频详情与字幕抓取），以 MCP 风格 CLI 接口对外提供 JSON 输出。当任务需要从 YouTube 获取视频元数据、字幕文本、按关键词搜集视频、或追踪频道最新视频时使用本技能；不适用视频下载、转码、上传等场景。
---

# nexa-yt — YouTube 内容采集工具集

## 何时使用

**使用本工具集：**
- 按关键词搜集 YouTube 视频（含标题、描述、字幕、可选 Rubric 过滤）
- 扫描某个/某些频道的最新视频列表（支持增量游标，记录上次扫描时间）
- 获取单个视频的完整元数据 + 多语言字幕（VTT 解析为分段文本）

**不使用：**
- 下载视频/音频文件、转码（yt-dlp 仅用于元数据与字幕）
- YouTube 以外的平台

## 运行环境

所有命令在项目根目录执行（若当前不在项目目录，先 `cd /Users/emsure/Documents/Workspace/nexa_yt`）：

```bash
PY=.venv/bin/python   # 工程虚拟环境，依赖 yt-dlp / curl_cffi / PyYAML / pydantic
```

网络依赖本地代理与 Chrome 登录态（`config/app_config.yaml` 配置）：
- 代理：`proxy: {enabled: true, host: 127.0.0.1, port: 7897}`（直连超时，**勿关代理**）
- Cookie：从本地 Chrome 提取（需 Chrome 已登录 YouTube）；提取失败自动降级为匿名访问
- 反爬：固定 `impersonate: chrome`，请求间隔 `sleep_interval`（默认 10s，防限流）

## 三个工具

### 1. 关键词搜索 — `src.yt_scf`

```bash
# 按配置文件 keywords.yaml 中启用的关键词逐个搜索
$PY -m src.yt_scf search --config config/keywords.yaml --limit 5 --output-stdout

# 手动关键词（可多次 --keyword）；一旦指定即完全忽略配置文件中的关键词
$PY -m src.yt_scf search --keyword "AI agent framework" --limit 5 --output-stdout

# 开启过滤（app_config.yaml filter.enabled: true 时默认开启；--no-filter 关闭）
```

参数要点：`--limit N`（每关键词结果数）、`--no-filter`、`--output-stdout`（JSON 直出 stdout，Agent 捕获）、
`--output path.json`（写文件，默认 `./data/yt_search_<keyword>_<时间戳>.json`）。

> 说明：搜索仅使用 ytsearch `extract_flat` 平扫（单次请求，**不逐条抓取详情/字幕**，速度快）；
> 条目含 title/url/video_id/channel/upload_date 等基础字段，可能缺少 description，
> `subtitle` 恒为 `null`——需要完整元数据或字幕时，用单视频工具对 `video.url` 逐个补抓。

### 2. 频道扫描 — `src.yt_channel_scanner`

```bash
# 按 channels.yaml 中 enabled 的频道扫描（自动读取/更新增量状态 data/channel_state.json）
$PY -m src.yt_channel_scanner scan --config config/channels.yaml --max-results 20 --output-stdout

# Agent 接口：直接指定频道，不写状态
$PY -m src.yt_channel_scanner agent --action scan \
  --channel-urls '["https://www.youtube.com/@LinusTechTips/videos"]' \
  --max-results 20 --output-stdout

# 覆盖增量游标
$PY -m src.yt_channel_scanner scan --since 2026-09-01 --channel-url "https://www.youtube.com/@MKBHD/videos" --output-stdout
```

要点：`extract_flat` 平扫仅获取视频列表数据（单次列表请求，**不逐条抓取单视频详情**，速度快）；
因此条目可能缺少 description / view_count / like_count 等完整字段（title/url/video_id/published_at 等基本字段齐全），
需要完整元数据请再用单视频工具抓取；`--since YYYY-MM-DD` 为增量游标（yt-dlp `dateafter`）；
扫描结果写 `data/yt_channel_scan_<时间戳>.json`。

> `--channel-urls`（复数，JSON 数组）在 `scan` 与 `agent` 两个子命令都可用；
> `scan` 另有单数 `--channel-url`（单个 URL），两者同时给出时以 `--channel-urls` 为准。
> 注意裸 handle（如 `@xxx`）会自动补全为 `/videos` 标签页。

### 3. 单视频详情 — `src.yt_video_detail_fetcher`

```bash
$PY -m src.yt_video_detail_fetcher fetch --video-id dQw4w9WgXcQ --lang zh-CN,en --output-stdout
$PY -m src.yt_video_detail_fetcher fetch --url "https://www.youtube.com/watch?v=xxx" --no-subtitle --output ./data/v.json
```

要点：`--lang` 为字幕语言优先级（支持别名 `zh` → zh-CN/zh-Hans/zh-TW 等），
手动字幕优先于自动字幕，解析为带时间戳的 segment 列表；`--no-subtitle` 只取元数据。

### 程序化调用（同进程内复用，避免重复提取 cookie）

```python
import asyncio
from src.config_loader import load_app_config
from src.yt_channel_scanner import YTChannelScanner

async def main():
    async with YTChannelScanner(load_app_config()) as scanner:
        output = await scanner.scan_channels(max_results=5)   # 读 channels.yaml 需传 channels=load_channels(...)
        print(output.model_dump_json(indent=2))
asyncio.run(main())
```

## 返回数据结构（stdout 均为 pretty JSON）

### SearchOutput（搜索）
```jsonc
{
  "generated_at": "ISO8601",
  "keyword": "...",
  "total": 2,
  "items": [{
    "video": { "url", "title", "description", "author", "published_at", "video_id",
               "thumbNail", "categories", "tags", "channel_id", "channel_url",
               "channel_follower_count", "subtitle", "audio_path", "raw" },
    "subtitle": null,   // 搜索仅平扫，不抓字幕（需要时用单视频工具补抓）
    "verdict": { "engine": "keyword_rubric", "status": "matched|needs_review|not_matched",
                 "score": 0.667, "dimensions": {"主题相关": true}, "reason": "..." }  // 未开启过滤为 null
  }]
}
```

### ChannelScanOutput（频道扫描）
```jsonc
{
  "generated_at": "ISO8601",
  "channels": [{
    "url": "...", "alias": "...",
    "items": [ /* 同 YouTubeItem，含 published_at/upload_date 派生字段 */ ],
    "failed_urls": []   // 本批次抓取失败、被跳过的视频
  }]
}
```
另：`data/channel_state.json` 记录各频道 `last_scan` 时间（增量游标，YYYYMMDD 粒度）。

### VideoFetchOutput（单视频）
```jsonc
{
  "meta": { "operation", "url", "fetched_at", "elapsed_seconds",
            "proxy_used": true, "cookie_used": true },
  "detail": { "video_id", "url", "title", "description", "author", "duration",
              "view_count", "like_count", "upload_date": "YYYYMMDD",
              "published_at": "ISO8601", "thumbnail", "categories", "tags", ... },
  "subtitles": { "tracks": [ { "lang": "en", "kind": "manual|auto",
                               "segments": [ {"start": 1.36, "end": 3.04, "text": "..."} ] } ] }
}
```

## 错误处理约定

- **退出码 2**：致命错误（`LoginRequiredError` / `RateLimitedError`）。
  登录失效 → 提示用户在 Chrome 登录 YouTube 后重试；限流 → 退避等待后重试。
- **单条失败不中断**：失败条目记录到 `failed_urls`（频道扫描）或打日志跳过（搜索），
  批次结果仍返回已抓到的部分。
- **字幕缺失不报错**：无字幕/下载失败仅返回空 `tracks` / `subtitle: null`。

## 注意事项

- `sleep_interval` 是防限流关键配置，不要随意调小。
- 搜索/扫描走代理 + Chrome 指纹伪装，异常时先确认代理可用（`nc -z 127.0.0.1 7897`）。
- 输出 JSON 一律 pretty print（indent=2），不使用数据库，结果仅落盘 `./data/*.json`。
