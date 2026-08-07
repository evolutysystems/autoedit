# プレビュー (docs/request/ver3/resolve.md §6.4 / §6.5)
# 上部に置かれ、再生ヘッドが示すフレームを表示しながら、その上で画像・字幕を
# 選択・ドラッグ・右クリック操作できる「編集画面かつ再生機」。
#
#   ・フレーム取得は PyAV でプロセス内デコードし、再生ヘッドの移動へ追従する (§6.4-1)
#   ・オーバーレイは QGraphicsItem として重ね、選択・ドラッグ・レイヤー変更を行う (§6.5)
#   ・音声は QMediaPlayer をマスタークロックにして再生し、映像を追従させる (§6.4-5)
import os
import subprocess

from PySide6.QtCore import (
    QMutex,
    QMutexLocker,
    QThread,
    QTimer,
    QUrl,
    QWaitCondition,
    Qt,
    Signal,
)
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

# QtMultimedia はビルド/環境により使用不可のことがあるため防御的に import する (§6.4-5)
try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    _MULTIMEDIA_AVAILABLE = True
except Exception:  # noqa: BLE001
    _MULTIMEDIA_AVAILABLE = False

from ...modules import ffmpeg_runner, subtitle_generator
from ...settings.settings_window import resolve_fonts_dir
from ...timeline import commands
from ...timeline.audio_source import AudioChunkSource
from ...timeline.frame_source import create_frame_source, is_pyav_available
from ...timeline.model import SubtitleClip
from ...utils.logger import get_logger
from ...utils.proc import no_window_creationflags
from .preview_items import (
    BaseFrameItem,
    ImageOverlayItem,
    SubtitleOverlayItem,
    pixel_to_normalized,
)
from .timeline_view import format_time_precise

_logger = get_logger(__name__)


# ==================================================================
# ワーカー
# ==================================================================

# フレーム取得ワーカー (常駐。最新の要求だけを処理する)
class _FrameWorker(QThread):

    frame_ready = Signal(int, int, int, object)   # (job_id, width, height, rgb_bytes)
    frame_failed = Signal(int)

    def __init__(self, frame_source, parent=None):
        super().__init__(parent)
        self._frame_source = frame_source
        self._mutex = QMutex()
        self._condition = QWaitCondition()
        self._pending = None
        self._stopping = False

    # 取得を要求する (処理中に上書きされた古い要求は捨てられる)
    def request(self, job_id, media, source_sec):
        with QMutexLocker(self._mutex):
            self._pending = (job_id, media, source_sec)
            self._condition.wakeAll()

    def stop(self):
        with QMutexLocker(self._mutex):
            self._stopping = True
            self._condition.wakeAll()

    def run(self):
        while True:
            with QMutexLocker(self._mutex):
                while self._pending is None and not self._stopping:
                    self._condition.wait(self._mutex)
                if self._stopping:
                    return
                job_id, media, source_sec = self._pending
                self._pending = None
            try:
                result = self._frame_source.frame_at(media, source_sec)
            except Exception:  # noqa: BLE001 (取得失敗で画面を落とさない)
                _logger.exception("フレーム取得に失敗しました")
                result = None
            if result is None:
                self.frame_failed.emit(job_id)
            else:
                width, height, data = result
                self.frame_ready.emit(job_id, width, height, data)


# 音声チャンク生成ワーカー (1 要求ごとに使い捨て)
class _AudioWorker(QThread):

    chunk_ready = Signal(int, str, float)   # (job_id, path, chunk_start)
    chunk_failed = Signal(int)

    def __init__(self, audio_source, job_id, start_sec, length_sec, parent=None):
        super().__init__(parent)
        self._audio_source = audio_source
        self._job_id = job_id
        self._start = start_sec
        self._length = length_sec

    def run(self):
        try:
            result = self._audio_source.build(self._start, self._length)
        except Exception:  # noqa: BLE001
            _logger.exception("プレビュー音声の生成に失敗しました")
            result = None
        if result is None:
            self.chunk_failed.emit(self._job_id)
        else:
            path, chunk_start = result
            self.chunk_ready.emit(self._job_id, path, chunk_start)


# ==================================================================
# プレビュー本体
# ==================================================================

class PreviewPanel(QWidget):

    # 再生状態の変化 (再生中は編集操作を止めるため画面側へ通知する / §4-8)
    playing_changed = Signal(bool)

    def __init__(self, controller, work_dir, asr_audio_path=None, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._cfg = controller.cfg["preview"]
        self._settings = controller.settings
        self._canvas = (controller.timeline.width, controller.timeline.height)
        self._work_dir = work_dir

        # 実効字幕設定 (縦動画の上書き反映済み) と FontProfile を 1 回だけ解決する
        self._eff_cfg = subtitle_generator.build_effective_subtitle_cfg(
            self._settings.get("subtitle", {}), self._settings.get("vertical", {}),
            {"is_portrait": controller.timeline.orientation == "portrait",
             "width": self._canvas[0], "height": self._canvas[1]},
        )
        self._font_profile = subtitle_generator.build_font_profile(self._eff_cfg)

        # フレーム取得
        self._frame_source = create_frame_source(self._settings, self._cfg)
        self._frame_worker = _FrameWorker(self._frame_source, parent=self)
        self._frame_worker.frame_ready.connect(self._on_frame_ready)
        self._frame_worker.frame_failed.connect(self._on_frame_failed)
        self._frame_worker.start()
        self._frame_job = 0

        # 音声
        self._audio_source = AudioChunkSource(
            controller.timeline, self._settings, work_dir, asr_audio_path)
        self._audio_worker = None
        self._audio_job = 0
        self._chunk_start = 0.0
        self._playing = False
        self._last_frame_at = 0.0

        self._overlay_items = {}
        self._build_ui()

        controller.timeline_changed.connect(self._on_timeline_changed)
        controller.playhead_moved.connect(self._on_playhead_moved)
        controller.selection_changed.connect(self._sync_selection)

        self._refresh_all()

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        self._scene = QGraphicsScene(self)
        self._scene.setSceneRect(0, 0, self._canvas[0], self._canvas[1])
        self._scene.selectionChanged.connect(self._on_scene_selection_changed)

        self._view = _PreviewGraphicsView(self, self._scene)
        self._view.setRenderHints(self._view.renderHints())
        self._view.setBackgroundBrush(Qt.black)
        root.addWidget(self._view, 1)

        self._base_item = BaseFrameItem()
        self._base_item.show_blank(self._canvas)
        self._scene.addItem(self._base_item)

        # トランスポート
        row = QHBoxLayout()
        self.play_button = QPushButton("▶")
        self.play_button.setFixedWidth(36)
        self.play_button.setToolTip("再生 / 一時停止 (Space)")
        self.play_button.clicked.connect(self.toggle_play)
        row.addWidget(self.play_button)

        prev_button = QPushButton("⏮")
        prev_button.setFixedWidth(32)
        prev_button.setToolTip("先頭へ")
        prev_button.clicked.connect(lambda: self._controller.set_playhead(0.0))
        row.addWidget(prev_button)

        next_button = QPushButton("⏭")
        next_button.setFixedWidth(32)
        next_button.setToolTip("末尾へ")
        next_button.clicked.connect(
            lambda: self._controller.set_playhead(
                self._controller.timeline.duration_sec()))
        row.addWidget(next_button)

        self.time_label = QLabel("0:00:00.00 / 0:00:00.00")
        row.addWidget(self.time_label)
        row.addStretch(1)

        # 音声コントロール (QtMultimedia が無い環境では無効化する)
        self._player = None
        self._audio_output = None
        if _MULTIMEDIA_AVAILABLE:
            self._audio_output = QAudioOutput(self)
            self._audio_output.setMuted(not self._cfg["audio_enabled"])
            self._audio_output.setVolume(float(self._cfg["audio_volume"]))
            self._player = QMediaPlayer(self)
            self._player.setAudioOutput(self._audio_output)
            self._player.positionChanged.connect(self._on_audio_position)
            self._player.errorOccurred.connect(self._on_audio_error)
            self._player.mediaStatusChanged.connect(self._on_audio_status)

            self.mute_button = QPushButton("🔊" if self._cfg["audio_enabled"] else "🔇")
            self.mute_button.setFixedWidth(32)
            self.mute_button.setToolTip("ミュート切り替え")
            self.mute_button.clicked.connect(self._toggle_mute)
            row.addWidget(self.mute_button)

            self.volume_slider = QSlider(Qt.Horizontal)
            self.volume_slider.setFixedWidth(90)
            self.volume_slider.setRange(0, 100)
            self.volume_slider.setValue(int(float(self._cfg["audio_volume"]) * 100))
            self.volume_slider.valueChanged.connect(
                lambda v: self._audio_output.setVolume(v / 100.0))
            row.addWidget(self.volume_slider)
        else:
            self.mute_button = None
            self.volume_slider = None
            _logger.info("QtMultimedia が利用できないためプレビュー音声を無効にします")

        if self._cfg["high_quality_button"]:
            hq_button = QPushButton("高精度プレビュー")
            hq_button.setToolTip(
                "現在のフレームを本番と同じ ASS で描画し直して表示します "
                "(画面上の字幕は Qt による近似表示です)")
            hq_button.clicked.connect(self._show_high_quality)
            row.addWidget(hq_button)
        root.addLayout(row)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color:#888;")
        root.addWidget(self.status_label)

        if not is_pyav_available():
            self.status_label.setText("PyAV が利用できないため ffmpeg でフレームを取得します")

    # ------------------------------------------------------------------
    # 表示更新
    # ------------------------------------------------------------------

    def _on_timeline_changed(self):
        # 編集が入ったら再生用に生成済みの音声を捨てる (§6.4-5)
        self._audio_source.invalidate()
        self._refresh_all()

    def _on_playhead_moved(self, _sec):
        self._request_frame()
        self._rebuild_overlays()
        self._update_time_label()

    def _refresh_all(self):
        self._request_frame()
        self._rebuild_overlays()
        self._update_time_label()

    def _update_time_label(self):
        total = self._controller.timeline.duration_sec()
        self.time_label.setText(
            f"{format_time_precise(self._controller.playhead())} / "
            f"{format_time_precise(total)}")

    # 再生ヘッド位置のフレームを要求する (常駐ワーカーが最新だけを処理する)
    def _request_frame(self):
        resolved = self._controller.source_at_playhead()
        if resolved is None:
            # 空白 (ギャップ) は黒フレーム
            self._base_item.show_blank(self._canvas)
            return
        media, source_sec = resolved
        self._frame_job += 1
        self._frame_worker.request(self._frame_job, media, source_sec)

    def _on_frame_ready(self, job_id, width, height, data):
        if job_id != self._frame_job:
            return  # 差し替え済みの古い結果は捨てる
        image = QImage(data, width, height, width * 3, QImage.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(image)
        if pixmap.width() != self._canvas[0] or pixmap.height() != self._canvas[1]:
            # 素材寸法がキャンバスと異なる場合は縦横比を保って中央へ収める
            pixmap = pixmap.scaled(self._canvas[0], self._canvas[1],
                                   Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._base_item.setPixmap(pixmap)
        self._base_item.setPos((self._canvas[0] - pixmap.width()) / 2.0,
                               (self._canvas[1] - pixmap.height()) / 2.0)

    def _on_frame_failed(self, job_id):
        if job_id != self._frame_job:
            return
        self._base_item.show_blank(self._canvas)

    # 再生ヘッド位置に表示されるオーバーレイだけをシーンへ並べ直す
    def _rebuild_overlays(self):
        for item in self._overlay_items.values():
            self._scene.removeItem(item)
        self._overlay_items = {}

        playhead = self._controller.playhead()
        selected = set(self._controller.selected_ids())
        for element in self._controller.timeline.overlay_elements():
            if not element.contains(playhead):
                continue
            item = self._make_item(element)
            if item is None:
                continue
            item.setZValue(element.z_order)
            item.move_finished = self._on_overlay_moved
            self._scene.addItem(item)
            self._overlay_items[element.id] = item
            if element.id in selected:
                item.setSelected(True)

    def _make_item(self, element):
        try:
            if isinstance(element, SubtitleClip):
                return SubtitleOverlayItem(
                    element, self._eff_cfg, self._font_profile, self._canvas)
            media = self._controller.timeline.media_by_id(element.media_id)
            if media is None:
                return None
            pixmap = self._overlay_pixmap(media, element)
            if pixmap is None or pixmap.isNull():
                return None
            return ImageOverlayItem(element, pixmap, self._canvas)
        except Exception:  # noqa: BLE001 (描画失敗で画面を落とさない)
            _logger.exception("オーバーレイの生成に失敗しました: %s", element.id)
            return None

    # オーバーレイ素材の見た目を得る (画像はそのまま、動画は現在位置のフレーム)
    def _overlay_pixmap(self, media, clip):
        if media.is_image():
            return QPixmap(media.path)
        offset = max(self._controller.playhead() - clip.timeline_start, 0.0)
        result = self._frame_source.frame_at(media, clip.source_in + offset)
        if result is None:
            return None
        width, height, data = result
        image = QImage(data, width, height, width * 3, QImage.Format_RGB888).copy()
        return QPixmap.fromImage(image)

    # ------------------------------------------------------------------
    # 選択・ドラッグ・右クリック (§6.5)
    # ------------------------------------------------------------------

    def _on_scene_selection_changed(self):
        ids = [item.clip_id for item in self._scene.selectedItems()
               if hasattr(item, "clip_id")]
        if ids:
            self._controller.select(ids)

    # Timeline 側の選択をプレビューへ反映する (双方向同期)
    def _sync_selection(self, _clip):
        selected = set(self._controller.selected_ids())
        self._scene.blockSignals(True)
        for clip_id, item in self._overlay_items.items():
            item.setSelected(clip_id in selected)
        self._scene.blockSignals(False)

    # ドラッグ確定 → 正規化座標へ直してコマンドを積む
    def _on_overlay_moved(self, clip_id, center_x, center_y):
        x, y = pixel_to_normalized(center_x, center_y, *self._canvas)
        self._controller.move_overlay(clip_id, x, y)

    # プレビュー上の右クリックメニュー (削除 / レイヤー / §6.5-2)
    def show_context_menu(self, global_pos, scene_pos):
        item = self._scene.itemAt(scene_pos, self._view.transform())
        clip_id = getattr(item, "clip_id", None)
        if clip_id is None:
            return
        if clip_id not in self._controller.selected_ids():
            self._controller.select([clip_id])

        menu = QMenu(self)
        menu.addAction("削除", lambda: self._controller.delete_clips([clip_id]))
        layer = menu.addMenu("レイヤー")
        layer.addAction("最前面",
                        lambda: self._controller.change_layer(clip_id, commands.LAYER_TOP))
        layer.addAction("前面",
                        lambda: self._controller.change_layer(clip_id, commands.LAYER_UP))
        layer.addAction("背面",
                        lambda: self._controller.change_layer(clip_id, commands.LAYER_DOWN))
        layer.addAction("最背面",
                        lambda: self._controller.change_layer(clip_id, commands.LAYER_BOTTOM))
        menu.exec(global_pos)

    # ------------------------------------------------------------------
    # 音声再生 (§6.4-5)
    # ------------------------------------------------------------------

    def toggle_play(self):
        if self._playing:
            self.pause()
        else:
            self.play()

    def play(self):
        if self._player is None:
            self.status_label.setText("音声を再生できない環境のため再生できません")
            return
        total = self._controller.timeline.duration_sec()
        if self._controller.playhead() >= total - 0.05:
            self._controller.set_playhead(0.0)

        start = self._controller.playhead()
        hit = self._audio_source.cached(start, self._cfg["audio_chunk_sec"])
        if hit is not None:
            self._start_playback(*hit)
            return
        # チャンクが無い → 生成してから再生する
        self.status_label.setText("音声を準備中…")
        self._audio_job += 1
        self._audio_worker = _AudioWorker(
            self._audio_source, self._audio_job, start,
            self._cfg["audio_chunk_sec"], parent=self)
        self._audio_worker.chunk_ready.connect(self._on_chunk_ready)
        self._audio_worker.chunk_failed.connect(self._on_chunk_failed)
        self._audio_worker.start()

    def _on_chunk_ready(self, job_id, path, chunk_start):
        if job_id != self._audio_job:
            return
        self.status_label.setText("")
        self._start_playback(path, chunk_start)

    def _on_chunk_failed(self, job_id):
        if job_id != self._audio_job:
            return
        self.status_label.setText("音声を再生できません (無音で再生します)")
        self._start_silent_playback()

    def _start_playback(self, path, chunk_start):
        self._chunk_start = float(chunk_start)
        offset_ms = int(max(self._controller.playhead() - self._chunk_start, 0.0) * 1000)
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.setPosition(offset_ms)
        self._player.play()
        self._set_playing(True)

    # 音声が使えないときはタイマーで再生ヘッドだけを進める (§6.4-5 利用不可時)
    def _start_silent_playback(self):
        self._silent_timer = QTimer(self)
        interval = int(1000 / max(int(self._cfg["play_fps"]), 1))
        self._silent_timer.setInterval(interval)
        self._silent_timer.timeout.connect(
            lambda: self._controller.step_playhead(
                self._controller.timeline.fps / max(int(self._cfg["play_fps"]), 1)))
        self._silent_timer.start()
        self._set_playing(True)

    def pause(self):
        if self._player is not None:
            self._player.pause()
        timer = getattr(self, "_silent_timer", None)
        if timer is not None:
            timer.stop()
        self._set_playing(False)

    def _set_playing(self, playing):
        if self._playing == playing:
            return
        self._playing = playing
        self.play_button.setText("⏸" if playing else "▶")
        self.playing_changed.emit(playing)

    # 音声位置がマスタークロック。ここから再生ヘッドを進める。
    def _on_audio_position(self, position_ms):
        if not self._playing:
            return
        sec = self._chunk_start + position_ms / 1000.0
        total = self._controller.timeline.duration_sec()
        if sec >= total:
            self.pause()
            self._controller.set_playhead(total)
            return
        # 映像の更新は play_fps で頭打ちにする (音声を優先し、間に合わなければ間引く)
        min_interval = 1.0 / max(int(self._cfg["play_fps"]), 1)
        if sec - self._last_frame_at >= min_interval:
            self._last_frame_at = sec
            self._controller.set_playhead(sec)

        # 残りが少なくなったら次チャンクを先読みする
        remaining = (self._player.duration() - position_ms) / 1000.0
        if remaining <= float(self._cfg["audio_prefetch_sec"]):
            self._prefetch_next(sec)

    def _prefetch_next(self, current_sec):
        next_start = self._chunk_start + self._player.duration() / 1000.0
        if next_start >= self._controller.timeline.duration_sec() - 0.05:
            return
        if self._audio_worker is not None and self._audio_worker.isRunning():
            return
        self._audio_job += 1
        self._audio_worker = _AudioWorker(
            self._audio_source, self._audio_job, next_start,
            self._cfg["audio_chunk_sec"], parent=self)
        self._audio_worker.chunk_ready.connect(self._on_chunk_ready)
        self._audio_worker.chunk_failed.connect(self._on_chunk_failed)
        self._audio_worker.start()

    def _on_audio_status(self, status):
        if not self._playing or self._player is None:
            return
        if status == QMediaPlayer.EndOfMedia:
            # チャンクの終端 → 次のチャンクから続ける
            next_start = self._chunk_start + self._player.duration() / 1000.0
            if next_start >= self._controller.timeline.duration_sec() - 0.05:
                self.pause()
                return
            self._controller.set_playhead(next_start)
            self.play()

    def _on_audio_error(self, error, error_string):
        if _MULTIMEDIA_AVAILABLE and error == QMediaPlayer.NoError:
            return
        _logger.warning("プレビュー音声の再生エラー: %s", error_string)
        self.status_label.setText("音声を再生できません (無音で再生します)")
        self._start_silent_playback()

    def _toggle_mute(self):
        if self._audio_output is None:
            return
        muted = not self._audio_output.isMuted()
        self._audio_output.setMuted(muted)
        self.mute_button.setText("🔇" if muted else "🔊")

    # ------------------------------------------------------------------
    # 高精度プレビュー (§6.4-4)
    # ------------------------------------------------------------------

    # 現在のフレームを本番と同じ経路 (ASS + ffmpeg) で描き直して別窓に出す
    def _show_high_quality(self):
        resolved = self._controller.source_at_playhead()
        if resolved is None:
            self.status_label.setText("この位置には映像がありません")
            return
        media, source_sec = resolved
        playhead = self._controller.playhead()

        # 現在時刻を 0 とみなす 1 件ぶんの ASS を書き出す
        items = []
        for clip in self._controller.timeline.subtitle_clips():
            if not clip.contains(playhead):
                continue
            item = clip.to_item()
            item["start"], item["end"] = 0.0, 1.0
            items.append(item)
        ass_path = os.path.join(self._work_dir, "preview_hq.ass")
        subtitle_generator.build_subtitle_file(
            items, self._font_profile, ass_path,
            video_width=self._canvas[0], video_height=self._canvas[1])

        out_path = os.path.join(self._work_dir, "preview_hq.png")
        ffmpeg_cfg = self._settings.get("ffmpeg", {})
        safe_ass = ass_path.replace("\\", "/").replace(":", "\\:")
        chain = (f"scale={self._canvas[0]}:{self._canvas[1]}:"
                 f"force_original_aspect_ratio=decrease,"
                 f"pad={self._canvas[0]}:{self._canvas[1]}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                 f"ass='{safe_ass}'")
        fonts_dir = resolve_fonts_dir(self._settings)
        if fonts_dir and os.path.isdir(fonts_dir):
            safe_dir = fonts_dir.replace("\\", "/").replace(":", "\\:")
            chain = chain.replace(f"ass='{safe_ass}'",
                                  f"ass='{safe_ass}':fontsdir='{safe_dir}'")
        cmd = [
            ffmpeg_runner.get_ffmpeg_exe(ffmpeg_cfg), "-y", "-hide_banner",
            "-loglevel", "error",
            "-ss", f"{source_sec:.3f}", "-i", media.path,
            "-frames:v", "1", "-vf", chain, out_path,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, creationflags=no_window_creationflags())
        except OSError as e:
            self.status_label.setText(f"高精度プレビューに失敗しました: {e}")
            return
        if result.returncode != 0 or not os.path.exists(out_path):
            self.status_label.setText("高精度プレビューに失敗しました")
            return
        _HighQualityDialog(out_path, self).exec()

    # ------------------------------------------------------------------
    # 後片付け
    # ------------------------------------------------------------------

    # 画面クローズ時に必ず呼ぶ (再生停止・スレッド終了・一時ファイル削除)
    def shutdown(self):
        self.pause()
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())
        if self._audio_worker is not None and self._audio_worker.isRunning():
            self._audio_worker.wait(2000)
        self._frame_worker.stop()
        self._frame_worker.wait(2000)
        self._frame_source.close()
        self._audio_source.cleanup()


# プレビューの QGraphicsView (右クリックメニューを親へ委譲する)
class _PreviewGraphicsView(QGraphicsView):

    def __init__(self, panel, scene):
        super().__init__(scene)
        self._panel = panel
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

    # キャンバス全体が常に収まるようにする
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)

    def contextMenuEvent(self, event):
        self._panel.show_context_menu(event.globalPos(), self.mapToScene(event.pos()))


# 高精度プレビューの表示ダイアログ
class _HighQualityDialog(QDialog):

    def __init__(self, image_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("高精度プレビュー (本番と同じ描画)")
        layout = QVBoxLayout(self)
        label = QLabel()
        pixmap = QPixmap(image_path)
        if not pixmap.isNull():
            label.setPixmap(pixmap.scaledToWidth(900, Qt.SmoothTransformation))
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        note = QLabel(
            "この画像は本番と同じ ASS 描画です。編集画面上の字幕は Qt による近似表示のため、"
            "縁取りや影の見え方が異なります。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#888;")
        layout.addWidget(note)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)
