# 素材の区間を 1 枚ずつ取り出す (ver5 resolve8 §5.1 / 旧 analyzer.py の後半)
#
# 追従 (tracker.py) だけが使う。ver5 resolve8 で人物の全体解析を廃止したため、
# 「対象区間の決定・検出・トラッキング」は無くなり、**デコードだけが残った**。
#
# デコードは PyAV の前進デコード。使えない環境では FFmpeg へ rawvideo をパイプする
# (frame_source が既に同じ二段構えになっている)。
import subprocess

import numpy as np

from ..modules import ffmpeg_runner
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags

_logger = get_logger(__name__)

# PyAV は任意依存として扱う (未導入・import 失敗時は FFmpeg 方式へ倒す)
try:
    import av
    _PYAV_AVAILABLE = True
except Exception:  # noqa: BLE001
    _PYAV_AVAILABLE = False

# 後ろ向きに読むとき、一度にデコードする長さ (秒)。フレームを溜めるためメモリを抑える
_BACKWARD_CHUNK_SEC = 2.0


# 区間を sample_fps の間隔で 1 枚ずつ返す。RGB の ndarray を (時刻, 画像) で返す。
def iter_frames(media, start, end, sample_fps, settings):
    if _PYAV_AVAILABLE:
        try:
            yield from _iter_frames_pyav(media, start, end, sample_fps)
            return
        except Exception as error:              # noqa: BLE001 (壊れた素材でも追従を続ける)
            _logger.warning("PyAV でのデコードに失敗したため FFmpeg へ切り替えます: %s", error)
    yield from _iter_frames_ffmpeg(media, start, end, sample_fps, settings)


# 基準の時刻より後ろを前向きに返す (基準そのものは含めない)
def forward(media, anchor_sec, end, sample_fps, settings):
    interval = 1.0 / max(float(sample_fps), 0.01)
    if end - anchor_sec < interval * 0.5:
        return
    for sec, image in iter_frames(media, anchor_sec + interval * 0.5, end, sample_fps, settings):
        if sec > anchor_sec + 1e-3:
            yield sec, image


# 基準の時刻より前を、基準に近い順に返す。短い区間ずつデコードして逆順に並べる。
def backward(media, start, anchor_sec, sample_fps, settings, cancel=None):
    chunk_end = anchor_sec
    while chunk_end - start > 1e-3:
        if cancel is not None and cancel():
            return
        chunk_start = max(start, chunk_end - _BACKWARD_CHUNK_SEC)
        chunk = [(sec, image) for sec, image in iter_frames(
            media, chunk_start, chunk_end, sample_fps, settings)
            if chunk_start - 1e-3 <= sec < chunk_end - 1e-3]
        yield from reversed(chunk)
        chunk_end = chunk_start


# 指定時刻の 1 枚を取り出す (追従の基準にする絵)。取れなければ None。
def first_frame(media, sec, settings):
    for _t, image in iter_frames(media, max(sec - 0.01, 0.0), sec + 0.5, 4.0, settings):
        return image
    return None


# PyAV で区間をデコードする (frame_source と同じ手口。seek してから前進デコード)
def _iter_frames_pyav(media, start, end, sample_fps):
    interval = 1.0 / max(float(sample_fps), 0.01)
    container = av.open(media.path)
    try:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        # 目的時刻の手前のキーフレームまで戻ってから前進デコードする
        if stream.time_base:
            offset = int(max(start, 0.0) / float(stream.time_base))
            container.seek(offset, stream=stream, any_frame=False, backward=True)

        next_sec = start
        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            sec = float(frame.pts * stream.time_base)
            if sec < start - interval:
                continue
            if sec > end:
                break
            if sec + 1e-6 < next_sec:
                continue
            yield sec, frame.to_ndarray(format="rgb24")
            next_sec = max(sec, next_sec) + interval
    finally:
        container.close()


# FFmpeg へ rawvideo をパイプさせて 1 枚ずつ読む (PyAV が無い環境)
def _iter_frames_ffmpeg(media, start, end, sample_fps, settings):
    ffmpeg_cfg = (settings or {}).get("ffmpeg", {})
    width = int(getattr(media, "width", 0) or 0)
    height = int(getattr(media, "height", 0) or 0)
    if width <= 0 or height <= 0:
        _logger.warning("素材の寸法が分からないためデコードできません: %s", media.path)
        return

    interval = 1.0 / max(float(sample_fps), 0.01)
    command = [
        ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg), "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(start, 0.0):.3f}", "-i", media.path,
        "-t", f"{max(end - start, 0.0):.3f}",
        "-vf", f"fps={sample_fps}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    frame_bytes = width * height * 3
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=no_window_creationflags())
    try:
        index = 0
        while True:
            raw = process.stdout.read(frame_bytes)
            if not raw or len(raw) < frame_bytes:
                break
            image = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
            yield start + index * interval, image
            index += 1
    finally:
        if process.stdout is not None:
            process.stdout.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
