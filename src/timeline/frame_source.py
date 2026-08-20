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

# 設定値 "nonref"/"bidir" → PyAV (FFmpeg) の skip_frame 値
# 再生中だけ「画質を落として速くする」ための指定 (ver3 resolve6 §3-6 R7)
_SKIP_FRAME_VALUES = {"nonref": "NONREF", "bidir": "BIDIR"}


# フレーム取得の共通インタフェース
class FrameSource:

    # 指定メディアの指定ソース時刻のフレームを (width, height, rgb_bytes) で返す
    # 取得できない場合は None を返す (画面側で「表示できません」を出す)
    def frame_at(self, media, source_sec):
        raise NotImplementedError

    # 再生の開始・終了を伝える (再生中だけ画質を落とす実装のためのフック)
    # 既定は何もしない = 常に原寸・全フレーム。
    def set_playing(self, playing):
        pass

    # 使用中のリソースを解放する (画面クローズ時に必ず呼ぶ)
    def close(self):
        pass


# PyAV によるプロセス内デコード (既定)
#
# 連続再生では「1 フレームごとに seek し直す」のをやめ、直前に読んだ位置から
# 読み進める (ver3 resolve6 §3-7)。seek は目的時刻の前のキーフレームまで戻って
# 読み直すため、実測で 1 枚 134ms かかっていた。前進デコードなら 9〜11ms で済む。
class PyAvFrameSource(FrameSource):

    # sequential_decode_sec : この秒数以内の前進なら seek せず読み進める (0 = 常に seek)
    # playback_width        : 再生中この幅へ縮小して返す (0 = 原寸のまま)
    # playback_skip_frame   : 再生中のフレーム間引き ("none"/"nonref"/"bidir")
    def __init__(self, cache_frames=8, sequential_decode_sec=2.0,
                 playback_width=0, playback_skip_frame="none"):
        # メディアごとに container を開いたまま保持する (シークのたびに開き直さない)
        self._containers = {}
        # メディアごとのデコード継続用ジェネレータと、直前に読んだ位置 (秒)
        self._decoders = {}
        self._positions = {}
        # 直近の取得結果を LRU で持つ (スクラブで同じ位置へ戻ったときに効く)
        self._cache = {}
        self._cache_order = []
        self._cache_limit = max(int(cache_frames), 1)
        self._sequential_sec = max(float(sequential_decode_sec or 0.0), 0.0)
        self._playback_width = max(int(playback_width or 0), 0)
        self._skip_frame = str(playback_skip_frame or "none").strip().lower()
        self._playing = False

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

    # ------------------------------------------------------------------
    # 再生状態 (再生中だけ画質を落とす / resolve6 §3-6)
    # ------------------------------------------------------------------

    def set_playing(self, playing):
        playing = bool(playing)
        if playing == self._playing:
            return
        self._playing = playing
        value = _SKIP_FRAME_VALUES.get(self._skip_frame) if playing else None
        for _media_id, (container, stream) in self._containers.items():
            if container is None:
                continue
            try:
                stream.codec_context.skip_frame = value or "DEFAULT"
            except Exception as e:  # noqa: BLE001 (未対応のコーデック/ビルドがある)
                _logger.debug("skip_frame を設定できません: %s", e)
        # 間引き・縮小の切り替えはデコーダの状態を変えるため、
        # 保持しているデコーダを捨てて次の要求で読み直させる。
        self._decoders.clear()
        self._positions.clear()

    # 再生中に縮小して返すか (縮小するなら幅を返す)
    def _scaled_width(self, media):
        if not self._playing or self._playback_width <= 0:
            return 0
        width = int(getattr(media, "width", 0) or 0)
        # 拡大はしない (小さい素材を無駄にぼかさない)
        if width and width <= self._playback_width:
            return 0
        return self._playback_width

    # ------------------------------------------------------------------
    # 取得
    # ------------------------------------------------------------------

    def frame_at(self, media, source_sec):
        if media is None or not media.path:
            return None
        scaled_width = self._scaled_width(media)
        # 縮小の有無をキーへ含める。含めないと再生中に作った粗いフレームが
        # 停止後の原寸要求へ返り、止めても粗いままになる (resolve6 §5.7)。
        key = (media.id, round(float(source_sec), 3), scaled_width)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        container, stream = self._open(media)
        if container is None:
            return None

        frame = self._decode_at(media.id, container, stream, float(source_sec))
        if frame is None:
            return None
        result = self._to_rgb(frame, scaled_width)
        if result is None:
            return None
        self._store(key, result)
        return result

    # フレームを RGB のバイト列へ直す (必要なら縮小する)
    def _to_rgb(self, frame, scaled_width):
        try:
            if scaled_width > 0:
                height = int(frame.height * scaled_width / max(frame.width, 1))
                # 偶数へ丸める (奇数高さを嫌う変換器があるため)
                height = max(height - (height % 2), 2)
                frame = frame.reformat(width=scaled_width, height=height,
                                       format="rgb24")
                array = frame.to_ndarray()
            else:
                array = frame.to_ndarray(format="rgb24")
        except Exception as e:  # noqa: BLE001
            _logger.warning("フレームの変換に失敗しました: %s", e)
            return None
        return (array.shape[1], array.shape[0], array.tobytes())

    # 目的時刻のフレームを返す
    #   ・同じ素材の少し先 → seek せずに読み進める (連続再生 / resolve6 §3-7)
    #   ・それ以外          → 従来どおり seek してから読む
    def _decode_at(self, media_id, container, stream, source_sec):
        target = max(source_sec, 0.0)
        position = self._positions.get(media_id)
        if (self._sequential_sec > 0 and position is not None
                and self._decoders.get(media_id) is not None
                and 0.0 <= target - position <= self._sequential_sec):
            frame = self._read_forward(media_id, stream, target)
            if frame is not None:
                return frame
            # 読み進めても届かなかった (末尾到達・欠落) → seek でやり直す
            _logger.debug("前進デコードで届かなかったため seek し直します: %.3fs", target)

        self._seek(container, stream, target)
        self._decoders[media_id] = container.decode(stream)
        return self._read_forward(media_id, stream, target)

    def _seek(self, container, stream, target):
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

    # 保持しているデコーダから目的時刻まで読み進める (届かなければ None)
    def _read_forward(self, media_id, stream, target):
        decoder = self._decoders.get(media_id)
        if decoder is None:
            return None
        frame_rate = max(float(stream.average_rate or 30), 1.0)
        last = None
        try:
            for index, frame in enumerate(decoder):
                last = frame
                if frame.pts is None:
                    # タイムスタンプが無い素材 (静止画等) は最初のフレームを使う
                    self._positions[media_id] = target
                    return frame
                position = float(frame.pts * stream.time_base)
                self._positions[media_id] = position
                if position >= target - 1.0 / frame_rate:
                    return frame
                if position > target + _SEEK_TOLERANCE_SEC:
                    return frame
                if index >= _MAX_DECODE_FRAMES:
                    return frame
        except Exception as e:  # noqa: BLE001 (末尾到達・破損フレーム)
            _logger.debug("デコード終了: %s", e)
            self._drop_decoder(media_id)
            return last
        # ストリームが尽きた (末尾) → 最後のフレームを返す。次回は seek し直す。
        self._drop_decoder(media_id)
        return last

    def _drop_decoder(self, media_id):
        self._decoders.pop(media_id, None)
        self._positions.pop(media_id, None)

    def _store(self, key, value):
        self._cache[key] = value
        self._cache_order.append(key)
        while len(self._cache_order) > self._cache_limit:
            oldest = self._cache_order.pop(0)
            self._cache.pop(oldest, None)

    def close(self):
        self._decoders.clear()
        self._positions.clear()
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
        return PyAvFrameSource(
            cache_frames=cache_frames,
            # 再生の応答改善 (ver3 resolve6 §3-6 / §3-7)
            sequential_decode_sec=float(cfg.get("sequential_decode_sec", 2.0) or 0.0),
            playback_width=int(cfg.get("playback_width", 0) or 0),
            playback_skip_frame=str(cfg.get("playback_skip_frame", "none") or "none"),
        )
    if backend == "pyav" and not _PYAV_AVAILABLE:
        _logger.info("PyAV が利用できないため ffmpeg フレーム抽出で動作します")
    return FfmpegFrameSource(settings, cache_frames=cache_frames)


# PyAV が使えるか (画面側の案内表示用)
def is_pyav_available():
    return _PYAV_AVAILABLE
