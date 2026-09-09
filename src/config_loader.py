"""配置文件加载与校验。

约定：
- config/app_config.yaml  → AppConfig（全局：输出目录 / 代理 / yt-dlp / 过滤）
- config/channels.yaml    → list[ChannelConfig]（频道扫描）
- config/keywords.yaml    → list[KeywordConfig]（关键词搜索）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .logger import get_logger

logger = get_logger(__name__)

DEFAULT_APP_CONFIG = Path(__file__).resolve().parent.parent / "config" / "app_config.yaml"
DEFAULT_CHANNELS_CONFIG = Path(__file__).resolve().parent.parent / "config" / "channels.yaml"
DEFAULT_KEYWORDS_CONFIG = Path(__file__).resolve().parent.parent / "config" / "keywords.yaml"


@dataclass
class AppConfig:
    """全局配置（app_config.yaml）。"""
    output_dir: Path = Path("./data")
    proxy: dict[str, Any] = field(default_factory=lambda: {"enabled": False, "host": "127.0.0.1", "port": 7897})
    yt_dlp: dict[str, Any] = field(default_factory=dict)
    filter: dict[str, Any] = field(default_factory=lambda: {"enabled": False})
    rate_limit: dict[str, Any] = field(default_factory=lambda: {
        "min_request_interval": 15,
        "subtitle_max_retry": 2,
        "subtitle_retry_backoff": 30,
    })

    @property
    def proxy_url(self) -> Optional[str]:
        if not self.proxy.get("enabled"):
            return None
        host = self.proxy.get("host", "127.0.0.1")
        port = self.proxy.get("port", 7897)
        if not host or not isinstance(port, int):
            return None
        return f"http://{host}:{port}"

    @property
    def request_interval_seconds(self) -> float:
        """yt-dlp 下载间隔（sleep_interval，向后兼容保留）。"""
        return float(self.yt_dlp.get("sleep_interval", 10))

    @property
    def rate_limit_min_interval(self) -> float:
        """全局最小请求间隔（秒），跨进程生效。"""
        return float(self.rate_limit.get("min_request_interval", 15))

    @property
    def subtitle_max_retry(self) -> int:
        """字幕下载遇 429 的退避重试次数。"""
        return int(self.rate_limit.get("subtitle_max_retry", 2))

    @property
    def subtitle_retry_backoff(self) -> float:
        """字幕 429 首次退避秒数（逐次翻倍）。"""
        return float(self.rate_limit.get("subtitle_retry_backoff", 30))

    def ensure_output_dir(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self.output_dir


@dataclass
class ChannelConfig:
    url: str
    alias: str = ""
    max_results: int = 10
    enabled: bool = True


@dataclass
class KeywordConfig:
    keyword: str
    enabled: bool = True


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        logger.warning("配置文件不存在，使用默认值: %s", path)
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件格式错误（应为 YAML 对象）: {path}")
    return data


def load_app_config(path: Path | str = DEFAULT_APP_CONFIG) -> AppConfig:
    raw = _load_yaml(Path(path))
    return AppConfig(
        output_dir=Path(raw.get("output_dir", "./data")),
        proxy=raw.get("proxy") or {"enabled": False},
        yt_dlp=raw.get("yt_dlp") or {},
        filter=raw.get("filter") or {"enabled": False},
        rate_limit=raw.get("rate_limit") or {},
    )


def load_channels(path: Path | str = DEFAULT_CHANNELS_CONFIG) -> list[ChannelConfig]:
    raw = _load_yaml(Path(path))
    channels = [
        ChannelConfig(
            url=str(c["url"]),
            alias=str(c.get("alias", "")),
            max_results=int(c.get("max_results", 10)),
            enabled=bool(c.get("enabled", True)),
        )
        for c in (raw.get("channels") or [])
        if isinstance(c, dict) and c.get("url")
    ]
    return channels


def load_keywords(path: Path | str = DEFAULT_KEYWORDS_CONFIG) -> list[KeywordConfig]:
    raw = _load_yaml(Path(path))
    keywords = [
        KeywordConfig(keyword=str(k["keyword"]), enabled=bool(k.get("enabled", True)))
        for k in (raw.get("keywords") or [])
        if isinstance(k, dict) and k.get("keyword")
    ]
    return keywords
