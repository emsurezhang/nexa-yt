"""临时脚本：批量抓取 Graph Engineering 选题候选视频详情（复用 fetcher 实例）。"""
import asyncio
import json
from pathlib import Path

from src.config_loader import load_app_config
from src.yt_video_detail_fetcher import YTVideoDetailFetcher

VIDEOS = [
    ("IrW0_f-w4kA", "google_graph_engineering_101"),
    ("JWhICz1QR8M", "greg_isenberg_graph_engineering"),
    ("8RedSkw1UjE", "zuojiapaidang_graph_engineering_cn"),
    ("qAF1NjEVHhY", "ibm_langchain_vs_langgraph"),
    ("XdbpCM4yGyE", "karpathy_keep_graph"),
]

OUT_DIR = Path("data/graph_engineering_topic")


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    config = load_app_config()
    async with YTVideoDetailFetcher(config) as fetcher:
        for vid, name in VIDEOS:
            try:
                out = await fetcher.fetch(video_id=vid, langs=["zh-CN", "zh-Hans", "en"])
                path = OUT_DIR / f"{name}.json"
                path.write_text(out.model_dump_json(indent=2), encoding="utf-8")
                n_tracks = len(out.subtitles.tracks)
                n_segs = sum(len(t.segments) for t in out.subtitles.tracks)
                print(f"OK {vid} -> {path} tracks={n_tracks} segments={n_segs}")
            except Exception as e:  # noqa: BLE001
                print(f"FAIL {vid}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
