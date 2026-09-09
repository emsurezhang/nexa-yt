#!/usr/bin/env python3
"""
m4a 转 mp3 工具

用法:
    python m4a2mp3.py <原文路径> <目标文件路径>

示例:
    python m4a2mp3.py ./music/song.m4a ./music/song.mp3

依赖: 需要先安装 ffmpeg，并保证在系统 PATH 中可用。
"""

import argparse
import sys
from pathlib import Path
import subprocess


def convert_m4a_to_mp3(src: str, dst: str) -> None:
    src_path = Path(src)
    dst_path = Path(dst)

    # 基本校验
    if not src_path.exists():
        sys.exit(f"错误: 原文文件不存在 -> {src_path}")
    if src_path.suffix.lower() != ".m4a":
        sys.exit(f"错误: 原文文件不是 .m4a 格式 -> {src_path}")

    # 如果目标文件没写 .mp3 后缀，自动补上
    if dst_path.suffix.lower() != ".mp3":
        dst_path = dst_path.with_suffix(".mp3")

    # 确保目标目录存在
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    # 调用 ffmpeg 转码:
    #   -vn            去掉视频流
    #   -codec:a libmp3lame  使用 mp3 编码器
    #   -q:a 2         高质量（约 190kbps，VBR）
    cmd = [
        "ffmpeg",
        "-y",                       # 覆盖已存在的目标文件
        "-i", str(src_path),
        "-vn",
        "-codec:a", "libmp3lame",
        "-q:a", "2",
        str(dst_path),
    ]

    print(f"正在转换: {src_path} -> {dst_path}")
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("错误: 未找到 ffmpeg，请先安装（如: apt install ffmpeg / brew install ffmpeg）")
    except subprocess.CalledProcessError as e:
        sys.exit(f"转换失败:\n{e.stderr}")

    print(f"转换完成: {dst_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="将 m4a 音频转换为 mp3")
    parser.add_argument("src", help="原文 m4a 文件路径")
    parser.add_argument("dst", help="目标 mp3 文件路径")
    args = parser.parse_args()

    convert_m4a_to_mp3(args.src, args.dst)