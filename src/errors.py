"""错误分级体系。

- LoginRequiredError / RateLimitedError：致命错误，直接上抛，调用方应中止当前批次。
- 单条内容失败：由调用方记录 failed_urls 后继续，最终汇总为 PluginError 上报。
"""
from __future__ import annotations


class NexaYTError(Exception):
    """工程根异常。"""


class LoginRequiredError(NexaYTError):
    """YouTube 要求登录（cookie 失效或未提供）。"""


class RateLimitedError(NexaYTError):
    """触发 YouTube 限流（HTTP 429 等），应退避后重试。"""


class PluginError(NexaYTError):
    """批次级错误：部分条目失败后的汇总上报。"""


class ChannelNotFoundError(NexaYTError):
    """频道不存在（按 url / alias 均无法定位订阅频道）。"""


class ChannelAlreadyExistsError(NexaYTError):
    """频道已存在（按 url / handle 归一化判重）。"""
