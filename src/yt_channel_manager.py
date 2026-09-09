# 功能设计：对用户关注的 YouTube 频道提供管理功能（订阅源：config/channels.yaml）
# 1. 加载 / 写回 channels.yaml（PyYAML safe_dump；重写后注释不保留，字段顺序固定
#    url / alias / max_results / enabled）
# 2. 频道定位（ref）：alias 精确匹配（大小写不敏感）→ url 归一化匹配。
#    identity key：@handle 形式取 handle 小写；其余取 host+path 小写（去尾斜杠）
# 3. 增删改查 + enable/disable：
#    - alias 非空时全局唯一；新增 / 改 alias 时做冲突检测
#    - 新增按 identity key 判重（ChannelAlreadyExistsError）
#    - 按 ref 定位失败抛 ChannelNotFoundError
# 4. add 默认联网校验（yt-dlp extract_flat 拉取频道信息，确认存在并自动补全 alias），
#    可 --no-verify 跳过；LoginRequired / RateLimited 直接上抛
# 5. remove 同步清理 channel_state.json 中该频道的增量游标
# 6. videos 委托 YTChannelScanner.channel_scan 产出视频列表（ChannelVideosOutput）
# 7. 输出经 Pydantic 校验（ChannelManagerOutput / ChannelVideosOutput），
#    双入口：CLI 与 agent（--action + --params-json，JSON 直出 stdout）
#
# 基础用法：
#   python -m src.yt_channel_manager list
#   python -m src.yt_channel_manager add --url "https://www.youtube.com/@handle" \
#     --alias "ltt" [--max-results 30] [--no-verify]
#   python -m src.yt_channel_manager remove --ref "ltt"
#   python -m src.yt_channel_manager update --ref "ltt" [--alias-new "ltt2"] \
#     [--max-results 50] [--enabled false]
#   python -m src.yt_channel_manager enable --ref "ltt"
#   python -m src.yt_channel_manager disable --ref "ltt"
#   python -m src.yt_channel_manager videos --ref "ltt" [--max-results 20] \
#     [--since 2026-09-01] [--output-stdout]
# 作为 Agent 工具调用（暴露标准接口，JSON 直出 stdout）：
#   python -m src.yt_channel_manager agent --action add \
#     --params-json '{"url": "https://www.youtube.com/@handle", "alias": "ltt"}' --output-stdout

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import yaml

from .config_loader import (
    DEFAULT_CHANNELS_CONFIG,
    AppConfig,
    ChannelConfig,
    load_app_config,
    load_channels,
)
from .errors import (
    ChannelAlreadyExistsError,
    ChannelNotFoundError,
    NexaYTError,
)
from .logger import get_logger
from .types import (
    ChannelManagerOutput,
    ChannelSubscription,
    ChannelVideosOutput,
    YouTubeItem,
)
from .ydl_base import YDLBase
from .yt_channel_scanner import STATE_FILENAME, YTChannelScanner, _parse_since


def _identity_key(url: str) -> str:
    """提取频道身份键：@handle 取小写 handle；其余取 host+path 小写并去尾斜杠。"""
    text = url.strip()
    if text.startswith("@"):
        return text.lower()
    parsed = urlsplit(text if "://" in text else f"https://{text}")
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    parts = [part for part in parsed.path.split("/") if part]
    # @handle 形式（可能带 /videos 尾巴）：以 handle 本身作为身份
    if host == "youtube.com" and parts and parts[0].startswith("@"):
        return parts[0].lower()
    return f"{host}/{'/'.join(parts)}".lower().rstrip("/")


class YTChannelManager(YDLBase):
    """频道管理器：channels.yaml 的增删改查 + enable/disable + 视频列表。"""

    def __init__(
        self,
        app_config: Optional[AppConfig] = None,
        channels_path: Path | str = DEFAULT_CHANNELS_CONFIG,
    ) -> None:
        super().__init__(app_config)
        self.channels_path = Path(channels_path)

    # ------------------------------------------------------------------ 持久化

    def _load(self) -> list[ChannelConfig]:
        return load_channels(self.channels_path)

    def _save(self, channels: list[ChannelConfig]) -> None:
        payload = {
            "channels": [
                {
                    "url": c.url,
                    "alias": c.alias,
                    "max_results": c.max_results,
                    "enabled": c.enabled,
                }
                for c in channels
            ]
        }
        self.channels_path.parent.mkdir(parents=True, exist_ok=True)
        self.channels_path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        self.logger.info("频道配置已写回: %s（共 %d 个）", self.channels_path, len(channels))

    @staticmethod
    def _subscription(config: ChannelConfig) -> ChannelSubscription:
        return ChannelSubscription(**asdict(config))

    def _all_subscriptions(self, channels: list[ChannelConfig]) -> list[ChannelSubscription]:
        return [self._subscription(c) for c in channels]

    # ------------------------------------------------------------------ 定位

    def _find(self, channels: list[ChannelConfig], ref: str) -> ChannelConfig:
        """按 alias（大小写不敏感）→ url 归一化 定位频道，失败抛 ChannelNotFoundError。"""
        needle = ref.strip()
        for config in channels:
            if config.alias and config.alias.lower() == needle.lower():
                return config
        key = _identity_key(needle)
        for config in channels:
            if _identity_key(config.url) == key:
                return config
        raise ChannelNotFoundError(f"频道不存在: {ref}")

    def _check_alias_conflict(
        self, channels: list[ChannelConfig], alias: str, exclude_url: Optional[str] = None
    ) -> None:
        """alias 非空时全局唯一（大小写不敏感），exclude_url 用于更新时排除自身。"""
        if not alias:
            return
        for config in channels:
            if exclude_url is not None and _identity_key(config.url) == _identity_key(exclude_url):
                continue
            if config.alias and config.alias.lower() == alias.lower():
                raise ChannelAlreadyExistsError(f"alias 已被占用: {alias}（{config.url}）")

    # ------------------------------------------------------------------ 查询

    def list_channels(self) -> ChannelManagerOutput:
        channels = self._load()
        self.logger.info("当前订阅频道 %d 个", len(channels))
        return ChannelManagerOutput(
            generated_at=datetime.now(),
            action="list",
            channels=self._all_subscriptions(channels),
        )

    # ------------------------------------------------------------------ 新增

    def _validate_new_url(self, url: str) -> str:
        text = url.strip()
        if not text:
            raise ValueError("频道 URL 不能为空")
        if not (text.startswith("@") or "youtube.com" in text.lower()):
            raise ValueError(f"仅支持 YouTube 频道 URL 或 @handle: {url}")
        return text

    async def _verify_channel(self, url: str) -> dict[str, Any]:
        """联网校验频道是否存在（extract_flat 拉 1 条），返回 info_dict。"""
        cookie_file = await self._get_cookie_file()
        opts = await self._get_ydl_opts(cookie_file)
        opts["extract_flat"] = "in_playlist"
        opts["playlistend"] = 1
        target = YTChannelScanner._videos_tab_url(url)
        info = await self._extract_info(target, opts, "channel verify")
        if not info or not (info.get("channel") or info.get("channel_id") or info.get("title")):
            raise ChannelNotFoundError(f"无法访问或不存在该频道: {url}")
        return info

    async def add_channel(
        self,
        url: str,
        alias: str = "",
        max_results: int = 10,
        enabled: bool = True,
        verify: bool = True,
    ) -> ChannelManagerOutput:
        url = self._validate_new_url(url)
        alias = alias.strip()
        channels = self._load()

        key = _identity_key(url)
        if any(_identity_key(c.url) == key for c in channels):
            raise ChannelAlreadyExistsError(f"频道已存在: {url}")
        self._check_alias_conflict(channels, alias)

        if verify:
            info = await self._verify_channel(url)
            title = (info.get("channel") or info.get("title") or "").strip()
            self.logger.info("频道校验通过: %s（title=%s）", url, title)
            if not alias and title:
                alias = title  # 未指定 alias 时以频道标题补全

        config = ChannelConfig(url=url, alias=alias, max_results=max_results, enabled=enabled)
        channels.append(config)
        self._save(channels)
        return ChannelManagerOutput(
            generated_at=datetime.now(),
            action="add",
            channel=self._subscription(config),
            channels=self._all_subscriptions(channels),
        )

    # ------------------------------------------------------------------ 删除

    def _prune_state(self, url: str) -> None:
        """删除频道时同步清理 channel_state.json 中的增量游标。"""
        state_path = self.app_config.ensure_output_dir() / STATE_FILENAME
        if not state_path.exists():
            return
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            self.logger.warning("状态文件损坏，跳过清理: %s (%s)", state_path, error)
            return
        entry = (state.get("channels") or {}).get(url)
        if entry is None:
            return
        del state["channels"][url]
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.logger.info("已清理频道增量游标: %s", url)

    def remove_channel(self, ref: str) -> ChannelManagerOutput:
        channels = self._load()
        config = self._find(channels, ref)
        channels.remove(config)
        self._save(channels)
        self._prune_state(config.url)
        return ChannelManagerOutput(
            generated_at=datetime.now(),
            action="remove",
            channel=self._subscription(config),
            channels=self._all_subscriptions(channels),
        )

    # ------------------------------------------------------------------ 更新

    def update_channel(
        self,
        ref: str,
        alias: Optional[str] = None,
        max_results: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> ChannelManagerOutput:
        channels = self._load()
        config = self._find(channels, ref)

        if alias is not None:
            alias = alias.strip()
            self._check_alias_conflict(channels, alias, exclude_url=config.url)
            config.alias = alias
        if max_results is not None:
            if max_results < 1:
                raise ValueError(f"max_results 必须 >= 1: {max_results}")
            config.max_results = max_results
        if enabled is not None:
            config.enabled = enabled

        self._save(channels)
        return ChannelManagerOutput(
            generated_at=datetime.now(),
            action="update",
            channel=self._subscription(config),
            channels=self._all_subscriptions(channels),
        )

    def enable_channel(self, ref: str) -> ChannelManagerOutput:
        output = self.update_channel(ref, enabled=True)
        output.action = "enable"
        return output

    def disable_channel(self, ref: str) -> ChannelManagerOutput:
        output = self.update_channel(ref, enabled=False)
        output.action = "disable"
        return output

    # ------------------------------------------------------------------ 视频列表

    async def get_channel_videos(
        self,
        ref: str,
        max_results: Optional[int] = None,
        since: Optional[str] = None,
    ) -> ChannelVideosOutput:
        """获取订阅频道视频列表（委托 YTChannelScanner，不更新增量游标）。"""
        config = self._find(self._load(), ref)
        limit = max_results or config.max_results
        items: list[YouTubeItem] = []
        async with YTChannelScanner(self.app_config) as scanner:
            async for item in scanner.channel_scan(config.url, limit=limit, since=since):
                items.append(item)
        return ChannelVideosOutput(
            generated_at=datetime.now(),
            channel=self._subscription(config),
            items=items,
            failed_urls=[],
        )


# ---------------------------------------------------------------------- CLI


def _emit(
    action: str,
    payload: ChannelManagerOutput | ChannelVideosOutput,
    config: AppConfig,
    output_stdout: bool,
    output: Optional[str] = None,
) -> None:
    """统一出口：--output-stdout 时 JSON 直出 stdout，否则写入结果文件。"""
    if output_stdout:
        print(payload.model_dump_json(indent=2))
        return
    if output:
        path = Path(output)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = config.ensure_output_dir() / f"yt_channel_manage_{action}_{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
    get_logger(__name__).info("结果已写入: %s", path)


async def _dispatch(manager: YTChannelManager, action: str, params: dict[str, Any]) -> Any:
    """按 action 分发到管理器方法（agent 模式与 CLI 共用）。"""
    if action == "list":
        return manager.list_channels()
    if action == "add":
        return await manager.add_channel(
            url=str(params.get("url") or ""),
            alias=str(params.get("alias") or ""),
            max_results=int(params.get("max_results", 10)),
            enabled=bool(params.get("enabled", True)),
            verify=bool(params.get("verify", True)),
        )
    if action == "remove":
        return manager.remove_channel(ref=str(params.get("ref") or ""))
    if action == "update":
        return manager.update_channel(
            ref=str(params.get("ref") or ""),
            alias=params.get("alias"),
            max_results=params.get("max_results"),
            enabled=params.get("enabled"),
        )
    if action == "enable":
        return manager.enable_channel(ref=str(params.get("ref") or ""))
    if action == "disable":
        return manager.disable_channel(ref=str(params.get("ref") or ""))
    if action == "videos":
        return await manager.get_channel_videos(
            ref=str(params.get("ref") or ""),
            max_results=params.get("max_results"),
            since=params.get("since"),
        )
    raise ValueError(f"未知 action: {action}")


_AGENT_ACTIONS = ("list", "add", "remove", "update", "enable", "disable", "videos")


async def _run(args: argparse.Namespace) -> None:
    config = load_app_config()
    manager = YTChannelManager(config, channels_path=args.config)

    if args.command == "agent":
        if args.action not in _AGENT_ACTIONS:
            raise ValueError(f"未知 action: {args.action}")
        params = json.loads(args.params_json or "{}")
        if not isinstance(params, dict):
            raise ValueError("--params-json 必须是 JSON 对象")
        payload = await _dispatch(manager, args.action, params)
        _emit(args.action, payload, config, output_stdout=True)
        return

    # CLI 子命令：构造 params 后走同一分发逻辑
    if args.command == "add":
        params: dict[str, Any] = {
            "url": args.url,
            "alias": args.alias or "",
            "max_results": args.max_results or 10,
            "enabled": not args.disabled,
            "verify": not args.no_verify,
        }
    elif args.command == "update":
        params = {"ref": args.ref, "alias": args.alias_new, "max_results": args.max_results, "enabled": _parse_bool(args.enabled)}
    elif args.command == "videos":
        params = {"ref": args.ref, "max_results": args.max_results, "since": _parse_since(args.since)}
    else:  # list / remove / enable / disable
        params = {"ref": getattr(args, "ref", "")}

    payload = await _dispatch(manager, args.command, params)
    _emit(args.command, payload, config, args.output_stdout, args.output)


def _parse_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    return value.strip().lower() in ("1", "true", "yes", "on")


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="YouTube 订阅频道管理器")
    parser.add_argument("--config", default=str(DEFAULT_CHANNELS_CONFIG), help="频道列表配置")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出全部订阅频道")
    sub.add_parser("remove", help="删除频道").add_argument("--ref", required=True, help="alias 或 URL")
    sub.add_parser("enable", help="启用频道").add_argument("--ref", required=True)
    sub.add_parser("disable", help="停用频道").add_argument("--ref", required=True)

    add_parser = sub.add_parser("add", help="新增订阅频道")
    add_parser.add_argument("--url", required=True, help="频道 URL 或 @handle")
    add_parser.add_argument("--alias", help="别名（未指定且校验通过时以频道标题补全）")
    add_parser.add_argument("--max-results", type=int, default=10)
    add_parser.add_argument("--disabled", action="store_true", help="以停用状态添加")
    add_parser.add_argument("--no-verify", action="store_true", help="跳过联网校验")

    update_parser = sub.add_parser("update", help="更新频道配置")
    update_parser.add_argument("--ref", required=True, help="alias 或 URL")
    update_parser.add_argument("--alias-new", dest="alias_new", help="新别名")
    update_parser.add_argument("--max-results", type=int)
    update_parser.add_argument("--enabled", help="true / false")

    videos_parser = sub.add_parser("videos", help="获取频道视频列表")
    videos_parser.add_argument("--ref", required=True, help="alias 或 URL")
    videos_parser.add_argument("--max-results", type=int)
    videos_parser.add_argument("--since", help="起始日期 YYYY-MM-DD / YYYYMMDD / ISO8601")
    videos_parser.add_argument("--output", help="覆盖输出路径")
    videos_parser.add_argument("--output-stdout", action="store_true", help="JSON 直出 stdout")

    for name in ("list", "add", "remove", "update", "enable", "disable"):
        sub.choices[name].add_argument("--output", help="覆盖输出路径")
        sub.choices[name].add_argument("--output-stdout", action="store_true", help="JSON 直出 stdout")

    agent_parser = sub.add_parser("agent", help="Agent 工具接口")
    agent_parser.add_argument("--action", required=True, help=f"动作: {'|'.join(_AGENT_ACTIONS)}")
    agent_parser.add_argument("--params-json", help='JSON 对象，如 \'{"ref": "ltt"}\'')
    agent_parser.add_argument("--output-stdout", action="store_true")

    args = parser.parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (NexaYTError, ValueError) as error:
        parser.exit(2, f"错误: {error}\n")


if __name__ == "__main__":
    main()
