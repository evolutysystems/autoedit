# 音声クリップの波形 (音量) データ
# Timeline の音声ノードへ DaVinci Resolve と同じ見え方の波形を敷くための土台。
#
# 方針:
#   ・波形は「素材ごと」に 1 回だけ ffmpeg でデコードし、一定間隔のピーク (と RMS) に
#     畳んで持つ。クリップは素材内の時刻 (source_in) で引くため、移動・トリム・分割・
#     リップル削除では作り直しが要らない。
#   ・長尺 (数時間) でも編集を待たせないよう、取得はワーカースレッドで行う。
#     途中結果を随時受け取れるので、先頭から順に波形が現れる。
#   ・失敗しても編集は止めない (§10)。波形が出ないだけで従来どおり操作できる。
import math
import os
import subprocess
from array import array

from PySide6.QtCore import QObject, QThread, Signal

from ...modules import ffmpeg_runner
from ...utils.logger import get_logger
from ...utils.proc import no_window_creationflags

_logger = get_logger(__name__)

# numpy があれば桁違いに速いので使う。無い環境ではピークのみの簡易計算へ落とす
# (RMS は純 Python では重すぎるため出さない)。
try:
    import numpy as _np
except ImportError:  # pragma: no cover (実行環境依存)
    _np = None

# 1 回の読み出しサイズ。小さいほど波形が早く現れ、その分やり取りが増える。
# 8kHz モノラル 16bit なら 256KB ≒ 16 秒ぶん。
_READ_BYTES = 256 * 1024

# 同時に走らせるデコード数。素材が多くても CPU をプレビューから奪い過ぎないようにする。
_MAX_WORKERS = 2

# これ以上の列数なら numpy でまとめて畳む (下回るときは素の loop の方が速い)
_VECTOR_MIN_COLUMNS = 48

# 完成した波形を素材ごとに保持する (画面を開き直しても再デコードしない)。
# キーに更新時刻とサイズを含めるため、素材が差し替わったら作り直される。
_COMPLETED = {}
_COMPLETED_LIMIT = 4


# 素材を識別するキー (パス + 更新時刻 + サイズ)
def _media_key(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (os.path.normcase(os.path.abspath(path)), int(stat.st_mtime), stat.st_size)


# ==================================================================
# 波形データ
# ==================================================================

# 素材 1 つぶんの波形。resolution_hz 個/秒のバケツごとに
# ピーク (絶対値の最大) と RMS (実効値) を 0.0〜1.0 で持つ。
class Peaks:

    def __init__(self, resolution_hz):
        self.resolution_hz = float(resolution_hz)
        self.complete = False
        self._blocks = []        # 到着順の (peak, rms)
        self._peak = None        # 連結済み (参照されたときに作る)
        self._rms = None

    # 1 バケツでも取れているか
    def has_data(self):
        peak, _rms = self._merged()
        return peak is not None and len(peak) > 0

    # ワーカーから届いた区間を末尾へ足す
    def append(self, peak, rms):
        self._blocks.append((peak, rms))
        self._peak = None
        self._rms = None

    # 溜まった区間を 1 本に連結する (連結結果は次の append まで使い回す)
    def _merged(self):
        if self._peak is None:
            if not self._blocks:
                return None, None
            if _np is not None:
                self._peak = _np.concatenate([b[0] for b in self._blocks])
                rms_blocks = [b[1] for b in self._blocks]
                self._rms = (_np.concatenate(rms_blocks)
                             if all(r is not None for r in rms_blocks) else None)
            else:
                merged = []
                for block in self._blocks:
                    merged.extend(block[0])
                self._peak = merged
                self._rms = None
            self._blocks = [(self._peak, self._rms)]
        return self._peak, self._rms

    # 素材内の [start_sec, end_sec) を count 列へ畳んで返す。
    # 戻り値: (ピーク列, RMS 列 or None)。まだ取得できていない範囲は列を短くして返す
    # (描ける範囲だけ描き、残りは取得が進んだ時点で描き足す)。
    # with_rms=False なら RMS を計算しない (細いクリップでは見えないため)。
    def columns(self, start_sec, end_sec, count, with_rms=True):
        peak, rms = self._merged()
        if peak is None or count <= 0 or end_sec <= start_sec:
            return None
        total = len(peak)
        if total <= 0:
            return None
        span = (end_sec - start_sec) * self.resolution_hz
        first = start_sec * self.resolution_hz
        if first >= total:
            return None
        # 取得済みの末尾を越える列は落とす
        usable = count
        if first + span > total:
            usable = int((total - first) * count / span)
            if usable <= 0:
                return None
        step = span / count
        end_index = min(int(math.ceil(first + step * usable)), total)
        if end_index <= 0:
            return None

        if rms is not None and not with_rms:
            rms = None

        # 列数が多いときだけベクトル化する。数列しかない細いクリップでは
        # numpy を呼ぶ費用の方が高くつく (無音カット後は細いクリップが大量に並ぶ)。
        if _np is not None and usable >= _VECTOR_MIN_COLUMNS:
            starts = _np.floor(
                first + step * _np.arange(usable, dtype=_np.float64)).astype(_np.int64)
            _np.clip(starts, 0, end_index - 1, out=starts)
            # reduceat は [starts[i], starts[i+1]) を 1 列にする。
            # 拡大して 1 バケツ未満になった列は starts[i] の値がそのまま入る。
            peak_cols = _np.maximum.reduceat(peak[:end_index], starts)
            rms_cols = (_np.maximum.reduceat(rms[:end_index], starts)
                        if rms is not None else None)
            return peak_cols, rms_cols

        peak_cols = []
        rms_cols = [] if rms is not None else None
        for index in range(usable):
            lo = min(int(first + step * index), end_index - 1)
            hi = min(max(int(first + step * (index + 1)), lo + 1), end_index)
            peak_cols.append(_slice_max(peak, lo, hi))
            if rms_cols is not None:
                rms_cols.append(_slice_max(rms, lo, hi))
        return peak_cols, rms_cols


# 区間の最大値 (numpy 配列・list のどちらでも引ける)
def _slice_max(values, lo, hi):
    segment = values[lo:hi]
    return segment.max() if hasattr(segment, "max") else max(segment)


# ==================================================================
# 取得ワーカー
# ==================================================================

# 素材の音声をモノラル・低レートの生 PCM へデコードし、バケツごとのピークへ畳む。
# 映像はデコードしない (-vn) ため、長尺でも実時間の数十倍の速さで進む。
class _ExtractWorker(QThread):

    block_ready = Signal(object, object, object)   # (key, peak, rms)
    completed = Signal(object, bool)               # (key, 成否)

    def __init__(self, key, path, ffmpeg_cfg, sample_rate, resolution_hz, parent=None):
        super().__init__(parent)
        self._key = key
        self._path = path
        self._ffmpeg_cfg = ffmpeg_cfg or {}
        self._sample_rate = int(sample_rate)
        self._resolution_hz = float(resolution_hz)
        self._stopping = False
        self._process = None

    # 画面を閉じるときなどに打ち切る
    def stop(self):
        self._stopping = True
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def run(self):
        samples_per_bucket = max(int(round(self._sample_rate / self._resolution_hz)), 1)
        block_bytes = samples_per_bucket * 2      # s16le = 2 バイト/サンプル
        cmd = [
            ffmpeg_runner.get_ffmpeg_exe(self._ffmpeg_cfg),
            "-hide_banner", "-loglevel", "error",
            "-i", self._path,
            "-vn", "-map", "0:a:0",
            "-ac", "1", "-ar", str(self._sample_rate),
            "-f", "s16le", "-",
        ]
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=no_window_creationflags(),
            )
        except (OSError, FileNotFoundError) as e:
            _logger.warning("波形の取得を開始できませんでした: %s (%s)", self._path, e)
            self.completed.emit(self._key, False)
            return

        ok = True
        remainder = b""
        try:
            while not self._stopping:
                data = self._process.stdout.read(_READ_BYTES)
                if not data:
                    break
                buffer = remainder + data if remainder else data
                usable = (len(buffer) // block_bytes) * block_bytes
                if usable <= 0:
                    remainder = buffer
                    continue
                remainder = buffer[usable:]
                peak, rms = _reduce(buffer[:usable], samples_per_bucket)
                if peak is not None:
                    self.block_ready.emit(self._key, peak, rms)
        except (OSError, ValueError):
            _logger.exception("波形の取得に失敗しました: %s", self._path)
            ok = False
        finally:
            try:
                self._process.stdout.close()
            except OSError:
                pass
            if self._stopping:
                ok = False
            elif self._process.wait() != 0:
                # 音声を持たない素材でもここへ来る。編集は止めないので警告に留める。
                _logger.info("波形を取得できませんでした: %s", os.path.basename(self._path))
                ok = False
        self.completed.emit(self._key, ok)


# 生 PCM (s16le モノラル) をバケツごとのピーク・RMS へ畳む
def _reduce(raw, samples_per_bucket):
    count = len(raw) // (samples_per_bucket * 2)
    if count <= 0:
        return None, None
    if _np is not None:
        samples = _np.frombuffer(raw, dtype="<i2")[:count * samples_per_bucket]
        block = samples.reshape(count, samples_per_bucket).astype(_np.float32) / 32768.0
        peak = _np.abs(block).max(axis=1)
        rms = _np.sqrt(_np.square(block).mean(axis=1))
        return peak, rms

    # numpy が無い環境向け。RMS は純 Python では重すぎるため出さない。
    samples = array("h")
    samples.frombytes(raw[:count * samples_per_bucket * 2])
    peak = []
    for index in range(count):
        segment = samples[index * samples_per_bucket:(index + 1) * samples_per_bucket]
        peak.append(max(max(segment), -min(segment)) / 32768.0)
    return peak, None


# ==================================================================
# キャッシュ (画面から使う窓口)
# ==================================================================

class WaveformCache(QObject):

    # 波形が伸びた (再描画してほしい)
    changed = Signal()

    def __init__(self, settings, cfg, parent=None):
        super().__init__(parent)
        wave_cfg = cfg["waveform"]
        self._enabled = bool(wave_cfg["enabled"])
        self._sample_rate = int(wave_cfg["sample_rate"])
        self._resolution_hz = int(wave_cfg["resolution_hz"])
        self._ffmpeg_cfg = (settings or {}).get("ffmpeg", {})
        self._peaks = {}       # key -> Peaks (取得中を含む)
        self._workers = {}     # key -> _ExtractWorker
        self._pending = []     # 同時実行数を越えた分の (key, path)
        self._failed = set()
        # パス -> キー。キーの算出には os.stat が要り、これは描画のたびに
        # クリップの数だけ呼ばれるため、画面を開いている間は引き当てで済ませる。
        self._keys = {}

    # 素材の波形を返す (未取得なら取得を始めて、その時点までの分を返す)
    def peaks_for(self, media):
        if not self._enabled or media is None:
            return None
        if media.is_image() or not media.has_audio:
            return None
        key = self._key_for(media.path)
        if key is None or key in self._failed:
            return None
        done = _COMPLETED.get(key)
        if done is not None:
            return done
        peaks = self._peaks.get(key)
        if peaks is None:
            peaks = Peaks(self._resolution_hz)
            self._peaks[key] = peaks
            self._start(key, media.path)
        return peaks

    # 走っているデコードをすべて止める (画面を閉じるとき)
    def shutdown(self):
        self._pending = []
        for worker in list(self._workers.values()):
            worker.stop()
        for worker in list(self._workers.values()):
            worker.wait(2000)
        self._workers = {}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    # 素材のキーを引く (見つからない素材は None を覚えて再取得しない)
    def _key_for(self, path):
        if path in self._keys:
            return self._keys[path]
        key = _media_key(path)
        self._keys[path] = key
        return key

    def _start(self, key, path):
        if len(self._workers) >= _MAX_WORKERS:
            self._pending.append((key, path))
            return
        worker = _ExtractWorker(
            key, path, self._ffmpeg_cfg, self._sample_rate, self._resolution_hz,
            parent=self)
        worker.block_ready.connect(self._on_block)
        worker.completed.connect(self._on_completed)
        self._workers[key] = worker
        worker.start()

    def _on_block(self, key, peak, rms):
        peaks = self._peaks.get(key)
        if peaks is None:
            return
        peaks.append(peak, rms)
        self.changed.emit()

    def _on_completed(self, key, ok):
        worker = self._workers.pop(key, None)
        if worker is not None:
            worker.deleteLater()
        peaks = self._peaks.get(key)
        if ok and peaks is not None:
            peaks.complete = True
            # 完成したものだけ共有キャッシュへ載せる (以後は即座に描ける)
            _remember(key, peaks)
        elif peaks is not None and not peaks.has_data():
            # 音声を取り出せなかった素材。毎回デコードを試みないよう覚えておく
            # (途中まで取れていれば、その分は描けるので捨てない)
            self._peaks.pop(key, None)
            self._failed.add(key)
        while self._pending and len(self._workers) < _MAX_WORKERS:
            next_key, next_path = self._pending.pop(0)
            self._start(next_key, next_path)
        self.changed.emit()


def _remember(key, peaks):
    _COMPLETED[key] = peaks
    # 素材 1 本 1 時間ぶんで数 MB。取り替えながら長く使っても増え続けないよう、
    # 古いものから捨てる (捨てても次に開いたときに取り直せるだけ)。
    while len(_COMPLETED) > _COMPLETED_LIMIT:
        _COMPLETED.pop(next(iter(_COMPLETED)))
