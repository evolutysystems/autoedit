# ぼかし指定画面 (ver5 resolve8 §5.10)
#
# 選んだ**クリップ 1 本**だけを対象に、
#   ・キャンバスをドラッグして囲む
#   ・1 件目は「ボカす / ボカさない」を聞く (ボカさない = 画面全体をぼかして穴を開ける)
#   ・囲んだ場所は自動で追いかける
#   ・← → でコマ送りし、ずれていたら掴んで動かす・角で大きさを変える
#     = その時刻にキーフレームが打たれ、前後だけ追い直す
# を行う。指定は並び順のまま重なり、**後から足したものが上**になる (§3.2)。
#
# 対象クリップは開いたときに決まり、**画面の中で入れ替わらない** (要望⑪)。
# 指定の変更はすべて commands.Command として積むため、Timeline 編集画面の
# 「元に戻す」からぼかし指定も戻せる。この画面の中でも Ctrl+Z / Ctrl+Y が効く。
import numpy as np
from PySide6.QtCore import QCoreApplication, QThread, Qt, Signal
from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ...blur import config as blur_config_module
from ...blur import contour, store
from ...blur import decisions as blur_decisions
from ...blur import preview as blur_preview
from ...blur.config import config as blur_config
from ...blur.plan import STATUS_FAILED, STATUS_LOST
from ...timeline import commands
from ...timeline.frame_source import create_frame_source
from ...timeline.model import SubtitleClip
from ...utils.logger import get_logger
from .. import theme
from .blur_canvas import BlurCanvas

_logger = get_logger(__name__)

# 外した範囲を塗る色と濃さ (表示「ボカさない範囲 (緑)」)
_KEEP_RGB = (60, 200, 110)
_KEEP_ALPHA = 0.45


# 囲みの追従を背後で走らせるワーカー (ver5 resolve8 §5.10.7)
#
# 指紋の合わない区切りだけを追い直す。囲みを足した直後・キーフレームを打った直後に走る。
class BlurTrackWorker(QThread):

    # 0.0〜1.0
    progress = Signal(float)
    # 追い直した区切りの数 (失敗したら -1)
    finished_tracking = Signal(int)

    def __init__(self, timeline, settings, tracks, decisions, cfg, parent=None):
        super().__init__(parent)
        self._timeline = timeline
        self._settings = settings
        self._tracks = tracks
        self._decisions = decisions
        self._cfg = cfg
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from ...blur import tracker                   # noqa: PLC0415 (機能 OFF なら読まない)

        try:
            count = tracker.ensure_tracks(
                self._timeline, self._settings, self._tracks, self._decisions, self._cfg,
                on_progress=lambda ratio: self.progress.emit(float(ratio)),
                cancel=lambda: self._cancelled)
        except Exception as error:                    # noqa: BLE001 (画面を落とさない / §4-5)
            _logger.exception("ぼかしの追従に失敗しました: %s", error)
            count = -1
        self.finished_tracking.emit(int(count))


class BlurSpecDialog(QDialog):

    #   controller : TimelineController (コマンドを積むため)
    #   tracks     : 既にある追従結果 (無ければ None)
    #   cache_path : 追従結果の置き場
    #   clip       : 対象のベースクリップ (必須。呼び出し元が選択中のものを渡す)
    def __init__(self, controller, tracks=None, parent=None, cache_path=None,
                 settings=None, clip=None):
        super().__init__(parent)
        self.setWindowTitle("ぼかし")
        self._controller = controller
        self._cache_path = cache_path
        self._timeline = controller.timeline
        self._settings = settings if settings is not None else controller.settings
        self._cfg = blur_config(self._settings)
        self._tracks = tracks
        self._clip = clip
        self._worker = None
        self._notified_migration = False
        self._selected = ""          # 選択中の指定 ID
        self._selected_key = None    # 選択中のキーフレームの時刻
        self._plan = None
        self._overlay_cache = {}     # クリップ ID → 絵 (動画を何度もデコードしない)

        # コマ送りの単位 (§3.7)
        self._fps = int(self._timeline.fps or 60)
        self._frames = self._clip_frames()
        self._index = 0
        # 同じキーフレームとみなす時刻の差 = 半コマ
        self._epsilon = 0.5 / max(self._fps, 1)

        # 書き出しと同じマスク・同じ効果でフレームをぼかす道具
        self._preview = blur_preview.BlurPreview(
            self._timeline, self._tracks, self._decisions(), self._cfg)
        self._preview_mode = str(self._cfg["editor"]["preview_mode"])
        if not blur_preview.is_available() and self._preview_mode == blur_config_module.PREVIEW_BLUR:
            self._preview_mode = blur_config_module.PREVIEW_KEEP
        theme.install_window_background(self)

        self._frame_source = create_frame_source(self._settings, preview_cfg=self._preview_cfg())
        self.resize(1180, 820)
        self._build_ui()
        self._install_shortcuts()
        self._warn_if_migrated()
        self._refresh()

    # 追従結果 (呼び出し元が引き取る)
    def tracks(self):
        return self._tracks

    # 対象クリップのコマ数
    def _clip_frames(self):
        if self._clip is None:
            return 1
        length = float(self._clip.source_out) - float(self._clip.source_in)
        return max(int(round(length * self._fps)), 1)

    # 指定画面だけフレームキャッシュを広げる (コマ送りの後退が速くなる / §5.12.2)
    def _preview_cfg(self):
        base = ((self._settings or {}).get("timeline", {}) or {}).get("preview", {}) or {}
        return dict(base, cache_frames=int(self._cfg["editor"]["frame_cache"]))

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)

        upper = QHBoxLayout()
        self.canvas = BlurCanvas(self._timeline.width, self._timeline.height, parent=self)
        self.canvas.set_metrics(self._cfg["editor"]["handle_px"],
                                self._cfg["editor"]["min_size_ratio"])
        self.canvas.set_overlay_config(*self._overlay_config())
        self.canvas.area_drawn.connect(self._on_area_drawn)
        self.canvas.rect_committed.connect(self._on_rect_committed)
        self.canvas.spec_selected.connect(self._on_spec_selected)
        self.canvas.spec_activated.connect(self._on_spec_activated)
        self.canvas.context_requested.connect(self._on_context_requested)
        self.canvas.delete_requested.connect(self._delete_selected)
        self.canvas.navigate_requested.connect(self._navigate)
        upper.addWidget(self.canvas, 4)

        side = QVBoxLayout()

        # 指定の一覧 (重ね順)
        side.addWidget(QLabel("指定 (下にあるものが優先)"))
        self.spec_list = QListWidget()
        self.spec_list.itemClicked.connect(self._on_spec_clicked)
        self.spec_list.setToolTip(
            "上から順に重なります。下にあるものほど優先されます。\n"
            "「画面全体をぼかす」の上に「ボカさない」を置くと、そこだけ素で見えます。")
        side.addWidget(self.spec_list, 3)

        spec_row = QHBoxLayout()
        self.spec_up_button = QPushButton("上へ")
        self.spec_down_button = QPushButton("下へ")
        self.spec_rename_button = QPushButton("名前")
        self.spec_delete_button = QPushButton("削除")
        for button in (self.spec_up_button, self.spec_down_button,
                       self.spec_rename_button, self.spec_delete_button):
            button.setAutoDefault(False)
            spec_row.addWidget(button)
        self.spec_up_button.clicked.connect(lambda: self._move_spec(-1))
        self.spec_down_button.clicked.connect(lambda: self._move_spec(+1))
        self.spec_rename_button.clicked.connect(self._rename_selected)
        self.spec_delete_button.clicked.connect(self._delete_selected)
        side.addLayout(spec_row)

        # キーフレームの一覧
        self.key_label = QLabel("キーフレーム")
        side.addWidget(self.key_label)
        self.key_list = QListWidget()
        self.key_list.itemClicked.connect(self._on_key_clicked)
        self.key_list.setToolTip(
            "クリックでその時刻へ移動します。\n"
            "枠を掴んで動かすと、そのコマにキーフレームが打たれます。")
        side.addWidget(self.key_list, 2)

        key_row = QHBoxLayout()
        self.key_add_button = QPushButton("ここに打つ")
        self.key_delete_button = QPushButton("キーを削除")
        for button in (self.key_add_button, self.key_delete_button):
            button.setAutoDefault(False)
            key_row.addWidget(button)
        self.key_add_button.clicked.connect(self._add_key_here)
        self.key_delete_button.clicked.connect(self._delete_key)
        side.addLayout(key_row)

        container = QWidget()
        container.setLayout(side)
        upper.addWidget(container, 2)
        root.addLayout(upper, 1)

        # スクラバ (1 目盛 = 1 コマ)
        seek_row = QHBoxLayout()
        self.prev_button = QPushButton("◀")
        self.prev_button.setToolTip("1 コマ戻る (←)")
        self.prev_button.clicked.connect(lambda: self._navigate("prev"))
        seek_row.addWidget(self.prev_button)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, max(self._frames - 1, 0))
        self.slider.setSingleStep(1)
        self.slider.setPageStep(int(self._cfg["editor"]["step_frames"]))
        self.slider.valueChanged.connect(self._on_slider_changed)
        seek_row.addWidget(self.slider, 1)
        self.next_button = QPushButton("▶")
        self.next_button.setToolTip("1 コマ進む (→)")
        self.next_button.clicked.connect(lambda: self._navigate("next"))
        seek_row.addWidget(self.next_button)
        for button in (self.prev_button, self.next_button):
            button.setAutoDefault(False)
        self.time_label = QLabel("")
        seek_row.addWidget(self.time_label)
        root.addLayout(seek_row)

        # 対象クリップと「これから」の指定
        clip_row = QHBoxLayout()
        self.clip_label = QLabel("対象: -")
        clip_row.addWidget(self.clip_label, 1)
        clip_row.addWidget(QLabel("これから:"))
        self._mode_group = QButtonGroup(self)
        self.blur_radio = QRadioButton("ボカす")
        self.keep_radio = QRadioButton("ボカさない")
        self.blur_radio.setChecked(True)
        self.blur_radio.setToolTip("囲んだ場所をぼかします。")
        self.keep_radio.setToolTip(
            "画面全体をぼかし、囲んだ場所だけを素で見せます。\n"
            "画面全体のぼかしが無ければ一緒に足します。")
        for radio in (self.blur_radio, self.keep_radio):
            self._mode_group.addButton(radio)
            clip_row.addWidget(radio)
        clip_row.addSpacing(16)
        clip_row.addWidget(QLabel("表示:"))
        self.preview_combo = QComboBox()
        self.preview_combo.addItem("実際のぼかし", blur_config_module.PREVIEW_BLUR)
        self.preview_combo.addItem("ボカさない範囲 (緑)", blur_config_module.PREVIEW_KEEP)
        self.preview_combo.addItem("枠だけ", blur_config_module.PREVIEW_NONE)
        self.preview_combo.setToolTip(
            "実際のぼかし … 書き出しと同じ計算で、本当にぼけた絵を表示します。\n"
            "ボカさない範囲 (緑) … ぼかしを外した範囲を緑で塗ります。\n"
            "枠だけ … 何も重ねません (コマ送りが一番速い)。")
        if not blur_preview.is_available():
            self.preview_combo.model().item(0).setEnabled(False)
        self.preview_combo.setCurrentIndex(
            max(self.preview_combo.findData(self._preview_mode), 0))
        self.preview_combo.currentIndexChanged.connect(self._on_preview_mode_changed)
        clip_row.addWidget(self.preview_combo)
        self.overlay_check = QCheckBox("画像・字幕も表示")
        self.overlay_check.setToolTip(
            "その時刻に重なる画像・動画・字幕を、出力と同じ重ね順で表示します。\n"
            "書き出しではぼかしが先に掛かるため、これらはぼけません。")
        self.overlay_check.setChecked(bool(self._cfg["editor"]["show_overlays"]))
        self.overlay_check.toggled.connect(lambda _c: self._seek(self._index))
        clip_row.addWidget(self.overlay_check)
        root.addLayout(clip_row)

        # 選択中の指定への操作
        selection_row = QHBoxLayout()
        self.selection_label = QLabel("選択中: なし")
        selection_row.addWidget(self.selection_label, 1)
        self.warning_label = QLabel("")
        selection_row.addWidget(self.warning_label)
        self.goto_lost_button = QPushButton("ここへ移動")
        self.follow_button = QPushButton("動かさない")
        self.retrack_button = QPushButton("追う")
        for button in (self.goto_lost_button, self.follow_button, self.retrack_button):
            button.setAutoDefault(False)
            selection_row.addWidget(button)
        self.goto_lost_button.clicked.connect(self._goto_lost)
        self.follow_button.clicked.connect(self._toggle_follow)
        self.retrack_button.setToolTip("囲んだ場所をもう一度追いかけます。")
        self.retrack_button.clicked.connect(lambda: self._retrack(force=True))
        root.addLayout(selection_row)

        bottom_row = QHBoxLayout()
        self.summary_label = QLabel("")
        bottom_row.addWidget(self.summary_label, 1)
        close_button = QPushButton("閉じる")
        close_button.setAutoDefault(False)
        close_button.clicked.connect(self.accept)
        theme.mark_primary(close_button)
        bottom_row.addWidget(close_button)
        root.addLayout(bottom_row)

        self.status_label = QLabel(
            "ぼかしたい場所・見せたい場所をドラッグで囲んでください。"
            "枠は掴んで移動、角で大きさ、← → でコマ送りです。")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

    # 字幕・画像オーバーレイを Timeline 編集画面と同じ見た目で描くための設定
    def _overlay_config(self):
        from ...modules import subtitle_generator       # noqa: PLC0415 (重い import を遅らせる)

        settings = self._settings
        eff_cfg = subtitle_generator.build_effective_subtitle_cfg(
            settings.get("subtitle", {}), settings.get("vertical", {}),
            {"is_portrait": self._timeline.orientation == "portrait",
             "width": self._timeline.width, "height": self._timeline.height},
        )
        overlay_cfg = {}
        try:
            overlay_cfg = self._controller.cfg["overlay"]
        except (KeyError, TypeError):
            pass
        return eff_cfg, subtitle_generator.build_font_profile(eff_cfg), overlay_cfg

    # Ctrl+Z / Ctrl+Y でこの画面から Undo / Redo する
    def _install_shortcuts(self):
        QShortcut(QKeySequence.Undo, self).activated.connect(self._undo)
        QShortcut(QKeySequence.Redo, self).activated.connect(self._redo)

    def _undo(self):
        if self._controller.can_undo():
            self._controller.undo()
            self._refresh()

    def _redo(self):
        if self._controller.can_redo():
            self._controller.redo()
            self._refresh()

    # 方向キーはキャンバスにフォーカスが無くても効かせる (§5.10.2)
    def keyPressEvent(self, event):     # noqa: N802 (Qt の命名に合わせる)
        key = event.key()
        modifiers = event.modifiers()
        if key in (Qt.Key_Left, Qt.Key_Right):
            forward = key == Qt.Key_Right
            if modifiers & Qt.ControlModifier:
                self._navigate("next_key" if forward else "prev_key")
            elif modifiers & Qt.ShiftModifier:
                self._navigate("next_fast" if forward else "prev_fast")
            else:
                self._navigate("next" if forward else "prev")
            return
        if key == Qt.Key_Home:
            self._navigate("home")
            return
        if key == Qt.Key_End:
            self._navigate("end")
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # 時刻とコマ送り (§3.7 / §5.10.2)
    # ------------------------------------------------------------------

    # いまのコマの素材時刻
    def _source_sec(self, index=None):
        index = self._index if index is None else index
        return float(self._clip.source_in) + int(index) / float(self._fps)

    # いまのコマの Timeline 時刻 (オーバーレイの表示に使う)
    def _timeline_sec(self):
        return float(self._clip.timeline_start) + self._index / float(self._fps)

    # 素材時刻をコマ番号へ (クリップの中へ丸める)
    def _index_of(self, source_sec):
        index = int(round((float(source_sec) - float(self._clip.source_in)) * self._fps))
        return max(min(index, self._frames - 1), 0)

    def _navigate(self, action):
        step = int(self._cfg["editor"]["step_frames"])
        if action == "prev":
            self._seek(self._index - 1)
        elif action == "next":
            self._seek(self._index + 1)
        elif action == "prev_fast":
            self._seek(self._index - step)
        elif action == "next_fast":
            self._seek(self._index + step)
        elif action == "home":
            self._seek(0)
        elif action == "end":
            self._seek(self._frames - 1)
        elif action in ("prev_key", "next_key"):
            self._seek_to_key(forward=action == "next_key")

    # 選択中の指定の前後のキーフレームへ飛ぶ
    def _seek_to_key(self, forward):
        spec = self._spec_by_id(self._selected)
        if spec is None:
            return
        times = [self._index_of(key["t"]) for key in blur_decisions.keys_of(spec)]
        candidates = [i for i in times if (i > self._index if forward else i < self._index)]
        if not candidates:
            return
        self._seek(min(candidates) if forward else max(candidates))

    def _on_slider_changed(self, value):
        self._seek(int(value))

    # ------------------------------------------------------------------
    # 表示の更新
    # ------------------------------------------------------------------

    def _decisions(self):
        return blur_decisions.load(self._timeline)

    # 旧書式から読み替えたときに 1 度だけ知らせる (§3.9)
    def _warn_if_migrated(self):
        decisions = self._decisions()
        if self._notified_migration or not decisions.get("migrated"):
            return
        self._notified_migration = True
        dropped = int(decisions.get("migrated_dropped") or 0)
        kept = int(decisions.get("migrated_kept") or 0)
        if not dropped:
            return
        QMessageBox.information(
            self, "ぼかしの指定",
            f"以前のぼかしの指定のうち、人物の枠を選んで指定したもの ({dropped} 件) は\n"
            "新しい方式へ移せませんでした。\n\n"
            "ぼかしたい場所・見せたい場所をマウスで囲み直してください。\n"
            f"手で囲んで足した枠と全面のぼかし ({kept} 件) は引き継いでいます。")

    # 指定が変わったら呼ぶ: 判定を作り直し、一覧とフレームを描き直す
    def _refresh(self):
        self._preview.set_tracks(self._tracks)
        self._preview.invalidate(self._decisions())
        self._plan = self._preview.plan()
        if self._spec_by_id(self._selected) is None:
            self._selected = ""
            self._selected_key = None
        self._refresh_clip_label()
        self._refresh_lists()
        self._seek(self._index)

    def _refresh_clip_label(self):
        if self._clip is None:
            self.clip_label.setText("対象: クリップが選ばれていません")
            return
        number = blur_decisions.clip_number(self._timeline, self._clip)
        self.clip_label.setText(
            f"対象: クリップ {number} "
            f"({_format_time(self._clip.timeline_start)}〜"
            f"{_format_time(self._clip.timeline_end)})")

    # 現在のコマのフレームと枠を出す
    def _seek(self, index):
        index = max(min(int(index), self._frames - 1), 0)
        self._index = index
        self.slider.blockSignals(True)
        self.slider.setValue(index)
        self.slider.blockSignals(False)
        self.time_label.setText(
            f"{_format_time(self._timeline_sec(), frames=True, fps=self._fps)}"
            f"  (コマ {index + 1} / {self._frames})")

        media_id = str(self._clip.media_id)
        source_sec = self._source_sec()
        media = self._timeline.media_by_id(media_id)
        if media is None:
            self.canvas.clear_frame()
        else:
            self._draw_frame(media, media_id, source_sec)

        self.canvas.set_specs(self._canvas_specs(media_id, source_sec), self._selected)
        self._update_overlay(media_id, source_sec)
        self._update_canvas_overlays()
        self._update_selection_controls()

    def _draw_frame(self, media, media_id, source_sec):
        try:
            result = self._frame_source.frame_at(media, source_sec)
        except Exception:                             # noqa: BLE001 (表示失敗で画面を落とさない)
            _logger.exception("フレーム取得に失敗しました")
            result = None
        if result is None:
            self.canvas.clear_frame()
            return
        width, height, data = result
        if self._preview_mode == blur_config_module.PREVIEW_BLUR:
            data = self._preview.apply(data, width, height, media_id, source_sec)
        self.canvas.set_frame(width, height, data)

    # キャンバスへ渡す枠の一覧 (キャンバス座標)
    def _canvas_specs(self, media_id, source_sec):
        entries = []
        for spec in self._plan.specs_in_clip(self._clip):
            is_frame = spec.get("kind") == blur_decisions.KIND_FRAME
            rect = self._plan.canvas_rect_at(spec, source_sec)
            if rect is None:
                continue
            relative = contour.decode(spec.get("outline"))
            polygon = contour.to_absolute(relative, rect) if relative is not None else None
            entries.append({
                "id": str(spec.get("id")),
                "rect": rect,
                "polygon": polygon,
                "mode": spec.get("mode"),
                "label": self._plan.label_of(spec),
                "is_frame": is_frame,
                "at_key": is_frame or blur_decisions.key_at(
                    spec, source_sec, self._epsilon) is not None,
            })
        return entries

    # 「ボカさない範囲 (緑)」: 書き出しと同じ計算の最終マスクを反転して重ねる
    def _update_overlay(self, media_id, source_sec):
        if self._preview_mode != blur_config_module.PREVIEW_KEEP:
            self.canvas.set_overlay(None)
            return
        mask = self._preview.mask(media_id, source_sec)
        self.canvas.set_overlay(_keep_overlay_image(mask))

    def _on_preview_mode_changed(self, _index):
        self._preview_mode = str(self.preview_combo.currentData()
                                 or blur_config_module.PREVIEW_NONE)
        self._seek(self._index)

    # その時刻に重なる画像・動画・字幕をキャンバスへ出す
    def _update_canvas_overlays(self):
        if not self.overlay_check.isChecked():
            self.canvas.set_overlays([])
            return
        try:
            self.canvas.set_overlays(self._overlay_specs())
        except Exception:                             # noqa: BLE001 (表示で画面を落とさない)
            _logger.debug("オーバーレイを作れませんでした", exc_info=True)
            self.canvas.set_overlays([])

    def _overlay_specs(self):
        specs = []
        playhead = self._timeline_sec()
        for element in self._timeline.overlay_elements():
            if not element.contains(playhead):
                continue
            if isinstance(element, SubtitleClip):
                specs.append({"kind": "text", "element": element})
                continue
            pixmap = self._overlay_pixmap(element)
            if pixmap is not None and not pixmap.isNull():
                specs.append({"kind": "image", "element": element, "pixmap": pixmap})
        return specs

    def _overlay_pixmap(self, clip):
        media = self._timeline.media_by_id(clip.media_id)
        if media is None:
            return None
        cached = self._overlay_cache.get(clip.id)
        if cached is not None:
            return cached
        if media.is_image():
            pixmap = QPixmap(media.path)
        else:
            result = self._frame_source.frame_at(media, float(clip.source_in))
            if result is None:
                return None
            width, height, data = result
            pixmap = QPixmap.fromImage(
                QImage(data, width, height, width * 3, QImage.Format_RGB888).copy())
        self._overlay_cache[clip.id] = pixmap
        return pixmap

    # ------------------------------------------------------------------
    # 一覧
    # ------------------------------------------------------------------

    def _refresh_lists(self):
        specs = self._plan.specs_in_clip(self._clip)

        self.spec_list.clear()
        for spec in specs:
            item = QListWidgetItem(self._plan.describe(spec))
            item.setData(Qt.UserRole, str(spec.get("id")))
            status = self._plan.status_of(spec)
            if status["status"] == STATUS_LOST:
                item.setText(item.text()
                             + f"  ⚠ {_format_time(status['lost_sec'], frames=True, fps=self._fps)}"
                               " で見失いました")
            elif status["status"] == STATUS_FAILED:
                item.setText(item.text() + "  ⚠ 追いかけられませんでした")
            elif not self._is_effective(spec):
                item.setToolTip("ぼかしが掛かっていないため効いていません。")
                item.setText(item.text() + "  (効いていません)")
            self.spec_list.addItem(item)
            if str(spec.get("id")) == self._selected:
                item.setSelected(True)
        if not specs:
            self.spec_list.addItem(QListWidgetItem(
                "まだ指定がありません。ドラッグで囲んでください。"))

        self._refresh_keys()

        blur_count = sum(1 for s in specs if s.get("mode") == blur_decisions.BLUR)
        self.summary_label.setText(
            f"指定 {len(specs)} 件 (ボカす {blur_count} / ボカさない {len(specs) - blur_count})")

    def _refresh_keys(self):
        spec = self._spec_by_id(self._selected)
        self.key_list.clear()
        keys = blur_decisions.keys_of(spec) if spec is not None else []
        for key in keys:
            index = self._index_of(key["t"])
            item = QListWidgetItem(
                f"◆ {_format_time(self._clip.timeline_start + index / float(self._fps), frames=True, fps=self._fps)}"
                f"  (コマ {index + 1})")
            item.setData(Qt.UserRole, float(key["t"]))
            self.key_list.addItem(item)
            if self._selected_key is not None and abs(
                    float(key["t"]) - self._selected_key) <= self._epsilon:
                item.setSelected(True)
        self.key_label.setText(f"キーフレーム ({len(keys)})")

    # その指定が実際に効いているか (下にぼかしが無い「ボカさない」指定は効かない)
    def _is_effective(self, spec):
        if spec.get("mode") == blur_decisions.BLUR:
            return True
        span = spec.get("span") or {}
        middle = (float(span.get("start", 0.0)) + float(span.get("end", 0.0))) / 2.0
        return blur_decisions.frame_spec_at(
            self._decisions(), str(spec.get("media_id") or ""), middle) is not None

    def _spec_by_id(self, spec_id):
        if not spec_id or self._plan is None:
            return None
        return self._plan.spec_by_id(spec_id)

    def _update_selection_controls(self):
        spec = self._spec_by_id(self._selected)
        if spec is None:
            self.selection_label.setText("選択中: なし")
            self.warning_label.setText("")
            self.goto_lost_button.setVisible(False)
            self.follow_button.setEnabled(False)
            for button in (self.spec_up_button, self.spec_down_button,
                           self.spec_rename_button, self.spec_delete_button,
                           self.key_add_button, self.key_delete_button):
                button.setEnabled(False)
            return

        is_area = spec.get("kind") == blur_decisions.KIND_AREA
        self.selection_label.setText(f"選択中: {self._plan.describe(spec)}")
        status = self._plan.status_of(spec)
        if status["status"] == STATUS_LOST:
            self.warning_label.setText(
                f"⚠ {_format_time(status['lost_sec'], frames=True, fps=self._fps)} で見失いました")
            self.goto_lost_button.setVisible(True)
        elif status["status"] == STATUS_FAILED:
            self.warning_label.setText("⚠ 追いかけられませんでした")
            self.goto_lost_button.setVisible(False)
        else:
            self.warning_label.setText("")
            self.goto_lost_button.setVisible(False)

        self.spec_up_button.setEnabled(True)
        self.spec_down_button.setEnabled(True)
        self.spec_rename_button.setEnabled(True)
        self.spec_delete_button.setEnabled(True)
        self.key_add_button.setEnabled(is_area)
        self.key_delete_button.setEnabled(
            is_area and len(blur_decisions.keys_of(spec)) > 1 and self._selected_key is not None)
        self.follow_button.setEnabled(is_area)
        following = spec.get("follow", blur_decisions.FOLLOW_TRACK) == blur_decisions.FOLLOW_TRACK
        self.follow_button.setText("動かさない" if following else "追いかける")

    # ------------------------------------------------------------------
    # 囲む (§5.10.3 / §5.10.4)
    # ------------------------------------------------------------------

    # 新しい囲みができた (正規化キャンバス座標)
    def _on_area_drawn(self, rect):
        if self._clip is None:
            return
        first = not self._plan.specs_in_clip(self._clip)
        mode = self._ask_first_mode() if first else self._current_mode()
        if mode is None:
            self.status_label.setText("囲みを取り消しました。")
            self._seek(self._index)
            return

        span = blur_decisions.span_for(self._timeline, self._clip)
        spec = blur_decisions.make_area_spec(
            "", mode, str(self._clip.media_id), span, self._source_sec(), rect)
        if spec is None:
            self.status_label.setText("囲みが小さすぎます。もう少し大きく囲んでください。")
            return

        specs = []
        # 「ボカさない」は全面ぼかしが要る。無ければ一緒に足す (R6-2)
        if mode == blur_decisions.KEEP and self._frame_spec() is None:
            specs.append(blur_decisions.make_frame_spec("", str(self._clip.media_id), span))
        specs.append(spec)
        if not self._controller.execute(commands.AddBlurSpecs(specs)):
            return

        self._selected = self._latest_spec_id()
        self._selected_key = self._source_sec()
        self.status_label.setText(
            "画面全体をぼかし、囲んだ場所を残します。" if mode == blur_decisions.KEEP
            else "囲んだ場所をぼかします。")
        self._refresh()
        self._retrack()

    # 1 件目の囲みで「ボカす / ボカさない」を聞く (要望④)
    def _ask_first_mode(self):
        box = QMessageBox(self)
        box.setWindowTitle("ぼかし")
        box.setText("指定した場所をどうしますか？")
        box.setInformativeText(
            "ボカす … 囲んだ場所だけをぼかします\n"
            "ボカさない … 画面全体をぼかし、囲んだ場所だけを残します")
        blur_button = box.addButton("ボカす", QMessageBox.AcceptRole)
        keep_button = box.addButton("ボカさない", QMessageBox.AcceptRole)
        box.addButton("取り消す", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is blur_button:
            self.blur_radio.setChecked(True)
            return blur_decisions.BLUR
        if box.clickedButton() is keep_button:
            self.keep_radio.setChecked(True)
            return blur_decisions.KEEP
        return None

    def _current_mode(self):
        return blur_decisions.KEEP if self.keep_radio.isChecked() else blur_decisions.BLUR

    # 対象クリップに掛かっている「画面全体をぼかす」指定 (無ければ None)
    def _frame_spec(self):
        middle = (float(self._clip.source_in) + float(self._clip.source_out)) / 2.0
        return blur_decisions.frame_spec_at(self._decisions(), str(self._clip.media_id), middle)

    # いま追加された指定の ID (採番の一番大きいもの)
    def _latest_spec_id(self):
        specs = self._decisions()["specs"]
        if not specs:
            return ""
        return max((str(s.get("id")) for s in specs), key=blur_decisions.spec_number)

    # ------------------------------------------------------------------
    # 動かす・大きさを変える (§5.10.5)
    # ------------------------------------------------------------------

    def _on_rect_committed(self, spec_id, rect):
        spec = self._spec_by_id(spec_id)
        if spec is None or spec.get("kind") != blur_decisions.KIND_AREA:
            return
        source_sec = self._source_sec()
        if self._controller.execute(
                commands.SetBlurKey(spec_id, source_sec, rect, epsilon=self._epsilon)):
            self._selected = str(spec_id)
            self._selected_key = source_sec
            self.status_label.setText(
                f"{_format_time(self._timeline_sec(), frames=True, fps=self._fps)} に"
                "キーフレームを打ちました。")
        self._refresh()
        self._retrack()

    def _add_key_here(self):
        spec = self._spec_by_id(self._selected)
        if spec is None or spec.get("kind") != blur_decisions.KIND_AREA:
            return
        rect = self._plan.rect_at(spec, self._source_sec())
        if rect is None:
            self.status_label.setText("この位置には枠がありません。")
            return
        self._on_rect_committed(self._selected, rect)

    def _delete_key(self):
        spec = self._spec_by_id(self._selected)
        if spec is None or self._selected_key is None:
            return
        if len(blur_decisions.keys_of(spec)) <= 1:
            QMessageBox.information(
                self, "キーフレーム",
                "キーフレームが 1 つだけのため消せません。\n"
                "指定そのものを消す場合は「削除」を押してください。")
            return
        if self._controller.execute(
                commands.RemoveBlurKey(self._selected, self._selected_key,
                                       epsilon=self._epsilon)):
            self.status_label.setText("キーフレームを消しました。")
        self._selected_key = None
        self._refresh()
        self._retrack()

    def _on_key_clicked(self, item):
        value = item.data(Qt.UserRole)
        if value is None:
            return
        self._selected_key = float(value)
        self._seek(self._index_of(float(value)))

    # ------------------------------------------------------------------
    # 選択と指定の操作
    # ------------------------------------------------------------------

    def _on_spec_selected(self, spec_id):
        self._selected = str(spec_id or "")
        self._selected_key = None
        self._refresh_lists()
        self._seek(self._index)

    def _on_spec_clicked(self, item):
        spec_id = str(item.data(Qt.UserRole) or "")
        if not spec_id:
            return
        self._on_spec_selected(spec_id)

    # ダブルクリックで「ボカす / ボカさない」を入れ替える
    def _on_spec_activated(self, spec_id):
        spec = self._spec_by_id(spec_id)
        if spec is None or spec.get("kind") != blur_decisions.KIND_AREA:
            return
        mode = (blur_decisions.KEEP if spec.get("mode") == blur_decisions.BLUR
                else blur_decisions.BLUR)
        specs = []
        if mode == blur_decisions.KEEP and self._frame_spec() is None:
            span = blur_decisions.span_for(self._timeline, self._clip)
            specs.append(blur_decisions.make_frame_spec("", str(self._clip.media_id), span))
        if specs:
            self._controller.execute(commands.AddBlurSpecs(specs))
        if self._controller.execute(commands.SetBlurSpecMode(spec_id, mode)):
            self.status_label.setText(
                "この囲みを残します (画面全体をぼかします)。" if mode == blur_decisions.KEEP
                else "この囲みをぼかします。")
        self._selected = str(spec_id)
        self._refresh()

    def _on_context_requested(self, spec_id, global_pos):
        self._on_spec_selected(spec_id)
        spec = self._spec_by_id(spec_id)
        if spec is None:
            return
        menu = QMenu(self)
        if spec.get("kind") == blur_decisions.KIND_AREA:
            menu.addAction("ボカす / ボカさない を入れ替える",
                           lambda: self._on_spec_activated(spec_id))
            menu.addAction("ここにキーフレームを打つ", self._add_key_here)
            menu.addAction("名前を変更", self._rename_selected)
            menu.addAction("動かさない" if spec.get("follow", blur_decisions.FOLLOW_TRACK)
                           == blur_decisions.FOLLOW_TRACK else "追いかける",
                           self._toggle_follow)
            menu.addSeparator()
        menu.addAction("削除", self._delete_selected)
        menu.exec(global_pos)

    def _move_spec(self, delta):
        if not self._selected:
            return
        if self._controller.execute(commands.MoveBlurSpec(self._selected, delta)):
            self.status_label.setText("指定の重ね順を変えました。")
        self._refresh()

    def _delete_selected(self):
        if not self._selected:
            return
        if self._controller.execute(commands.RemoveBlurSpecs(self._selected)):
            self.status_label.setText("指定を消しました。")
        self._selected = ""
        self._selected_key = None
        self._refresh()

    def _rename_selected(self):
        from PySide6.QtWidgets import QInputDialog      # noqa: PLC0415 (使うときだけ読む)

        spec = self._spec_by_id(self._selected)
        if spec is None:
            return
        current = self._plan.label_of(spec)
        text, accepted = QInputDialog.getText(self, "指定の名前", "名前:", text=current)
        if not accepted:
            return
        name = blur_decisions.clean_label(text, int(self._cfg["editor"]["name_max_len"]))
        if name == self._plan.default_label(spec):
            name = ""
        if self._controller.execute(commands.RenameBlurSpec(self._selected, name)):
            self.status_label.setText(
                f"「{name}」という名前にしました。" if name else "名前を消しました。")
        self._refresh()

    # 追う ⇔ 動かさない
    def _toggle_follow(self):
        spec = self._spec_by_id(self._selected)
        if spec is None or spec.get("kind") != blur_decisions.KIND_AREA:
            return
        following = spec.get("follow", blur_decisions.FOLLOW_TRACK) == blur_decisions.FOLLOW_TRACK
        follow = blur_decisions.FOLLOW_FIXED if following else blur_decisions.FOLLOW_TRACK
        if self._controller.execute(commands.SetBlurSpecFollow(self._selected, follow)):
            self.status_label.setText(
                "この囲みは動かしません (キーフレームの間は直線で結びます)。" if following
                else "この囲みを追いかけます。")
        self._refresh()
        if not following:
            self._retrack()

    # 見失った時刻へ飛ぶ
    def _goto_lost(self):
        spec = self._spec_by_id(self._selected)
        if spec is None:
            return
        lost = self._plan.status_of(spec).get("lost_sec")
        if lost is None:
            return
        self._seek(self._index_of(float(lost)))
        self.status_label.setText(
            "ここから追えていません。枠を掴んで正しい位置へ動かすとキーフレームが打たれます。")

    # ------------------------------------------------------------------
    # 追従 (§5.10.7)
    # ------------------------------------------------------------------

    # 指紋の合わない区切りを追い直す。
    #   force: True なら「追う」ボタンから明示的に呼ばれた
    def _retrack(self, force=False):
        if self._worker is not None:
            return
        decisions = self._decisions()
        if not blur_decisions.needs_tracking(decisions):
            return
        if not force and not self._cfg["track"]["auto_start"]:
            self.status_label.setText("「追う」を押すと囲んだ場所を追いかけます。")
            return
        if not self._models_ready(force):
            return
        self._ensure_tracks_holder(decisions)

        progress = QProgressDialog("囲んだ場所を追いかけています…", "キャンセル", 0, 100, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        worker = BlurTrackWorker(self._timeline, self._settings, self._tracks, decisions,
                                 self._cfg, parent=self)
        self._worker = worker
        worker.progress.connect(lambda ratio: progress.setValue(int(ratio * 100)))
        holder = []
        worker.finished_tracking.connect(holder.append)
        progress.canceled.connect(worker.cancel)
        worker.start(QThread.LowPriority)
        # 追っている間も画面を固まらせない (進捗とキャンセルを拾う)
        while worker.isRunning():
            QCoreApplication.processEvents()
            worker.wait(30)
        progress.close()
        self._worker = None

        count = holder[0] if holder else -1
        if count < 0:
            self.status_label.setText(
                "追いかけられませんでした。枠はキーフレームの位置に置かれます。")
        elif count:
            self.status_label.setText(f"{count} か所を追いかけました。")
        if self._cache_path:
            store.save(self._cache_path, self._tracks)
        self._refresh()

    # 追従結果の入れ物を用意する (無ければ作る / 素材の控えを足す)
    def _ensure_tracks_holder(self, decisions):
        media_ids = blur_decisions.media_ids(decisions)
        if self._tracks is None:
            self._tracks = store.new_tracks(self._timeline, self._cfg, media_ids)
            return
        store.drop_stale_media(self._tracks, self._timeline)
        store.merge_media(self._tracks, self._timeline, media_ids)

    # 検出モデルが使えるか。使えなくても追従は走る (相関だけ) ので True を返す。
    def _models_ready(self, notify):
        from ...blur import models                    # noqa: PLC0415

        available, reason = models.availability(self._cfg)
        if not available:
            self.status_label.setText(reason)
            if notify:
                QMessageBox.information(self, "ぼかし", reason)
        return True

    # ------------------------------------------------------------------
    # 後始末
    # ------------------------------------------------------------------

    # 終了時 (閉じる/Esc/×) にフレーム取得と追従を閉じる。
    # closeEvent は「閉じる」ボタン (accept) では呼ばれないため done で行う。
    def done(self, code):
        worker = self._worker
        if worker is not None:
            worker.cancel()
            worker.wait()
            self._worker = None
        try:
            self._frame_source.close()
        except Exception:               # noqa: BLE001 (閉じる処理で落とさない)
            _logger.debug("フレーム取得の後始末に失敗しました", exc_info=True)
        super().done(code)


# マスク (PIL の L 画像) を「ボカさない範囲」の緑の QImage にする。
# ぼかす所ではなく **ぼかさない所** を塗るため、マスクを反転して使う。
def _keep_overlay_image(mask):
    if mask is None:
        return None
    values = 255 - np.asarray(mask, dtype=np.float32)
    values *= float(_KEEP_ALPHA)
    height, width = values.shape[:2]
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[:, :, 0], rgba[:, :, 1], rgba[:, :, 2] = _KEEP_RGB
    rgba[:, :, 3] = np.clip(values, 0, 255).astype(np.uint8)
    image = QImage(rgba.data, width, height, width * 4, QImage.Format_RGBA8888)
    return image.copy()


# 時刻の表示。frames=True ならコマまで出す (0:02.10)
def _format_time(seconds, frames=False, fps=60):
    total = max(float(seconds or 0.0), 0.0)
    whole = int(total)
    text = f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}"
    if not frames:
        return text
    frame = int(round((total - whole) * max(int(fps), 1)))
    return f"{text}.{frame:02d}"
