from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional

@dataclass
class ChannelItem():
    """YouTube 频道内容模型"""
    url: str                                # 原文链接（必填，全局唯一标识 / 增量游标）
    title: str                              # 标题
    description: str = ""                   # 描述
    thumbNail: str = ""      
    channel_id: str = ""        
    channel_follower_count: Optional[int] = None

@dataclass
class YouTubeItem():
    """YouTube 视频内容模型"""
    url: str                                # 原文链接（必填，全局唯一标识 / 增量游标）
    title: str                              # 标题
    description: str = ""                   # 描述
    author: str = ""
    published_at: Optional[datetime] = None    
    thumbNail: str = ""                     # URL/ base64 Data    
    video_id: str = ""    
    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    subtitle: Optional[str] = None
    audio_path: Optional[str] = None
    channel_id: str = ""
    channel_url: str = ""
    channel_follower_count: Optional[int] = None
    raw: dict = field(default_factory=dict)  

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（datetime 等需配合 json_default 使用）。"""
        return asdict(self)


# ---------------------------------------------------------------------------
# JSON 序列化辅助
# ---------------------------------------------------------------------------

def json_default(o: Any) -> str:
    """json.dump 的 default 回调：datetime → ISO 字符串。"""
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


# ---------------------------------------------------------------------------
# Pydantic 输出模型（对外 JSON 结构校验）
# ---------------------------------------------------------------------------

try:
    from pydantic import BaseModel, Field
except ImportError:  # 允许在仅使用 dataclass 的场景下工作
    BaseModel = None  # type: ignore[assignment,misc]
    Field = None  # type: ignore[assignment]


if BaseModel is not None:

    class SubtitleSegment(BaseModel):
        """单条字幕片段。"""
        start: float
        end: float
        text: str

    class SubtitlesContainer(BaseModel):
        """视频字幕的连续纯文本。"""
        text: str = ""

    class VideoDetail(BaseModel):
        """视频完整元数据（由 yt-dlp info_dict 转换）。"""
        video_id: str
        url: str
        title: str
        description: str = ""
        author: str = ""
        channel_id: str = ""
        channel_url: str = ""
        channel_follower_count: Optional[int] = None
        duration: Optional[float] = None
        view_count: Optional[int] = None
        like_count: Optional[int] = None
        upload_date: Optional[str] = None          # YYYYMMDD
        published_at: Optional[datetime] = None    # 解析后的时间
        thumbnail: str = ""
        categories: list[str] = Field(default_factory=list)
        tags: list[str] = Field(default_factory=list)

    class FetchMeta(BaseModel):
        """抓取过程元信息。"""
        operation: str
        url: str
        fetched_at: datetime
        elapsed_seconds: float = 0.0
        proxy_used: bool = False
        cookie_used: bool = False

    class VideoFetchOutput(BaseModel):
        """单视频抓取输出根模型。"""
        meta: FetchMeta
        detail: VideoDetail
        subtitles: SubtitlesContainer = Field(default_factory=SubtitlesContainer)

    class FilterVerdict(BaseModel):
        """内容过滤评估结果。status: matched | not_matched | needs_review。"""
        engine: str
        status: str
        score: float = 0.0
        match_threshold: float = 0.6
        needs_review_threshold: float = 0.3
        dimensions: dict[str, bool] = Field(default_factory=dict)
        reason: str = ""

    class SearchItem(BaseModel):
        """单条搜索结果：视频信息 + 过滤判定。"""
        video: YouTubeItem
        subtitle: Optional[str] = None
        verdict: Optional[FilterVerdict] = None

    class SearchOutput(BaseModel):
        """关键词搜索输出根模型。"""
        generated_at: datetime
        keyword: str
        total: int = 0
        items: list[SearchItem] = Field(default_factory=list)

    class ChannelResult(BaseModel):
        """单个频道的扫描结果。"""
        url: str
        alias: str = ""
        items: list[YouTubeItem] = Field(default_factory=list)
        failed_urls: list[str] = Field(default_factory=list)

    class ChannelScanOutput(BaseModel):
        """频道扫描输出根模型。"""
        generated_at: datetime
        channels: list[ChannelResult] = Field(default_factory=list)

    class ChannelSubscription(BaseModel):
        """订阅频道条目（channels.yaml 的对外表示）。"""
        url: str
        alias: str = ""
        max_results: int = 10
        enabled: bool = True

    class ChannelManagerOutput(BaseModel):
        """频道管理操作输出根模型。action: list | add | remove | update | enable | disable。"""
        generated_at: datetime
        action: str
        channel: Optional[ChannelSubscription] = None  # 受影响的频道（list 时为 None）
        channels: list[ChannelSubscription] = Field(default_factory=list)  # 操作后的全量订阅列表

    class ChannelVideosOutput(BaseModel):
        """频道视频列表输出根模型。"""
        generated_at: datetime
        channel: ChannelSubscription
        items: list[YouTubeItem] = Field(default_factory=list)
        failed_urls: list[str] = Field(default_factory=list)
        