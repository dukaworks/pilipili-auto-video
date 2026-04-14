import os
import subprocess

# ============================================================
# 文件上传 API（角色参考图 + 对标视频）
# ============================================================

UPLOAD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "uploads", "references"
)
VIDEO_UPLOAD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "uploads",
    "reference_videos",
)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(VIDEO_UPLOAD_DIR, exist_ok=True)

def _extract_frame_from_video(video_path: str, output_path: str) -> str:
    """
    从视频中提取最清晰的一帧作为参考图。
    策略：取视频 1/3 处的帧（通常比第一帧更有代表性）
    """
    try:
        # 获取视频时长
        probe_cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", video_path]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
        duration = 1.0
        if probe_result.returncode == 0:
            import json as _json

            info = _json.loads(probe_result.stdout)
            duration = float(info.get("format", {}).get("duration", 3.0))

        # 取 1/3 处的帧
        seek_time = duration / 3

        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            str(seek_time),
            "-i",
            video_path,
            "-vframes",
            "1",
            "-q:v",
            "1",  # 最高质量 JPEG
            output_path,
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        return output_path
    except Exception as e:
        raise RuntimeError(f"视频截帧失败: {e}")





if __name__ == "__main__":
    print("Upload directory for reference images:", UPLOAD_DIR)
    print("Upload directory for reference videos:", VIDEO_UPLOAD_DIR)