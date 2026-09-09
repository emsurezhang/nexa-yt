# Agents.md — nexa_yt

## 1. 工程定位

`nexa_yt` 是一个 **YouTube 内容采集工具集**，以 **MCP Server** 形式对外提供服务，
供其他 Agent Framework 调用。核心能力：

| 能力 | 模块 | 说明 |
|---|---|---|
| 关键词搜索 | `src/yt_scf.py` | `ytsearchN:{keyword}` 搜索视频，含元数据、字幕，可选本地模型 Rubric 过滤 |
| 频道扫描 | `src/yt_channel_scanner.py` | 按频道增量扫描视频列表（含状态游标） |
| 视频详情 | `src/yt_video_detail_fetcher.py` | 单视频完整元数据 + 多语言字幕抓取（VTT 解析） |

## 2. 硬性约束（必须遵守）

1. **不使用数据库**。所有输出持久化为 `./data/` 目录下的 **JSON 文件**（pretty print 必须）。
   - 扫描结果命名：`yt_channel_scan_YYYYMMDD_HHMMSS.json`
   - 增量状态：`channel_state.json`（记录各频道上次扫描时间，作为增量游标）
   - 数据模型见 `src/types.py`（`ChannelItem` / `YouTubeItem`，dataclass + `asdict` 序列化）
2. **依赖白名单**：工程高度依赖 **Chrome 浏览器**（cookie 提取、TLS 指纹伪装）与 **yt-dlp**。
   - 引入任何**新依赖前必须先向用户确认**，说明用途与必要性，获批后方可加入。
   - 反爬相关默认假设：`impersonate: chrome`（依赖 `curl_cffi`）、`cookiesfrombrowser`。

## 3. 架构与数据流

```
config/*.yaml  ──►  yt-dlp (proxy / cookies / impersonate / 限速)
                      │
        ┌─────────────┼──────────────────┐
        ▼             ▼                  ▼
  关键词搜索      频道扫描(extract_flat)   单视频详情
  yt_scf.py       yt_channel_scanner.py   yt_video_detail_fetcher.py
        │             │ (channel_state.json 增量)  │
        └─────────────┴──────────────────┘
                      ▼
              ./data/*.json (pretty JSON)
                      ▼
                MCP 工具接口 → 其他 Agent Framework
```

## 4. 关键设计约定

- **配置文件**（`config/`）：
  - `app_config.yaml`：全局配置（网络代理、输出目录）——当前为空，需补全
  - `channels.yaml`：频道列表（url / alias / max_results / enabled）
  - `keywords.yaml`：关键词列表（支持逐条 enabled 开关）
- **yt-dlp 选项约定**：`extract_flat` 快扫列表、`playlistend` 限制条数、
  `dateafter` 增量过滤、`socket_timeout: 15`、`retries: 1`、请求间隔 `sleep`（默认 10s 量级）防限流。
- **双入口**：每个模块同时支持 CLI（`python -m xxx scan --config ...`）和
  agent 接口（`--action ... --output-stdout`，JSON 直出 stdout 供 Agent 捕获），
  核心方法也可被其他模块直接调用。
- **错误分类**：区分 `LoginRequiredError` / `RateLimitedError`（直接上抛）与一般单条失败
  （记录 failed_urls 后继续，最终汇总上报）。
- **字幕处理**：按 `--lang` 优先级匹配手动/自动字幕 → 下载 VTT → 解析为
  segment 列表 → 清理时间戳与重复行。

## 5. 编码规范

- Python，异步优先（`asyncio.to_thread` 包裹 yt-dlp 同步调用）。
- 数据模型用 `@dataclass`，序列化统一走 `asdict`；对外输出前用 Pydantic 校验。
- 日志统一走 `src/logger.py`（待实现），禁止裸 `print`。
- 注释与设计文档使用中文，与现有文件风格保持一致。

## 6. 验证
- 使用python 虚拟环境 .venv/bin/activate 激活虚拟环境
