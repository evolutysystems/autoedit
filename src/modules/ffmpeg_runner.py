# FFmpeg 共通実行ラッパ
# 設計書 5.6 ffmpeg_runner.py に対応
import json
import shutil
import subprocess

from ..exceptions import FFmpegError
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags
from ..utils.progress import run_ffmpeg_progress

_logger = get_logger(__name__)


# FFmpeg 設定から実行ファイルパスを取得する
def get_ffmpeg_exe(ffmpeg_settings):
    return ffmpeg_settings.get("executable", "ffmpeg")


# ffprobe 実行ファイルパスを取得する
def get_ffprobe_exe(ffmpeg_settings):
    return ffmpeg_settings.get("ffprobe_executable", "ffprobe")


# 無進捗タイムアウト秒数を設定から取得する (0 以下で監視無効)
def get_progress_timeout_sec(ffmpeg_settings):
    try:
        return int(ffmpeg_settings.get("progress_timeout_sec", 0))
    except (TypeError, ValueError):
        return 0


# 出力フレームレート(CFR 正規化用)を設定から取得する (不正値は既定 60)
# VFR/タイムスタンプ破損対策で、再エンコード時の CFR 化に使用する (resolve 20260630 対策A/B)
def get_output_fps(ffmpeg_settings):
    try:
        fps = int(ffmpeg_settings.get("output_fps", 60))
    except (TypeError, ValueError):
        return 60
    return fps if fps > 0 else 60


# 実行可能性チェック (FFmpeg/ffprobe が PATH に存在するか)
def ensure_available(ffmpeg_settings):
    for exe in (get_ffmpeg_exe(ffmpeg_settings), get_ffprobe_exe(ffmpeg_settings)):
        if shutil.which(exe) is None:
            raise FFmpegError(f"FFmpeg 実行ファイルが見つかりません: {exe}")


# 動画の総再生時間を ffprobe で取得する
def probe_duration(input_path, ffmpeg_settings):
    ffprobe = get_ffprobe_exe(ffmpeg_settings)
    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        input_path,
    ]
    _logger.debug("ffprobe 実行: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            # GUI(windowed)実行時にコンソール窓を出さない (Windows のみ有効)
            creationflags=no_window_creationflags(),
        )
    except FileNotFoundError as e:
        raise FFmpegError(f"ffprobe 実行に失敗: {e}") from e

    if result.returncode != 0:
        raise FFmpegError(
            "ffprobe が失敗",
            command=cmd,
            stderr_tail=result.stderr,
            returncode=result.returncode,
        )

    try:
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        raise FFmpegError(f"ffprobe 出力解析に失敗: {e}") from e


# 進捗通知付きで FFmpeg コマンドを実行する
# 失敗時は FFmpegError を送出
# progress_timeout_sec : 無進捗タイムアウト秒数 (0 以下で監視無効)
def execute(command, total_duration=0.0, on_progress=None, progress_timeout_sec=0):
    returncode, stderr_tail = run_ffmpeg_progress(
        command,
        total_duration=total_duration,
        on_progress=on_progress,
        progress_timeout_sec=progress_timeout_sec,
    )
    if returncode != 0:
        _logger.error("FFmpeg 失敗 (code=%s): %s", returncode, stderr_tail)
        raise FFmpegError(
            f"FFmpeg 実行失敗 (returncode={returncode})",
            command=command,
            stderr_tail=stderr_tail,
            returncode=returncode,
        )
    return returncode


# 出力品質オプションを setting.json の ffmpeg セクションから構築する
def build_encode_options(ffmpeg_settings):
    return [
        "-c:v", ffmpeg_settings.get("video_codec", "libx264"),
        "-preset", ffmpeg_settings.get("preset", "medium"),
        "-crf", str(ffmpeg_settings.get("crf", 20)),
        "-c:a", ffmpeg_settings.get("audio_codec", "aac"),
    ]
