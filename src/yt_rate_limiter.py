"""跨进程最小请求间隔节流器（YTRateLimiter）。

背景：Agent 批量调用搜索/扫描/单视频工具时，常通过 shell 循环拉起多个
独立 CLI 进程，进程之间无法共享内存态限速，容易导致 YouTube 返回
HTTP 429（Too Many Requests）。本模块基于「状态文件 + 文件锁 + 时间槽预约」
实现硬约束：任意两个出站请求之间至少间隔 min_interval 秒，同进程与
跨进程场景均生效。

语义说明：
- 状态文件记录上一次「预约的请求发送时间」（epoch 秒，使用 time.time()，
  保证跨进程可比）。
- acquire() 持锁期间预约时间槽：slot = max(now, last + min_interval)，
  并立即写回状态文件；多进程竞争时自然排队，互不穿透。
- 预约后睡眠至时间槽起点再放行，因此实际发送时刻 >= slot。

默认状态文件：output_dir/.rate_limit_state.json（隐藏文件，不干扰数据目录）。
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import time
from pathlib import Path

from .logger import get_logger


class YTRateLimiter:
    """跨进程最小请求间隔限流器（时间槽预约 + 文件锁）。"""

    def __init__(self, state_file: Path, min_interval: float) -> None:
        self.state_file = Path(state_file)
        self.min_interval = max(0.0, float(min_interval))
        self.logger = get_logger(f"{__name__}.{self.__class__.__name__}")

    # ------------------------------------------------------------------ 内部

    def _reserve_slot(self) -> float:
        """预约下一个可用时间槽，返回需等待的秒数（0 表示可立即发送）。"""
        now = time.time()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                try:
                    last = float(json.load(fh).get("last_request_at", 0.0))
                except (json.JSONDecodeError, ValueError, AttributeError):
                    last = 0.0
                slot = max(now, last + self.min_interval)
                fh.seek(0)
                fh.truncate()
                json.dump({"last_request_at": slot}, fh)
                fh.flush()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        return max(0.0, slot - now)

    # ------------------------------------------------------------------ 公共接口

    async def acquire(self) -> float:
        """等待至下一个可用时间槽，返回实际等待秒数。

        min_interval <= 0 时直接放行（返回 0）。
        """
        if self.min_interval <= 0:
            return 0.0
        waited = await asyncio.to_thread(self._reserve_slot)
        if waited > 0:
            self.logger.debug("rate limiter: sleep %.1fs before next request", waited)
            await asyncio.sleep(waited)
        return waited
