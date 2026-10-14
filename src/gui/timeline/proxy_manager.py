# プロキシのキャッシュとワーカー管理 (ver6 resolve3 §3.5 / §4.3)
#
# GUI 寄りの都合 (優先順・シグナル・キャンセル・後片付け) はここへ閉じる。
# 絵の作り方は proxy_render → compositor が持ち、ここは「どのタイルをいつ作るか」
# と「どれが使えるか」だけを決める。
#
# 【スレッドの約束】
#   ・Timeline を読むのは**GUI スレッドだけ**。ワーカーへ渡すのは組み立て済みの
#     ffmpeg コマンドだけにする (proxy_render.plan_tile の注記)。生成中に利用者が
#     編集しても、別スレッドから中途半端な Timeline を読む余地が無い。
#   ・ワーカーは 1 要求ずつキューから取る。作ったタイルは捨てずに残るため、
#     「最新の要求だけを処理する」_FrameWorker 方式ではなくキュー方式にする。
import os

from PySide6.QtCore import QMutex, QMutexLocker, QObject, QThread, QWaitCondition, Signal

from ...i18n import tr
from ...modules import ffmpeg_runner
from ...settings.settings_window import resolve_fonts_dir
from ...timeline import compositor, proxy_render
from ...utils.logger import get_logger

_logger = get_logger(__name__)

# 生成中のファイルに付ける拡張子。完成したら外して公開する。
# 「その名前のファイルが在る = 完成している」を保つための仕掛けで、
# 打ち切りや異常終了で残った中途半端なタイルを拾わない。
_PART_SUFFIX = ".part"

# 同じタイルがこの回数続けて失敗したら、以降このセッションではプロキシを作らない (§7)
_MAX_TILE_FAILURES = 3
# 1 回の優先順計算で積む上限 (巨大プロジェクトでキューが膨らむのを防ぐ)
_MAX_QUEUE = 64


# タイル生成ワーカー (常駐。キューから 1 件ずつ取り出して ffmpeg を実行する)
class _TileWorker(QThread):

    # (tile_index, key, path, 成功したか, 理由)
    finished_tile = Signal(int, str, str, bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mutex = QMutex()
        self._condition = QWaitCondition()
        self._jobs = []
        self._current_key = ""
        self._cancelled = set()
        self._stopping = False

    # 生成を要求する (同じ鍵が既に積まれていれば足さない)
    def submit(self, tile_index, key, path, command):
        with QMutexLocker(self._mutex):
            if key == self._current_key:
                return
            if any(job[1] == key for job in self._jobs):
                return
            self._jobs.append((tile_index, key, path, command))
            self._cancelled.discard(key)
            self._condition.wakeAll()

    # 積んである要求をすべて捨てる。生成中のものは鍵を指定して打ち切る。
    def clear(self, cancel_keys=None):
        with QMutexLocker(self._mutex):
            self._jobs = []
            for key in (cancel_keys or []):
                self._cancelled.add(key)
            self._condition.wakeAll()

    # 生成中の鍵 (無ければ空文字)
    def current_key(self):
        with QMutexLocker(self._mutex):
            return self._current_key

    def queued(self):
        with QMutexLocker(self._mutex):
            return len(self._jobs)

    def stop(self):
        with QMutexLocker(self._mutex):
            self._stopping = True
            self._jobs = []
            self._condition.wakeAll()

    def _is_cancelled(self, key):
        with QMutexLocker(self._mutex):
            return self._stopping or key in self._cancelled

    def run(self):
        while True:
            with QMutexLocker(self._mutex):
                while not self._jobs and not self._stopping:
                    self._condition.wait(self._mutex)
                if self._stopping:
                    return
                tile_index, key, path, command = self._jobs.pop(0)
                if key in self._cancelled:
                    self._cancelled.discard(key)
                    continue
                self._current_key = key
            try:
                ok, reason = proxy_render.run_tile(
                    command, lambda: self._is_cancelled(key))
            except Exception:  # noqa: BLE001 (生成の失敗で編集画面を落とさない)
                _logger.exception("プロキシのタイル生成で例外が発生しました")
                ok, reason = False, "failed"
            with QMutexLocker(self._mutex):
                self._current_key = ""
                self._cancelled.discard(key)
            self.finished_tile.emit(tile_index, key, path, ok, reason)


class ProxyManager(QObject):

    # タイルが 1 枚できた (tile_index)
    tile_ready = Signal(int)
    # 進捗表示の更新要求 (レンダ済みバーの塗り直し)
    coverage_changed = Signal()
    # プロキシを諦めた (理由の文面。状態表示へ 1 回だけ出す)
    gave_up = Signal(str)

    def __init__(self, controller, work_dir, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._settings = controller.settings
        self._cfg = dict(controller.cfg["preview"].get("proxy") or {})
        self._work_dir = os.path.join(work_dir, "proxy")
        self._enabled = str(self._cfg.get("mode", "tile")) == "tile"
        self._profile = compositor.CanvasProfile.proxy(
            controller.timeline, self._settings.get("ffmpeg", {}),
            self._cfg.get("scale", 0.5))

        # tile_index -> {"key": 鍵, "path": パス}
        self._tiles = {}
        # 組み立て済み / 生成中のタイル (tile_index -> 鍵)
        self._planned = {}
        # 生成に失敗した回数 (tile_index -> 回数)
        self._failures = {}
        self._focus = 0.0
        # 最後に積み直したときの焦点タイル。再生中は再生ヘッドが毎フレーム動くため、
        # タイルを跨いだときだけ積み直す (鍵の計算を毎フレーム走らせない)。
        self._focus_index = -1
        self._watermark_required = False
        self._blur_mask_path = None
        self._blur_signature = ""
        # ProxyEnvironment の作り置き。実効字幕設定と FontProfile の組み立ては
        # 安くないため、鍵の計算のたびに作り直さない (入力が変わったら捨てる)。
        self._env = None
        self._gave_up = False
        # 画面を閉じたあと。ワーカーを止めたあとに届く完了通知を無視するための印。
        # 印が無いと、掃除した直後に公開されたタイルが残ってしまう。
        self._closed = False

        self._worker = None
        if self._enabled:
            try:
                os.makedirs(self._work_dir, exist_ok=True)
            except OSError:
                _logger.warning("プロキシの置き場を作れないため近似プレビューで動かします")
                self._enabled = False
        if self._enabled:
            self._worker = _TileWorker(parent=self)
            self._worker.finished_tile.connect(self._on_tile_finished)
            self._worker.start()

    # ------------------------------------------------------------------
    # 状態
    # ------------------------------------------------------------------

    def is_enabled(self):
        return bool(self._enabled and not self._gave_up and not self._closed)

    def profile(self):
        return self._profile

    def tile_sec(self):
        return max(float(self._cfg.get("tile_sec", 4.0) or 4.0),
                   proxy_render.MIN_TILE_SEC)

    def draft_overlay_enabled(self):
        return bool(self._cfg.get("draft_overlay", True))

    def coverage_bar_enabled(self):
        return bool(self._cfg.get("coverage_bar", True))

    # その時刻を含むタイルの (パス, タイル開始秒)。無ければ None
    def tile_at(self, sec):
        if not self.is_enabled():
            return None
        index = proxy_render.tile_index_at(sec, self.tile_sec())
        entry = self._tiles.get(index)
        if entry is None:
            return None
        if not os.path.exists(entry["path"]):
            # 外から消された (ディスク掃除など)。作り直しへ回す
            self._tiles.pop(index, None)
            return None
        start, _end = self._range(index)
        return (entry["path"], start)

    # 次のタイルの (パス, 開始秒)。無ければ None (再生の継ぎに使う)
    def tile_after(self, sec):
        if not self.is_enabled():
            return None
        index = proxy_render.tile_index_at(sec, self.tile_sec()) + 1
        if index >= self._tile_count():
            return None
        start, _end = self._range(index)
        return self.tile_at(start + 1e-3)

    # その時刻のタイルが生成中 / 順番待ちか
    def is_pending(self, sec):
        if not self.is_enabled():
            return False
        index = proxy_render.tile_index_at(sec, self.tile_sec())
        if index in self._tiles:
            return False
        return index < self._tile_count()

    # レンダ済み区間の一覧 [(start, end), ...] (レンダ済みバー用)
    def coverage(self):
        if not self.is_enabled():
            return []
        spans = []
        for index in sorted(self._tiles):
            start, end = self._range(index)
            if spans and abs(spans[-1][1] - start) < 1e-6:
                spans[-1] = (spans[-1][0], end)      # 隣り合うタイルはつなげる
            else:
                spans.append((start, end))
        return [tuple(span) for span in spans]

    # 生成中のタイルの区間 (無ければ None)。点滅表示に使う
    def rendering_span(self):
        if not self.is_enabled() or self._worker is None:
            return None
        key = self._worker.current_key()
        if not key:
            return None
        for index, entry_key in self._planned.items():
            if entry_key == key:
                return self._range(index)
        return None

    # ------------------------------------------------------------------
    # 入力の変化
    # ------------------------------------------------------------------

    # Timeline が変わったら呼ぶ。鍵を取り直し、変わったタイルを捨てて積み直す。
    def invalidate(self):
        if not self.is_enabled():
            return
        stale_keys = []
        for index in list(self._tiles):
            entry = self._tiles[index]
            if entry["key"] != self._key(index):
                stale_keys.append(entry["key"])
                self._remove(index)
        # 生成中・順番待ちのものは、鍵が変わっていれば止める。
        # 変わっていなければそのまま走らせる (作り直す必要が無い)。
        for index, key in list(self._planned.items()):
            if key != self._key(index):
                stale_keys.append(key)
                self._planned.pop(index, None)
        if self._worker is not None:
            self._worker.clear(stale_keys)
        self._focus_index = -1
        self._pump()
        self.coverage_changed.emit()

    # プレイヘッド位置が変わったら呼ぶ。生成の優先順を組み替える。
    #
    # 再生中は 1 秒に何十回も呼ばれるため、**同じタイルの中を動いている間は
    # 何もしない**。鍵の計算を毎フレーム走らせると編集操作の応答を奪う。
    def set_focus(self, sec):
        if not self.is_enabled():
            return
        self._focus = max(float(sec), 0.0)
        index = proxy_render.tile_index_at(self._focus, self.tile_sec())
        if index == self._focus_index:
            return
        self._focus_index = index
        self._pump()

    # 「今出力したら透かしが入るか」を更新する (§4.5)。変われば作り直しになる。
    def set_watermark_required(self, required):
        required = bool(required)
        if required == self._watermark_required:
            return
        self._watermark_required = required
        self._env = None
        self.invalidate()

    # ぼかしのマスクと判定の署名を更新する。変われば作り直しになる。
    def set_blur_mask(self, mask_path, signature=""):
        signature = str(signature or "")
        if mask_path == self._blur_mask_path and signature == self._blur_signature:
            return
        self._blur_mask_path = mask_path
        self._blur_signature = signature
        self._env = None
        self.invalidate()

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------

    # 優先順にタイルを積む (§3.5)
    #   プレイヘッドを含むタイル → 前方 lookahead 本 → 後方 lookahead 本
    def _pump(self):
        if not self.is_enabled() or self._worker is None:
            return
        total = self._tile_count()
        if total <= 0:
            return
        focus = proxy_render.tile_index_at(self._focus, self.tile_sec())
        lookahead = int(self._cfg.get("lookahead_tiles", 3) or 0)

        order = [focus]
        order += [focus + offset for offset in range(1, lookahead + 1)]
        order += [focus - offset for offset in range(1, lookahead + 1)]
        wanted = []
        for index in order:
            if 0 <= index < total and index not in self._tiles and index not in wanted:
                wanted.append(index)
            if len(wanted) >= _MAX_QUEUE:
                break

        for index in wanted:
            self._submit(index)
        self._evict(focus)

    def _submit(self, index):
        if self._failures.get(index, 0) >= _MAX_TILE_FAILURES:
            return
        key = self._key(index)
        path = os.path.join(self._work_dir, f"tile_{index:05d}_{key[:12]}.mp4")
        if os.path.exists(path):
            # 前回の生成物がそのまま使える (鍵がパスに入っているため内容も一致する)。
            # 完成品だけがこの名前になる (生成中は .part / 下記) ので、
            # 途中で死んだファイルを拾うことは無い。
            self._tiles[index] = {"key": key, "path": path}
            self.tile_ready.emit(index)
            self.coverage_changed.emit()
            return
        try:
            command = proxy_render.plan_tile(
                self._controller.timeline, self._environment(), index,
                path + _PART_SUFFIX)
        except Exception:  # noqa: BLE001 (組み立ての失敗で編集画面を落とさない)
            _logger.exception("プロキシのタイルを組み立てられませんでした: #%d", index)
            return
        if command is None:
            return
        self._planned[index] = key
        self._worker.submit(index, key, path + _PART_SUFFIX, command)

    def _on_tile_finished(self, tile_index, key, path, ok, reason):
        self._planned.pop(tile_index, None)
        final = path[:-len(_PART_SUFFIX)] if path.endswith(_PART_SUFFIX) else path
        if self._closed:
            # 画面を閉じたあとに届いた通知。公開せずに捨てる (掃除の取りこぼし防止)
            _remove_file(path)
            _remove_file(final)
            return
        if ok:
            self._failures.pop(tile_index, None)
            # 完了までに Timeline が変わっていたら捨てる (鍵が合わなければ使わない)
            if key == self._key(tile_index) and _publish(path, final):
                self._tiles[tile_index] = {"key": key, "path": final}
                self.tile_ready.emit(tile_index)
                self.coverage_changed.emit()
            else:
                _remove_file(path)
            self._pump()
            return

        if reason == "cancelled":
            _remove_file(path)
            self._pump()
            return
        if reason == "missing":
            # ffmpeg が無い / 起動できない。以降は近似プレビューで動かす (§7)
            self._give_up(tr("ffmpeg を起動できないため、プレビューは近似表示のままになります"))
            return

        self._failures[tile_index] = self._failures.get(tile_index, 0) + 1
        _remove_file(path)
        if self._failures[tile_index] >= _MAX_TILE_FAILURES:
            self._give_up(tr("プレビューの生成に繰り返し失敗したため、近似表示に切り替えます"))
            return
        self._pump()

    # プロキシを諦める (mode="off" 相当へ落ちる)。既存の動作は変わらない (§7)。
    def _give_up(self, message):
        if self._gave_up:
            return
        self._gave_up = True
        _logger.warning("プロキシを諦めました: %s", message)
        if self._worker is not None:
            self._worker.clear()
        self._cleanup_tiles()
        self.coverage_changed.emit()
        self.gave_up.emit(message)

    # 保持数を超えたぶんを、再生ヘッドから遠い順に捨てる
    def _evict(self, focus_index):
        limit = int(self._cfg.get("cache_tiles", 64) or 64)
        if len(self._tiles) <= limit:
            return
        far = sorted(self._tiles, key=lambda index: abs(index - focus_index),
                     reverse=True)
        for index in far[:len(self._tiles) - limit]:
            self._remove(index)
        self.coverage_changed.emit()

    def _remove(self, index):
        entry = self._tiles.pop(index, None)
        if entry:
            _remove_file(entry["path"])

    # ------------------------------------------------------------------
    # 後片付け
    # ------------------------------------------------------------------

    # 画面クローズ時に必ず呼ぶ (スレッド終了 + 一時ファイル削除)
    def shutdown(self):
        self._closed = True
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(5000)
            self._worker = None
        self._cleanup_tiles()

    def _cleanup_tiles(self):
        for index in list(self._tiles):
            self._remove(index)
        self._tiles = {}
        # 鍵が変わって残ったファイルも含めて掃除する
        try:
            if os.path.isdir(self._work_dir):
                for name in os.listdir(self._work_dir):
                    if name.startswith("tile_") or name.startswith("proxy_tile_"):
                        _remove_file(os.path.join(self._work_dir, name))
        except OSError:
            _logger.debug("プロキシの置き場を掃除できませんでした")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    # 生成の前提 (寸法・字幕設定・ぼかし・透かし) をまとめた入れ物。
    # Timeline そのものは持たないため、作り置きしても古い内容を見ることはない。
    def _environment(self):
        if self._env is not None:
            return self._env
        eff_cfg, font_profile = _subtitle_profile(
            self._settings, self._controller.timeline)
        self._env = proxy_render.ProxyEnvironment(
            self._settings, self._profile, self._cfg, self._work_dir,
            eff_cfg=eff_cfg, font_profile=font_profile,
            fonts_dir=resolve_fonts_dir(self._settings),
            blur_mask_path=self._blur_mask_path,
            blur_signature=self._blur_signature,
            watermark_required=self._watermark_required)
        return self._env

    def _key(self, index):
        return proxy_render.tile_key(
            self._controller.timeline, self._environment(), index)

    def _range(self, index):
        return proxy_render.tile_range(
            self._controller.timeline, index, self.tile_sec(), self._profile.fps)

    def _tile_count(self):
        return proxy_render.tile_count(
            self._controller.timeline, self.tile_sec(), self._profile.fps)


# 実効字幕設定と FontProfile を解決する (縦動画の上書きを反映する)
# PipelineContext を持たない編集画面のため、PreviewPanel と同じ組み立てを使う。
def _subtitle_profile(settings, timeline):
    from ...modules import subtitle_generator          # noqa: PLC0415 (循環回避)

    eff_cfg = subtitle_generator.build_effective_subtitle_cfg(
        settings.get("subtitle", {}), settings.get("vertical", {}),
        {"is_portrait": timeline.orientation == "portrait",
         "width": timeline.width, "height": timeline.height})
    return eff_cfg, subtitle_generator.build_font_profile(eff_cfg)


# 生成し終えたタイルを正式な名前へ置き換える
def _publish(part_path, final_path):
    try:
        os.replace(part_path, final_path)
        return True
    except OSError:
        _logger.warning("プロキシのタイルを公開できませんでした: %s", final_path)
        return False


def _remove_file(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        _logger.debug("プロキシのタイルを削除できませんでした: %s", path)


# ffmpeg が使える環境かどうか (画面を開く前の判定に使う)
def ffmpeg_available(settings):
    try:
        return bool(ffmpeg_runner.get_ffmpeg_exe(settings.get("ffmpeg", {})))
    except Exception:  # noqa: BLE001
        return False
