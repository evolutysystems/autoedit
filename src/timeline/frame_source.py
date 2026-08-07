# プレビュー用フレーム取得 (docs/request/ver3/resolve.md §6.4-1)
# 再生ヘッドの移動へリアルタイムに追従させるため、既定では PyAV で
# プロセス内デコードする (ffmpeg のプロセス起動を挟まないので数〜数十 ms)。
# PyAV が使えない環境では ffmpeg で 1 フレーム抽出する方式へ自動フォールバックする。
#
# 返すフレームは (width, height, rgb24 のバイト列) の組。Qt 側で QImage 化する。
import os
import subprocess

from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags

_logger = get_logger(__name__)

# PyAV は任意依存として扱う (未導入・import 失敗時は ffmpeg 方式へ倒す)
try:
    import av
    _PYAV_AVAILABLE = True
except Exception:  # noqa: BLE001
    _PYAV_AVAILABLE = False

# シーク後にどこまで進めて目的フレームを探すかの許容 (秒)
_SEEK_TOLERANCE_SEC = 1.0
# 1 フレーム取得のためにデコードを進める上限 (無限ループ防止)
_MAX_DECODE_FRAMES = 240


# フレーム取得の共通インタフェース
class FrameSource:

    # 指定メディアの指定ソース時刻のフレームを (width, height, rgb_bytes) で返す
    # 取得できない場合は None を返す (画面側で「表示できません」を出す)
    def frame_at(self, media, source_sec):
        raise NotImplementedError

    # 使用中のリソースを解放する (画面クローズ時に必ず呼ぶ)
    def close(self):
        pass


# PyAV によるプロセス内デコード (既定)
class PyAvFrameSource(FrameSource):

    def __init__(self, cache_frames=8):
        # メディアごとに container を開いたまま保持する (シークのたびに開き直さない)
        self._containers = {}
        # 直近の取得結果を LRU で持つ (スクラブで同じ位置へ戻ったときに効く)
        self._cache = {}
        self._cache_order = []
        self._cache_limit = max(int(cache_frames), 1)

    # media_id ごとに (container, stream) を用意する
    def _open(self, media):
        entry = self._containers.get(media.id)
        if entry is not None:
            return entry
        try:
            container = av.open(media.path)
            stream = container.streams.video[0]
            # スレッドデコードでスクラブの応答を上げる
            stream.thread_type = "AUTO"
        except Exception as e:  # noqa: BLE001 (壊れた素材でも画面を落とさない)
            _logger.warning("素材を開けませんでした: %s (%s)", media.path, e)
            self._containers[media.id] = (None, None)
            return (None, None)
        entry = (container, stream)
        self._containers[media.id] = entry
        return entry

    def frame_at(self, media, source_sec):
        if media is None or not media.path:
            return None
        key = (media.id, round(float(source_sec), 3))
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        container, stream = self._open(media)
        if container is None:
            return None

        frame = self._decode_at(container, stream, float(source_sec))
        if frame is None:
            return None
        try:
            array = frame.to_ndarray(format="rgb24")
        except Exception as e:  # noqa: BLE001
            _logger.warning("フレームの変換に失敗しました: %s", e)
            return None
        result = (array.shape[1], array.shape[0], array.tobytes())
        self._store(key, result)
        return result

    # 目的時刻へシークして最も近いフレームまでデコードを進める
    def _decode_at(self, container, stream, source_sec):
        target = max(source_sec, 0.0)
        try:
            if stream.time_base:
                offset = int(target / float(stream.time_base))
            else:
                offset = int(target * 1000)
            # backward=True で目的時刻以前のキーフレームへ飛ぶ
            container.seek(offset, stream=stream, backward=True, any_frame=False)
        except Exception as e:  # noqa: BLE001 (静止画などシーク不可な素材がある)
            _logger.debug("シークできないため先頭から読みます: %s", e)
            try:
                container.seek(0)
            except Exception:  # noqa: BLE001
                pass

        last = None
        try:
            for index, frame in enumerate(container.decode(stream)):
                last = frame
                if frame.pts is None:
                    # タイムスタンプが無い素材 (静止画等) は最初のフレームを使う
                    return frame
                position = float(frame.pts * stream.time_base)
                if position >= target - 1.0 / max(float(stream.average_rate or 30), 1.0):
                    return frame
                if position > target + _SEEK_TOLERANCE_SEC:
                    return frame
                if index >= _MAX_DECODE_FRAMES:
                    return frame
        except Exception as e:  # noqa: BLE001 (末尾到達・破損フレーム)
            _logger.debug("デコード終了: %s", e)
        return last

    def _store(self, key, value):
        self._cache[key] = value
        self._cache_order.append(key)
        while len(self._cache_order) > self._cache_limit:
            oldest = self._cache_order.pop(0)
            self._cache.pop(oldest, None)

    def close(self):
        for container, _stream in self._containers.values():
            if container is not None:
                try:
                    container.close()
                except Exception:  # noqa: BLE001
                    pass
        self._containers.clear()
        self._cache.clear()
        self._cache_order.clear()


# ffmpeg で 1 フレームだけ取り出すフォールバック
# プロセス起動を挟むため応答は劣るが、PyAV が無い環境でもプレビューを成立させる。
class FfmpegFrameSource(FrameSource):

    def __init__(self, settings, cache_frames=8):
        from ..modules import ffmpeg_runner
        self._ffmpeg_cfg = (settings or {}).get("ffmpeg", {})
        self._ffmpeg = ffmpeg_runner.get_ffmpeg_exe(self._ffmpeg_cfg)
        self._cache = {}
        self._cache_order = []
        self._cache_limit = max(int(cache_frames), 1)

    def frame_at(self, media, source_sec):
        if media is None or not media.path or not os.path.exists(media.path):
            return None
        width = int(media.width or 0)
        height = int(media.height or 0)
        if width <= 0 or height <= 0:
            return None

        key = (media.id, round(float(source_sec), 3))
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        cmd = [
            self._ffmpeg, "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(float(source_sec), 0.0):.3f}",
            "-i", media.path,
            "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, creationflags=no_window_creationflags())
        except OSError as e:
            _logger.warning("フレーム抽出に失敗しました: %s", e)
            return None
        expected = width * height * 3
        if result.returncode != 0 or len(result.stdout) < expected:
            return None
        value = (width, height, result.stdout[:expected])
        self._cache[key] = value
        self._cache_order.append(key)
        while len(self._cache_order) > self._cache_limit:
            self._cache.pop(self._cache_order.pop(0), None)
        return value

    def close(self):
        self._cache.clear()
        self._cache_order.clear()


# 設定に応じたフレーム取得実装を返す (§6.4-1)
# backend="ffmpeg" 指定、または PyAV が使えない場合はフォールバックを使う。
def create_frame_source(settings, preview_cfg=None):
    cfg = preview_cfg or ((settings or {}).get("timeline", {}) or {}).get("preview", {}) or {}
    backend = str(cfg.get("backend", "pyav") or "pyav").strip().lower()
    cache_frames = int(cfg.get("cache_frames", 8))

    if backend == "pyav" and _PYAV_AVAILABLE:
        return PyAvFrameSource(cache_frames=cache_frames)
    if backend == "pyav" and not _PYAV_AVAILABLE:
        _logger.info("PyAV が利用できないため ffmpeg フレーム抽出で動作します")
    return FfmpegFrameSource(settings, cache_frames=cache_frames)


# PyAV が使えるか (画面側の案内表示用)
def is_pyav_available():
    return _PYAV_AVAILABLE
