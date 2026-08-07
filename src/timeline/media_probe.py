# メディア判定 (docs/request/ver3/resolve.md §6.8)
# Timeline へ追加する素材 (元動画 / OP / ED / D&D したファイル) の
# 種別・尺・表示寸法・フレームレート・音声有無を 1 回の ffprobe でまとめて取得する。
# 判定に失敗しても Timeline 構築を止めないよう、既定値へフォールバックして返す。
import json
import os
import subprocess

from ..exceptions import FFmpegError
from ..modules import ffmpeg_runner
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags
from .model import MEDIA_IMAGE, MEDIA_VIDEO, MediaRef

_logger = get_logger(__name__)

# 拡張子の既定 (setting.json timeline.media.* が未指定のときのフォールバック)
DEFAULT_VIDEO_EXTENSIONS = (".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv")
DEFAULT_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")

# 寸法が取れなかった場合のフォールバック (キャンバス側で正規化されるため実害は小さい)
_FALLBACK_WIDTH = 1920
_FALLBACK_HEIGHT = 1080


# timeline.media 設定を既定値で補完して返す
def media_config(settings):
    cfg = ((settings or {}).get("timeline", {}) or {}).get("media", {}) or {}
    return {
        "video_extensions": tuple(
            str(e).lower() for e in cfg.get("video_extensions", DEFAULT_VIDEO_EXTENSIONS)),
        "image_extensions": tuple(
            str(e).lower() for e in cfg.get("image_extensions", DEFAULT_IMAGE_EXTENSIONS)),
        "default_image_duration_sec": float(cfg.get("default_image_duration_sec", 5.0)),
        "max_video_tracks": int(cfg.get("max_video_tracks", 8)),
    }


# パスの拡張子からメディア種別を判定する (対応外は None)
def classify(path, cfg=None):
    cfg = cfg or media_config(None)
    ext = os.path.splitext(str(path))[1].lower()
    if ext in cfg["image_extensions"]:
        return MEDIA_IMAGE
    if ext in cfg["video_extensions"]:
        return MEDIA_VIDEO
    return None


# D&D で受理できるファイルか (種別が判定でき、実在すること)
def is_supported(path, cfg=None):
    return bool(path) and os.path.exists(path) and classify(path, cfg) is not None


# ffprobe で format + streams をまとめて取得する (失敗時は None)
def _probe_raw(path, ffmpeg_settings):
    ffprobe = ffmpeg_runner.get_ffprobe_exe(ffmpeg_settings or {})
    cmd = [
        ffprobe, "-v", "error",
        "-show_format", "-show_streams",
        "-of", "json", path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=no_window_creationflags(),
        )
    except (OSError, FileNotFoundError) as e:
        _logger.warning("ffprobe の実行に失敗しました: %s (%s)", path, e)
        return None
    if result.returncode != 0:
        _logger.warning("ffprobe が失敗しました: %s", path)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        _logger.warning("ffprobe 出力を解析できませんでした: %s", path)
        return None


# "60/1" 形式のフレームレート表記を float へ変換する
def _parse_fps(text):
    if not text:
        return 0.0
    try:
        if "/" in str(text):
            num, _, den = str(text).partition("/")
            denominator = float(den)
            return float(num) / denominator if denominator else 0.0
        return float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


# 映像ストリームの回転量を加味した表示寸法を返す (ffmpeg_runner._extract_rotation と同方針)
def _display_size(stream):
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    rotation = 0
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            try:
                rotation = int(round(float(side["rotation"])))
            except (TypeError, ValueError):
                rotation = 0
            break
    else:
        rotate = (stream.get("tags", {}) or {}).get("rotate")
        if rotate is not None:
            try:
                rotation = int(round(float(rotate)))
            except (TypeError, ValueError):
                rotation = 0
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return width, height


# メディア 1 件を調べて MediaRef を作る
# media_id はプールへ登録する ID。settings は ffprobe 解決と既定尺の取得に使う。
def probe(path, media_id, settings=None, cfg=None):
    cfg = cfg or media_config(settings)
    ffmpeg_settings = (settings or {}).get("ffmpeg", {})
    kind = classify(path, cfg) or MEDIA_VIDEO

    raw = _probe_raw(path, ffmpeg_settings)
    if raw is None:
        # 判定できなくても Timeline 構築は止めない (§10)
        return MediaRef(
            media_id, kind, path,
            duration_sec=None if kind == MEDIA_IMAGE else 0.0,
            width=_FALLBACK_WIDTH, height=_FALLBACK_HEIGHT,
            fps=0.0, has_audio=False,
        )

    streams = raw.get("streams", []) or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    width, height, fps = _FALLBACK_WIDTH, _FALLBACK_HEIGHT, 0.0
    if video_streams:
        width, height = _display_size(video_streams[0])
        fps = _parse_fps(video_streams[0].get("avg_frame_rate")
                         or video_streams[0].get("r_frame_rate"))
        if not width or not height:
            width, height = _FALLBACK_WIDTH, _FALLBACK_HEIGHT

    duration = None
    if kind != MEDIA_IMAGE:
        try:
            duration = float((raw.get("format", {}) or {}).get("duration"))
        except (TypeError, ValueError):
            duration = 0.0

    return MediaRef(
        media_id, kind, path,
        duration_sec=duration,
        width=width, height=height, fps=fps,
        has_audio=bool(audio_streams),
    )


# 素材の既定クリップ尺を返す (画像は設定値、動画は素材尺)
def default_clip_duration(media, cfg=None, settings=None):
    cfg = cfg or media_config(settings)
    if media.is_image():
        return float(cfg["default_image_duration_sec"])
    return float(media.duration_sec or 0.0)


# キャンバス規格と一致するか (レンダリング時に正規化チェーンを挟むかの判定 / §8.2)
# 画像は必ず正規化が要る。fps は動画のみ比較する。
def needs_normalize(media, canvas_width, canvas_height, canvas_fps):
    if media is None:
        return True
    if media.is_image():
        return True
    if int(media.width) != int(canvas_width) or int(media.height) != int(canvas_height):
        return True
    # フレームレートは丸め差を許容する (29.97 と 30 を別物にしない)
    if media.fps and abs(float(media.fps) - float(canvas_fps)) > 0.5:
        return True
    return False


# 総尺を取得する薄いラッパ (probe を通さず尺だけ欲しい箇所向け)
def duration_of(path, settings):
    try:
        return float(ffmpeg_runner.probe_duration(path, (settings or {}).get("ffmpeg", {})))
    except (FFmpegError, OSError, TypeError, ValueError):
        _logger.warning("総尺の取得に失敗しました: %s", path)
        return 0.0
