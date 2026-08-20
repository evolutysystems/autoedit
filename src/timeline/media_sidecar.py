# 正規化済み音声のサイドカー (docs/request/ver3/resolve9.md §3-1 案D / §5.5-a)
#
# ラウドネス正規化は映像を -c:v copy で通し、音声だけを AAC へ焼き直す
# (loudness_normalizer.py の適用パス)。つまり保存が要るのは音声だけで、
# 映像は元動画から同じ引数で切り直せば同じものが手に入る。
#
# そこで保存時には「正規化済み素材から音声だけ」を抜いて残しておき、
# 開くときは「切り出した映像 + 残した音声」を再エンコードなしで貼り合わせる。
# 測定も再エンコードも無いため、開くまでが数分から数十秒へ縮む。
import os

from ..exceptions import FFmpegError
from ..modules import ffmpeg_runner
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 音声コーデック → (サイドカーの拡張子, 復元先の拡張子)
# MP4 に入らないコーデックへ設定を変えられても壊れないよう Matroska へ倒す。
_SUFFIXES = {
    "aac": (".m4a", ".mp4"),
    "alac": (".m4a", ".mp4"),
    "mp3": (".mp3", ".mp4"),
}
_FALLBACK_SUFFIXES = (".mka", ".mkv")

# 尺の比較に使う既定の許容 (秒)。設定 timeline.project.sidecar_tolerance_sec で変えられる。
DEFAULT_TOLERANCE_SEC = 0.5


# 音声コーデックからサイドカーと復元先の拡張子を決める
# 戻り値: (サイドカーの拡張子, 復元先の拡張子)
def suffixes_for(ffmpeg_cfg):
    codec = str((ffmpeg_cfg or {}).get("audio_codec", "aac") or "aac").strip().lower()
    return _SUFFIXES.get(codec, _FALLBACK_SUFFIXES)


# メディア 1 件ぶんのサイドカーのパスを組む (<素材フォルダ>/<media_id>_audio.<拡張子>)
def sidecar_path(media_dir, media_id, ffmpeg_cfg):
    suffix, _container = suffixes_for(ffmpeg_cfg)
    return os.path.join(media_dir, f"{media_id}_audio{suffix}")


# 正規化済み素材から音声だけを再エンコードなしで抜き出す
#   ffmpeg -i <src> -vn -map 0:a:0 -c:a copy <dest>
# 戻り値: 書き出したパス / 音声が無い・失敗した場合は None
def export_audio(src_path, dest_path, ffmpeg_cfg):
    if not src_path or not os.path.exists(src_path):
        return None
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg or {})
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    cmd = [
        ffmpeg, "-y", "-hide_banner",
        "-i", src_path,
        "-vn",
        "-map", "0:a:0",
        "-c:a", "copy",
        dest_path,
    ]
    try:
        ffmpeg_runner.execute(cmd)
    except FFmpegError:
        # 音声が無い素材・抜き出せない容器でも保存そのものは続ける
        _logger.warning("音声サイドカーを作れませんでした (保存は続行します): %s", src_path)
        _remove(dest_path)
        return None
    if not os.path.exists(dest_path) or os.path.getsize(dest_path) <= 0:
        _remove(dest_path)
        return None
    _logger.info("音声サイドカーを保存しました: %s (%.1f MB)",
                 dest_path, os.path.getsize(dest_path) / 1024 / 1024)
    return dest_path


# 映像と音声を再エンコードなしで多重化する
#   ffmpeg -i <video> -i <audio> -map 0:v:0 -map 1:a:0 -c copy <dest>
# 戻り値: 書き出したパス / 失敗した場合は None
def mux(video_path, audio_path, dest_path, ffmpeg_cfg):
    if not (video_path and os.path.exists(video_path)):
        return None
    if not (audio_path and os.path.exists(audio_path)):
        return None
    ffmpeg = ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg or {})
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    cmd = [
        ffmpeg, "-y", "-hide_banner",
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c", "copy",
        dest_path,
    ]
    try:
        ffmpeg_runner.execute(cmd)
    except FFmpegError:
        _logger.warning("映像と音声の多重化に失敗しました: %s + %s", video_path, audio_path)
        _remove(dest_path)
        return None
    if not os.path.exists(dest_path) or os.path.getsize(dest_path) <= 0:
        _remove(dest_path)
        return None
    return dest_path


# サイドカーが保存時の素材と食い違っていないかを尺で確かめる (§5.5-a)
# VOD を差し替えた・設定を変えたといった食い違いを検知して、
# 誤った音声を貼らずに再正規化のフォールバックへ落とすために使う。
def matches_duration(audio_path, expected_sec, ffmpeg_cfg, tolerance_sec=None):
    expected = float(expected_sec or 0.0)
    if expected <= 0:
        return True         # 比べる基準が無ければ通す (保存値が無い旧データ)
    tolerance = float(DEFAULT_TOLERANCE_SEC if tolerance_sec is None else tolerance_sec)
    try:
        actual = ffmpeg_runner.probe_duration(audio_path, ffmpeg_cfg or {})
    except (FFmpegError, OSError, ValueError, KeyError):
        _logger.warning("音声サイドカーの尺を取得できませんでした: %s", audio_path)
        return False
    if abs(float(actual) - expected) <= tolerance:
        return True
    _logger.warning(
        "音声サイドカーの尺が保存値と合いません (%.2fs / 期待 %.2fs) → 使いません",
        float(actual), expected)
    return False


def _remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
