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
    QApplication,
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

from ...modules import comment_decor, ffmpeg_runner, subtitle_generator
from ...settings.settings_window import resolve_fonts_dir
from ...timeline import commands
from ...timeline.audio_source import AudioChunkSource
from ...timeline.frame_source import create_frame_source, is_pyav_available
from ...timeline.model import SubtitleClip
from ...utils.logger import get_logger
from ...utils.proc import no_window_creationflags
from .. import theme
from .preview_items import (
    BaseFrameItem,
    ImageOverlayItem,
    SubtitleOverlayItem,
    native_scale,
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

        # 音声 (生成方式・品質・保持本数は preview 設定から / resolve6 §5.6)
        self._audio_source = AudioChunkSource(
            controller.timeline, self._settings, work_dir, asr_audio_path,
            preview_cfg=self._cfg)
        self._audio_worker = None
        self._audio_job = 0
        # 完了したら再生を始めるジョブ ID (先読みジョブと区別する)
        self._autoplay_job = 0
        # 起動用チャンクの完了後に本チャンクを先読みするジョブ ID (resolve6 §3-6 R3)
        self._follow_full_job = 0
        self._chunk_start = 0.0
        # 再生状態は速度 1 つで持つ (resolve2 §4-9)
        #   0.0=停止 / +1.0=通常再生 / +N=倍速再生 / -N=倍速逆再生
        self._rate = 0.0
        self._pending_rate = 0.0     # 音声チャンク生成の完了後に適用する速度
        self._silent_timer = None
        self._reverse_timer = None
        self._last_frame_at = 0.0

        # スクラブ (再生ヘッドドラッグ中の音声 / resolve7 §5.13)
        #   ・self._rate は 0 のまま = 音声は再生ヘッドを動かさない (§2.5.2)
        #   ・一定間隔で「今の再生ヘッド位置」へ合わせ直し、その場から鳴らす
        self._scrub_timer = QTimer(self)
        self._scrub_timer.timeout.connect(self._scrub_tick)
        self._scrub_last_sec = None       # 直前の粒で鳴らした位置
        self._scrub_prev_sec = None       # "match" のときの速度計算用
        self._scrub_idle_ms = 0           # 動きが無い時間の合計
        self._scrub_source = None         # 現在 QMediaPlayer に載せているチャンク
        self._scrub_volume_backup = None  # スクラブ前の音量 (離したら戻す)

        self._overlay_items = {}
        # 直前に並べたオーバーレイの ID 列 (再生中の作り直しを省くため / resolve6 §3-9)
        self._overlay_ids = []
        self._build_ui()

        controller.timeline_changed.connect(self._on_timeline_changed)
        controller.playhead_moved.connect(self._on_playhead_moved)
        controller.selection_changed.connect(self._sync_selection)
        controller.scrub_changed.connect(self._on_scrub_changed)

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
        # 映像の周囲は黒のまま (焼き込み結果の色判断を誤らせないため / resolve3 §5.6)。
        # 枠だけをガラスにし、映像そのものには一切手を入れない。
        self._view.setBackgroundBrush(Qt.black)
        theme.mark_preview_canvas(self._view)
        root.addWidget(self._view, 1)

        self._base_item = BaseFrameItem()
        self._base_item.show_blank(self._canvas)
        self._scene.addItem(self._base_item)

        # トランスポート (resolve2 §5.6-4)
        # ボタン幅は設定から引く (ver3 resolve4 E2)。テーマの QSS が左右に余白を持つため、
        # 記号が見切れない幅を明示する。2 記号のボタン (◀◀ ▶▶) だけ広い値を使う。
        icon_width = int(self._cfg["transport_button_width_px"])
        wide_width = int(self._cfg["transport_wide_button_width_px"])

        row = QHBoxLayout()
        # 「高精度プレビュー」が列の末尾に来るため、列の右余白がそのまま右余白になる (E3)
        row.setContentsMargins(0, 0, int(self._cfg["transport_row_right_margin_px"]), 0)

        prev_button = QPushButton("⏮")
        prev_button.setFixedWidth(icon_width)
        theme.mark_icon_button(prev_button)
        prev_button.setToolTip("先頭へ (Home)")
        prev_button.clicked.connect(lambda: self._controller.set_playhead(0.0))
        row.addWidget(prev_button)

        rate = float(self._cfg["playback_rate"])
        self.backward_button = QPushButton("◀◀")
        self.backward_button.setFixedWidth(wide_width)
        theme.mark_icon_button(self.backward_button)
        self.backward_button.setCheckable(True)
        self.backward_button.setToolTip(f"{rate:g} 倍速逆再生 (Q) ※音声は出ません")
        self.backward_button.clicked.connect(lambda: self.toggle_rate(-rate))
        row.addWidget(self.backward_button)

        self.play_button = QPushButton("▶")
        self.play_button.setFixedWidth(icon_width)
        theme.mark_icon_button(self.play_button)
        self.play_button.setCheckable(True)
        self.play_button.setToolTip("再生 / 停止 (Space)")
        self.play_button.clicked.connect(self.toggle_play)
        row.addWidget(self.play_button)

        self.forward_button = QPushButton("▶▶")
        self.forward_button.setFixedWidth(wide_width)
        theme.mark_icon_button(self.forward_button)
        self.forward_button.setCheckable(True)
        self.forward_button.setToolTip(f"{rate:g} 倍速再生 (E)")
        self.forward_button.clicked.connect(lambda: self.toggle_rate(+rate))
        row.addWidget(self.forward_button)

        next_button = QPushButton("⏭")
        next_button.setFixedWidth(icon_width)
        theme.mark_icon_button(next_button)
        next_button.setToolTip("末尾へ (End)")
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
            self.mute_button.setFixedWidth(icon_width)
            theme.mark_icon_button(self.mute_button)
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
            # 音声を伴う再生 (通常再生・倍速再生) は使えない。
            # 倍速逆再生は音声を使わないためそのまま動く (resolve2 §7)。
            self.play_button.setEnabled(False)
            self.forward_button.setEnabled(False)
            _logger.info("QtMultimedia が利用できないためプレビュー音声を無効にします")
            _logger.info("QtMultimedia が利用できないため倍速再生を無効にします")

        if self._cfg["high_quality_button"]:
            hq_button = QPushButton("高精度プレビュー")
            hq_button.setToolTip(
                "現在のフレームを本番と同じ ASS で描画し直して表示します "
                "(画面上の字幕は Qt による近似表示です)")
            hq_button.clicked.connect(self._show_high_quality)
            row.addWidget(hq_button)
        root.addLayout(row)

        self.status_label = QLabel("")
        theme.mark_note(self.status_label)
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

    # 状態表示を更新する (再生状態・失敗の案内など。画面側からも呼ぶ)
    def set_status(self, text):
        self.status_label.setText(text or "")

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
        # 拡縮はアイテムの scale で行い、QPixmap.scaled は使わない (resolve6 §5.8)。
        # 再生中は縮小フレームが届くため、ここで拡大し直すと 1 枚あたり 4.5ms かかり
        # 縮小で浮いたぶんを食い潰す。scale なら描画時に処理され、費用はほぼゼロ。
        scale = 1.0
        if pixmap.width() > 0 and pixmap.height() > 0:
            scale = min(self._canvas[0] / pixmap.width(),
                        self._canvas[1] / pixmap.height())
        self._base_item.setPixmap(pixmap)
        self._base_item.setScale(scale)
        # 縦横比を保って中央へ収める
        self._base_item.setPos((self._canvas[0] - pixmap.width() * scale) / 2.0,
                               (self._canvas[1] - pixmap.height() * scale) / 2.0)

    def _on_frame_failed(self, job_id):
        if job_id != self._frame_job:
            return
        self._base_item.show_blank(self._canvas)

    # 再生ヘッド位置に表示されるオーバーレイだけをシーンへ並べ直す
    def _rebuild_overlays(self):
        playhead = self._controller.playhead()
        elements = [e for e in self._controller.timeline.overlay_elements()
                    if e.contains(playhead)]
        ids = [e.id for e in elements]
        # 再生中は、映るオーバーレイの顔ぶれが変わらないかぎり作り直さない
        # (resolve6 §3-9)。字幕は表示中ずっと同じ見た目のため差は出ない。
        # 映像オーバーレイの絵は止まるが、作り直すと GUI スレッドで
        # フレームを同期デコードすることになり再生そのものが引っかかる。
        if self.is_playing() and ids == self._overlay_ids:
            return
        self._overlay_ids = ids

        for item in self._overlay_items.values():
            self._scene.removeItem(item)
        self._overlay_items = {}

        selected = set(self._controller.selected_ids())
        for element in elements:
            item = self._make_item(element)
            if item is None:
                continue
            item.setZValue(element.z_order)
            item.move_finished = self._on_overlay_moved
            # 大きさの確定も位置と同じ規約で受ける (resolve7 §5.4)。
            # 再生中は編集を止める既存方針に合わせ、ハンドルを掴めなくする。
            if hasattr(item, "set_resizable"):
                item.resize_finished = self._on_overlay_resized
                item.set_resizable(not self.is_playing())
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
            return ImageOverlayItem(element, pixmap, self._canvas,
                                    overlay_cfg=self._controller.cfg["overlay"])
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

    # ハンドルのドラッグ確定 → コマンドを積む (resolve7 §5.4)
    def _on_overlay_resized(self, clip_id, scale):
        self._controller.resize_overlay(clip_id, scale)

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
        # 大きさ (画像・動画オーバーレイのみ / resolve7 §5.4)。
        # 字幕の大きさはインスペクタの「サイズ」(フォントサイズ) で変えるため出さない。
        if hasattr(item, "set_resizable"):
            size_menu = menu.addMenu("大きさ")
            native = native_scale(self._controller.timeline,
                                  self._controller.timeline.clip_by_id(clip_id))
            action = size_menu.addAction(
                "原寸", lambda: self._controller.resize_overlay(clip_id, native))
            action.setEnabled(native is not None)
            size_menu.addAction(
                "50%", lambda: self._controller.resize_overlay(clip_id, 0.5))
            size_menu.addAction(
                "100%（画面幅）", lambda: self._controller.resize_overlay(clip_id, 1.0))
        menu.exec(global_pos)

    # ------------------------------------------------------------------
    # 再生制御 (§6.4-5 / resolve2 §5.6)
    # ------------------------------------------------------------------
    # 再生状態は速度 self._rate 1 つで持つ (resolve2 §4-9):
    #   0.0 = 停止 / +1.0 = 通常再生 / +N = 倍速再生 / -N = 倍速逆再生
    # 符号が向き、絶対値が速さ、ゼロが停止を表す。

    # 再生中か (playing_changed の判定と外部からの参照用)
    def is_playing(self):
        return abs(self._rate) > 1e-6

    # 通常再生 / 停止のトグル (Space)
    def toggle_play(self):
        self.toggle_rate(1.0)

    # 指定速度へ切り替える。同じ速度で再生中なら停止する (トグル / resolve2 §3-6-3)
    #   rate > 0 : QMediaPlayer で再生 (音声あり)
    #   rate < 0 : QTimer で再生ヘッドを後退 (音声なし / resolve2 §3-6-2)
    def toggle_rate(self, rate):
        rate = float(rate)
        if abs(self._rate - rate) < 1e-6:
            self.pause()
            return
        self.pause()                        # いったん止めてから切り替える
        if rate > 0:
            self._play_forward(rate)
        elif rate < 0:
            self._play_backward(-rate)

    # 起動用チャンクの長さ (秒)。本チャンクより長くはしない (resolve6 §3-6 R3)
    def _startup_chunk_sec(self):
        return min(float(self._cfg["audio_startup_chunk_sec"]),
                   float(self._cfg["audio_chunk_sec"]))

    # 前進再生 (通常・倍速とも。速度だけが違う)
    def _play_forward(self, rate):
        if self._player is None:
            self.set_status("音声を再生できない環境のため再生できません")
            return
        total = self._controller.timeline.duration_sec()
        if self._controller.playhead() >= total - 0.05:
            self._controller.set_playhead(0.0)

        self._pending_rate = rate
        start = self._controller.playhead()
        # 手元の音声で「すぐ鳴らせるか」だけを見る。チャンク長ぶんの被覆を求めると
        # 終わり際で毎回作り直しになるため (resolve6 §2.2(b))。続きは終端で継ぐ。
        startup = self._startup_chunk_sec()
        hit = self._audio_source.cached(start, startup)
        if hit is not None:
            self._start_playback(*hit)
            return
        # チャンクが無い → まず短いチャンクを作って鳴らし、続けて本チャンクを先読みする
        self.set_status("音声を準備中…")
        self._request_chunk(start, length_sec=startup, follow_full=True)

    # 倍速逆再生 (音声なし / resolve2 §5.6-3)
    # QMediaPlayer は負の再生レートに対応していないため、QTimer で再生ヘッドを戻す。
    def _play_backward(self, rate):
        interval = int(1000 / max(int(self._cfg["reverse_play_fps"]), 1))
        self._reverse_timer = QTimer(self)
        self._reverse_timer.setInterval(interval)
        self._reverse_timer.timeout.connect(
            lambda: self._step_backward(rate, interval))
        self._reverse_timer.start()
        self._set_rate(-rate)
        self.set_status(f"{rate:g} 倍速逆再生中 (音声なし)")
        _logger.debug("再生速度: %.1f 倍 (逆再生・音声なし)", rate)

    # 1 ティックぶん再生ヘッドを戻す。先頭に達したら停止する。
    def _step_backward(self, rate, interval_ms):
        target = self._controller.playhead() - rate * interval_ms / 1000.0
        if target <= 0.0:
            self._controller.set_playhead(0.0)
            self.pause()
            return
        self._controller.set_playhead(target)

    # 音声チャンクの生成を依頼する
    # autoplay=True    : 生成できたらその場から再生を始める
    # autoplay=False   : 先読み。生成するだけで再生位置は動かさない
    # length_sec       : 生成する長さ (未指定は本チャンク長)
    # follow_full=True : 完了後に同じ位置から本チャンクを先読みする (起動用チャンク)
    def _request_chunk(self, start_sec, autoplay=True, length_sec=None,
                       follow_full=False):
        self._audio_job += 1
        if autoplay:
            self._autoplay_job = self._audio_job
        if follow_full:
            self._follow_full_job = self._audio_job
        self._audio_worker = _AudioWorker(
            self._audio_source, self._audio_job, start_sec,
            self._cfg["audio_chunk_sec"] if length_sec is None else length_sec,
            parent=self)
        self._audio_worker.chunk_ready.connect(self._on_chunk_ready)
        self._audio_worker.chunk_failed.connect(self._on_chunk_failed)
        self._audio_worker.start()

    def _on_chunk_ready(self, job_id, path, chunk_start):
        if job_id != self._audio_job:
            return
        self.set_status("")
        follow_full = job_id == self._follow_full_job
        # 先読みで作っただけのチャンクでは再生位置を動かさない
        # (動かすと再生が数秒先へ飛んでしまう)
        if job_id == self._autoplay_job:
            # 生成待ちの間に停止された場合は再生を始めない
            if not self._pending_rate:
                return
            self._start_playback(path, chunk_start)
        # 起動用の短いチャンクで鳴らし始めたら、続きを本チャンクとして先読みしておく
        if follow_full and self.is_playing():
            self._request_chunk(chunk_start, autoplay=False)

    def _on_chunk_failed(self, job_id):
        if job_id != self._audio_job:
            return
        if job_id != self._autoplay_job:
            _logger.debug("音声チャンクの先読みに失敗しました (再生は継続します)")
            return
        if not self._pending_rate:
            return
        self.set_status("音声を再生できません (無音で再生します)")
        self._start_silent_playback()

    def _start_playback(self, path, chunk_start):
        rate = self._pending_rate or 1.0
        self._chunk_start = float(chunk_start)
        offset_ms = int(max(self._controller.playhead() - self._chunk_start, 0.0) * 1000)
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.setPosition(offset_ms)
        self._player.setPlaybackRate(rate)   # 倍速再生はこの 1 行が本体 (resolve2 §3-6-1)
        self._player.play()
        self._set_rate(rate)
        if abs(rate - 1.0) > 1e-6:
            _logger.debug("再生速度: %.1f 倍 (前進)", rate)

    # 音声が使えないときはタイマーで再生ヘッドだけを進める (§6.4-5 利用不可時)
    def _start_silent_playback(self):
        rate = self._pending_rate or 1.0
        self._silent_timer = QTimer(self)
        interval = int(1000 / max(int(self._cfg["play_fps"]), 1))
        self._silent_timer.setInterval(interval)
        self._silent_timer.timeout.connect(
            lambda: self._controller.set_playhead(
                self._controller.playhead() + rate * interval / 1000.0))
        self._silent_timer.start()
        self._set_rate(rate)

    def pause(self):
        if self._player is not None:
            self._player.pause()
        for name in ("_silent_timer", "_reverse_timer"):
            timer = getattr(self, name, None)
            if timer is not None:
                timer.stop()
                setattr(self, name, None)
        self._pending_rate = 0.0
        self._set_rate(0.0)

    # 速度を切り替え、ボタン表示と playing_changed を更新する
    def _set_rate(self, rate):
        rate = float(rate)
        if abs(self._rate - rate) < 1e-6:
            return
        was_playing = self.is_playing()
        self._rate = rate
        playing = self.is_playing()
        self.play_button.setText("⏸" if abs(rate - 1.0) < 1e-6 else "▶")
        self.play_button.setChecked(abs(rate - 1.0) < 1e-6)
        self.forward_button.setChecked(rate > 1.0 + 1e-6)
        self.backward_button.setChecked(rate < -1e-6)
        if not playing:
            self.set_status("")
        if playing != was_playing:
            # 再生中だけ画質を落とす (resolve6 §3-6 R6/R7)
            self._frame_source.set_playing(playing)
            if not playing:
                # 止めた瞬間に原寸で取り直し、鮮明な絵へ戻す
                self._request_frame()
                self._rebuild_overlays()
            # 再生中は大きさ変更ハンドルを掴ませない (resolve7 §5.4)
            for item in self._overlay_items.values():
                if hasattr(item, "set_resizable"):
                    item.set_resizable(not playing)
            self.playing_changed.emit(playing)

    # 画面を開いた直後に先頭の音声を先読みしておく (アーカイブ用に効く / resolve6 §5.9)
    # 再生中・生成済みのときは何もしない。
    def prefetch_initial(self):
        if not self._cfg.get("audio_prefetch_on_open", True):
            return
        if self._player is None or self.is_playing():
            return
        start = self._controller.playhead()
        if self._audio_source.cached(start, self._startup_chunk_sec()) is not None:
            return
        if self._audio_worker is not None and self._audio_worker.isRunning():
            return
        self._request_chunk(start, autoplay=False,
                            length_sec=self._startup_chunk_sec())

    # 音声位置がマスタークロック。ここから再生ヘッドを進める。
    def _on_audio_position(self, position_ms):
        if self._rate <= 0:
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
        # 先読み: 生成だけ済ませておき、現在のチャンクが終わってから使う
        self._request_chunk(next_start, autoplay=False)

    def _on_audio_status(self, status):
        if self._rate <= 0 or self._player is None:
            return
        if status == QMediaPlayer.EndOfMedia:
            # チャンクの終端 → 同じ速度のまま次のチャンクから続ける
            next_start = self._chunk_start + self._player.duration() / 1000.0
            if next_start >= self._controller.timeline.duration_sec() - 0.05:
                self.pause()
                return
            rate = self._rate
            self._controller.set_playhead(next_start)
            self._pending_rate = rate
            # 先読み済みの本チャンクへ継ぐ (被覆判定は起動用の長さで足りる / resolve6 §5.6)
            hit = self._audio_source.cached(next_start, self._startup_chunk_sec())
            if hit is not None:
                self._start_playback(*hit)
            else:
                self._request_chunk(next_start, length_sec=self._startup_chunk_sec(),
                                    follow_full=True)

    def _on_audio_error(self, error, error_string):
        if _MULTIMEDIA_AVAILABLE and error == QMediaPlayer.NoError:
            return
        _logger.warning("プレビュー音声の再生エラー: %s", error_string)
        # スクラブ中は「再生」ではないため、失敗しても無音タイマー再生を始めない
        # (再生ヘッドが勝手に進んでしまう / resolve7 §2.5.2)
        if self._controller.is_scrubbing():
            self.set_status("この位置の音声を再生できません")
            return
        self.set_status("音声を再生できません (無音で再生します)")
        self._start_silent_playback()

    def _toggle_mute(self):
        if self._audio_output is None:
            return
        muted = not self._audio_output.isMuted()
        self._audio_output.setMuted(muted)
        self.mute_button.setText("🔇" if muted else "🔊")

    # ------------------------------------------------------------------
    # スクラブ (再生ヘッドドラッグ中の音声 / resolve7 §3-7・§5.13)
    # ------------------------------------------------------------------
    # 一定間隔で「今の再生ヘッド位置」へ合わせ直し、その場から等倍で鳴らす。
    # 動かした量だけ音が進むため、ゆっくり動かせばゆっくり、速く動かせば速く進む。
    # 音程は変わらず、逆方向へ動かしても「その位置の音」が鳴る。

    # 1 粒ぶんに必要な音の長さ (秒)。この範囲が手元に無ければ鳴らさない。
    def _grain_sec(self):
        return max(float(self._cfg["scrub_interval_ms"]) / 1000.0 * 2.0, 0.2)

    # 掴んだ / 離した (TimelineController.scrub_changed)
    def _on_scrub_changed(self, active):
        if not self._cfg["scrub_audio_enabled"] or self._player is None:
            return
        if active:
            self.pause()                   # 再生中に掴んだら再生は止める (機器は 1 台)
            self._scrub_last_sec = None
            self._scrub_prev_sec = None
            self._scrub_idle_ms = 0
            self._scrub_source = None
            if self._audio_output is not None:
                self._scrub_volume_backup = self._audio_output.volume()
                self._audio_output.setVolume(
                    self._scrub_volume_backup * float(self._cfg["scrub_volume"]))
            # ドラッグ中は再生中と同じ軽い取得にする (縮小・前進デコード)
            self._frame_source.set_playing(bool(self._cfg["scrub_low_quality_frames"]))
            self._scrub_timer.start(int(self._cfg["scrub_interval_ms"]))
            self._scrub_prepare()          # 手元に無ければ短いチャンクを 1 本要求する
            return
        self._scrub_timer.stop()
        self._player.pause()
        if self._audio_output is not None and self._scrub_volume_backup is not None:
            self._audio_output.setVolume(self._scrub_volume_backup)
        self._scrub_volume_backup = None
        self._scrub_source = None
        self.set_status("")
        self._frame_source.set_playing(False)
        self._request_frame()              # 離した瞬間に原寸で取り直す (停止時と同じ扱い)

    # 1 粒ぶんの処理 (既定 60ms ごと)
    def _scrub_tick(self):
        # 保険: 画面外で離した等で mouseReleaseEvent が来なかった場合に自分で終わる
        if not (QApplication.mouseButtons() & Qt.LeftButton):
            self._controller.end_scrub()
            return

        sec = self._controller.playhead()
        moved = (self._scrub_last_sec is None
                 or abs(sec - self._scrub_last_sec)
                 >= float(self._cfg["scrub_min_delta_sec"]))
        if not moved:
            # 掴んだまま止めている間は黙る (同じ 60ms をループさせない)
            self._scrub_idle_ms += int(self._cfg["scrub_interval_ms"])
            if self._scrub_idle_ms >= int(self._cfg["scrub_hold_ms"]):
                self._player.pause()
            return
        self._scrub_idle_ms = 0
        self._scrub_last_sec = sec

        hit = self._audio_source.cached(sec, self._grain_sec())
        if hit is None:
            # 手元に無い区間 → 嘘の位置を鳴らさず黙って用意する
            self._player.pause()
            self._scrub_prepare()
            return
        path, chunk_start = hit
        if path != self._scrub_source:     # チャンクが替わったときだけ差し替える
            self._player.setSource(QUrl.fromLocalFile(path))
            self._scrub_source = path
            self._chunk_start = float(chunk_start)
        self._player.setPosition(int(max(sec - self._chunk_start, 0.0) * 1000))
        self._player.setPlaybackRate(self._scrub_rate(sec))
        if self._player.playbackState() != QMediaPlayer.PlayingState:
            self._player.play()

    # 粒の再生速度。既定 "grain" は等倍 (音程が変わらない)。
    # "match" はドラッグ速度へ合わせる (音程が変わる / 環境により無音になり得る)。
    def _scrub_rate(self, sec):
        if str(self._cfg["scrub_rate_mode"]) != "match":
            self._scrub_prev_sec = sec
            return 1.0
        previous = self._scrub_prev_sec
        self._scrub_prev_sec = sec
        if previous is None:
            return 1.0
        speed = abs(sec - previous) / (float(self._cfg["scrub_interval_ms"]) / 1000.0)
        return min(max(speed, 0.5), 2.0)   # 環境依存を避けるため範囲を絞る

    # スクラブ用に短いチャンクを 1 本だけ用意する (同時要求は 1 本まで / §3-8)
    def _scrub_prepare(self):
        if self._audio_worker is not None and self._audio_worker.isRunning():
            return
        self.set_status("音声を準備中…")
        self._request_chunk(self._controller.playhead(), autoplay=False,
                            length_sec=float(self._cfg["scrub_chunk_sec"]))

    # ------------------------------------------------------------------
    # 高精度プレビュー (§6.4-4)
    # ------------------------------------------------------------------

    # 現在のフレームを本番と同じ経路 (ASS + ffmpeg) で描き直して別窓に出す
    def _show_high_quality(self):
        resolved = self._controller.source_at_playhead()
        if resolved is None:
            self.set_status("この位置には映像がありません")
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
            video_width=self._canvas[0], video_height=self._canvas[1],
            # コメントの背景 (角丸の箱) を本番と同じ経路で出す (ver3 resolve11 §5.6-5)
            subtitle_cfg=self._eff_cfg)

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
        # コメントアイコンを重ねる (ver3 resolve11 §5.6-5)。
        # 現在フレームの 1 枚絵なので表示時間 (enable) は付けない。
        icon_chains, _count, _groups = comment_decor.build_icon_chains(
            items, self._eff_cfg, self._canvas[0], self._canvas[1],
            in_label="[vsub]", out_label="", with_enable=False)
        if icon_chains:
            chain = f"{chain}[vsub];" + ";".join(icon_chains)
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
            self.set_status(f"高精度プレビューに失敗しました: {e}")
            return
        if result.returncode != 0 or not os.path.exists(out_path):
            self.set_status("高精度プレビューに失敗しました")
            return
        _HighQualityDialog(out_path, self).exec()

    # ------------------------------------------------------------------
    # 後片付け
    # ------------------------------------------------------------------

    # 画面クローズ時に必ず呼ぶ (再生停止・スレッド終了・一時ファイル削除)
    def shutdown(self):
        self._scrub_timer.stop()
        self._controller.end_scrub()
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
        # 本番と同じ描画の確認窓のため、画像そのものには手を加えない (resolve3 §5.6)
        theme.install_window_background(self)
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
        theme.mark_note(note)
        layout.addWidget(note)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)
