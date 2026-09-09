"""yt-dlp 共享基础设施。

- 构建 yt-dlp 选项（proxy / Chrome cookie / impersonate chrome 指纹伪装）
- 从本地 Chrome 导出 cookie 到临时文件（用完即删）
- 统一的 extract_info 异步封装与错误分类
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import yt_dlp
from yt_dlp.cookies import extract_cookies_from_browser
from yt_dlp.networking.impersonate import ImpersonateTarget

from .config_loader import AppConfig, load_app_config
from .errors import LoginRequiredError, RateLimitedError
from .logger import get_logger
from .yt_rate_limiter import YTRateLimiter


class YDLBase:
    """yt-dlp 公共基类：配置、cookie、代理、错误分类。"""

    def __init__(self, app_config: Optional[AppConfig] = None) -> None:
        self.app_config = app_config or load_app_config()
        self.logger = get_logger(f"{__name__}.{self.__class__.__name__}")
        # 跨进程全局限流：所有出站请求（extract_info / 字幕下载等）统一经过它
        self._rate_limiter = YTRateLimiter(
            state_file=self.app_config.ensure_output_dir() / ".rate_limit_state.json",
            min_interval=self.app_config.rate_limit_min_interval,
        )

    # ------------------------------------------------------------------ 配置

    def _apply_proxy_config(self, opts: dict[str, Any]) -> None:
        """根据全局配置为 yt-dlp 设置代理。"""
        proxy_url = self.app_config.proxy_url
        if proxy_url:
            opts["proxy"] = proxy_url
            self.logger.debug("yt-dlp will use proxy: %s", proxy_url)
        else:
            self.logger.debug("yt-dlp proxy disabled by config")

    def _build_base_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "socket_timeout": int(self.app_config.yt_dlp.get("socket_timeout", 15)),
            "retries": int(self.app_config.yt_dlp.get("retries", 1)),
            "extractor_retries": int(self.app_config.yt_dlp.get("extractor_retries", 1)),
            "ignore_no_formats_error": True,
            # yt-dlp 抽取阶段内部请求间隔（看网页 / player API 等），防限流
            "sleep_interval_requests": float(
                self.app_config.yt_dlp.get("sleep_interval_requests", 5)
            ),
            # 伪装成 Chrome 的 TLS/HTTP2 指纹，规避基于请求特征的反爬检测。
            "impersonate": ImpersonateTarget(
                self.app_config.yt_dlp.get("impersonate", "chrome")
            ),
        }
        self._apply_proxy_config(opts)
        return opts

    async def _get_cookie_file(self) -> Optional[Path]:
        """从本地 Chrome 导出 cookie 到临时文件；失败时返回 None（降级为匿名）。"""
        browser = self.app_config.yt_dlp.get("cookies_from_browser", "chrome")
        if not browser:
            return None
        try:
            cookie_path = Path(tempfile.mkstemp(prefix="nexa_yt_cookies_", suffix=".txt")[1])
            # yt-dlp 2026.x 移除了 YoutubeDL.extract_cookies_from_browser，
            # 改用 yt_dlp.cookies.extract_cookies_from_browser 模块级 API。
            cookiejar = extract_cookies_from_browser(browser)
            cookiejar.save(str(cookie_path))
            self.logger.debug("cookies exported from %s -> %s", browser, cookie_path)
            return cookie_path
        except Exception as error:  # noqa: BLE001
            self.logger.warning("无法从 %s 提取 cookie，降级为匿名访问: %s", browser, error)
            return None

    # ------------------------------------------------------------------ 抓取

    async def _get_ydl_opts(self, cookie_file: Optional[Path] = None) -> dict[str, Any]:
        opts = self._build_base_opts()
        if cookie_file and cookie_file.exists():
            opts["cookiefile"] = str(cookie_file)
        return opts

    def _classify_extraction_error(self, error: Exception) -> Exception:
        """将 yt-dlp 异常分类为 LoginRequired / RateLimited，其余原样返回。"""
        message = str(error).lower()
        if any(tag in message for tag in ("login required", "sign in to", "cookies are disabled")):
            return LoginRequiredError(str(error))
        if any(tag in message for tag in ("http error 429", "too many requests", "rate-limit", "ratelimit")):
            return RateLimitedError(str(error))
        return error

    async def _extract_info(
        self, target: str, opts: dict[str, Any], operation: str
    ) -> Optional[dict[str, Any]]:
        """在线程池中执行 yt-dlp extract_info，并做错误分类。"""
        self.logger.info("yt-dlp %s started: %s", operation, target)
        await self._rate_limiter.acquire()

        def _sync_extract() -> Optional[dict[str, Any]]:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(target, download=False)

        started = time.monotonic()
        try:
            info = await asyncio.to_thread(_sync_extract)
        except Exception as error:  # noqa: BLE001
            classified = self._classify_extraction_error(error)
            self.logger.error("yt-dlp %s failed: %s -> %s", operation, target, classified)
            if classified is error:
                raise
            raise classified from error
        self.logger.info(
            "yt-dlp %s finished: %s (%.1fs)", operation, target, time.monotonic() - started
        )
        return info
