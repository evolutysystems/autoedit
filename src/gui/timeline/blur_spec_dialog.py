# ぼかし指定画面 (ver5 resolve2 §5.6 / ver5 resolve3 §5.5)
#
# 解析済みの人物・追加した枠を一覧し、フレームの上で「ぼかす / ぼかさない」を指定する (R5)。
#
# 操作モード (resolve3 §2.4 (c) / §5.5.2):
#   指定する       … 囲んだ人物・枠へ「ぼかす / ぼかさない」を指定する
#   人物・物を追加 … 囲みを新しい枠として足し、その物そのものを追う (manual_tracker)
#   場所を追加     … 囲みを新しい枠として足し、カメラの動きに合わせて追う (region_tracker)
#
# 人物への指定は**人物 ID に対して**行うため、全セクションへ同時に効く (R6)。
# 何をぼかす / 守る / 使わないかは blur.plan が決め、出力 (mask_builder) と同じ結果を表示する
# (resolve3 §4-5)。
#
# 指定の変更はすべて commands.Command として積むため、Timeline 編集画面の
# 「元に戻す」からぼかし指定も戻せる (§5.6.5)。この画面の中でも Ctrl+Z / Ctrl+Y が効く。
import numpy as np
from PySide6.QtCore import QCoreApplication, QThread, Qt, Signal
from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
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

from ...blur import contour, geometry, store
from ...blur import decisions as blur_decisions
from ...blur.config import config as blur_config
from ...blur.plan import KIND_PERSON, ROLE_BLUR, ROLE_KEEP, BlurPlan
from ...timeline import commands
from ...timeline.frame_source import create_frame_source
from ...timeline.timemap import TimeMap
from ...utils.logger import get_logger
from .. import theme
from .blur_canvas import BlurCanvas

_logger = get_logger(__name__)

# 一覧に出す見本の大きさ (px)
_THUMB_PX = 64

# 操作モード
_OP_DESIGNATE = "designate"
_OP_ADD_OBJECT = "object"
_OP_ADD_PLACE = "place"

# 選択の種類
_SEL_TRACK = "track"          # 検出された人物の枠 (tracklet)
_SEL_REGION = "region"        # 追加した枠・場所
_SEL_IDENTITY = "identity"    # 人物 (一覧から選んだ)
_SEL_EXCLUDED = "excluded"    # 削除した枠 (一覧から選んだ)

# 仕上がり表示の色と濃さ
_OVERLAY_RGB = (232, 60, 60)
_OVERLAY_ALPHA = 0.55


# 解析を背後で走らせるワーカー (§5.3.5)
#
# Timeline 画面を開いた直後に始め、終わったらボタンを有効にする (§3.6 案 2)。
# 優先度を下げてプレビュー再生を邪魔しないようにする。
class BlurAnalysisWorker(QThread):

    # (0.0〜1.0, ラベル)
    progress = Signal(float, str)
    # 解析完了 (結果の辞書 / 失敗・中断なら None)
    finished_analysis = Signal(object)

    def __init__(self, timeline, settings, cache_path, parent=None):
        super().__init__(parent)
        self._timeline = timeline
        self._settings = settings
        self._cache_path = cache_path
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from ...blur import analyzer                  # noqa: PLC0415 (機能 OFF なら読まない)

        try:
            result = analyzer.analyze(
                self._timeline, self._settings, self._cache_path,
                on_progress=lambda ratio, _kv: self.progress.emit(float(ratio), ""),
                cancel=lambda: self._cancelled,
            )
        except Exception as error:                    # noqa: BLE001 (画面を落とさない / §5.9)
            _logger.exception("ぼかしの解析に失敗しました: %s", error)
            result = None
        self.finished_analysis.emit(result)


class BlurSpecDialog(QDialog):

    # controller  : TimelineController (コマンドを積むため)
    # analysis    : 解析結果 (store.load / analyzer.analyze の戻り値)
    # cache_path  : 解析結果の置き場 (枠の追従を足したら書き戻す)
    def __init__(self, controller, analysis, parent=None, cache_path=None):
        super().__init__(parent)
        self.setWindowTitle("ぼかし指定")
        self._controller = controller
        self._cache_path = cache_path
        self._timeline = controller.timeline
        self._settings = controller.settings
        self._analysis = analysis or {}
        self._cfg = blur_config(self._settings)
        self._timemap = TimeMap.from_timeline(self._timeline)
        self._playhead = 0.0
        self._selection = None       # (種類, ID)
        self._plan = None
        theme.install_window_background(self)

        self._frame_source = create_frame_source(self._settings)
        self.resize(1180, 800)
        self._build_ui()
        self._install_shortcuts()
        self._refresh()

    # ------------------------------------------------------------------
    # 画面構築 (resolve3 §5.5.1)
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)

        upper = QHBoxLayout()
        self.canvas = BlurCanvas(self._timeline.width, self._timeline.height, parent=self)
        self.canvas.path_drawn.connect(self._on_path_drawn)
        self.canvas.shape_selected.connect(self._on_shape_selected)
        self.canvas.shape_activated.connect(self._on_shape_activated)
        self.canvas.context_requested.connect(self._on_context_requested)
        self.canvas.delete_requested.connect(self._delete_selection)
        upper.addWidget(self.canvas, 4)

        side = QVBoxLayout()
        side.addWidget(QLabel("人物"))
        self.identity_list = QListWidget()
        self.identity_list.setIconSize(QPixmap(_THUMB_PX, _THUMB_PX).size())
        self.identity_list.itemClicked.connect(self._on_identity_clicked)
        self.identity_list.itemDoubleClicked.connect(self._on_identity_double_clicked)
        side.addWidget(self.identity_list, 3)

        self.merge_button = QPushButton("選んだ 2 人を同じ人物にする")
        self.merge_button.setAutoDefault(False)
        self.merge_button.setToolTip(
            "着替えなどで別人物として分かれてしまった 2 人を、Ctrl を押しながら選んで統合します。\n"
            "統合は次に解析し直したときに反映され、解析をやり直しても残ります。")
        self.identity_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.merge_button.clicked.connect(self._merge_identities)
        side.addWidget(self.merge_button)

        side.addWidget(QLabel("追加した枠・場所"))
        self.region_list = QListWidget()
        self.region_list.itemClicked.connect(self._on_region_clicked)
        side.addWidget(self.region_list, 1)

        self.excluded_label = QLabel("削除した枠 (0)")
        side.addWidget(self.excluded_label)
        self.excluded_list = QListWidget()
        self.excluded_list.itemClicked.connect(self._on_excluded_clicked)
        side.addWidget(self.excluded_list, 1)
        excluded_row = QHBoxLayout()
        self.show_excluded_check = QCheckBox("削除した枠も表示")
        self.show_excluded_check.toggled.connect(lambda _checked: self._seek(self._playhead))
        excluded_row.addWidget(self.show_excluded_check)
        self.restore_button = QPushButton("元に戻す")
        self.restore_button.setAutoDefault(False)
        self.restore_button.clicked.connect(self._restore_selection)
        excluded_row.addWidget(self.restore_button)
        side.addLayout(excluded_row)

        container = QWidget()
        container.setLayout(side)
        upper.addWidget(container, 2)
        root.addLayout(upper, 1)

        # スクラバ
        seek_row = QHBoxLayout()
        self.prev_button = QPushButton("◀")
        self.prev_button.setAutoDefault(False)
        self.prev_button.clicked.connect(lambda: self._step(-1.0))
        seek_row.addWidget(self.prev_button)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, max(int(self._timeline.duration_sec() * 10), 1))
        self.slider.valueChanged.connect(lambda v: self._seek(v / 10.0))
        seek_row.addWidget(self.slider, 1)
        self.next_button = QPushButton("▶")
        self.next_button.setAutoDefault(False)
        self.next_button.clicked.connect(lambda: self._step(1.0))
        seek_row.addWidget(self.next_button)
        self.time_label = QLabel("0:00:00")
        seek_row.addWidget(self.time_label)
        root.addLayout(seek_row)

        # 操作モード
        op_row = QHBoxLayout()
        op_row.addWidget(QLabel("操作:"))
        self._op_group = QButtonGroup(self)
        self.designate_radio = QRadioButton("指定する")
        self.designate_radio.setChecked(True)
        self.designate_radio.setToolTip("囲んだ人物・枠へ「ぼかす / ぼかさない」を指定します。")
        self.add_object_radio = QRadioButton("人物・物を追加")
        self.add_object_radio.setToolTip(
            "検出されなかった人物や物を囲んで足します。囲んだ物そのものを追いかけます。")
        self.add_place_radio = QRadioButton("場所を追加")
        self.add_place_radio.setToolTip(
            "建物・看板などを囲んで足します。カメラの動きに合わせて追いかけます。")
        for radio in (self.designate_radio, self.add_object_radio, self.add_place_radio):
            self._op_group.addButton(radio)
            op_row.addWidget(radio)
        self._op_group.buttonToggled.connect(lambda *_args: self._update_hint())
        op_row.addSpacing(24)

        op_row.addWidget(QLabel("指定:"))
        self._mode_group = QButtonGroup(self)
        self.blur_radio = QRadioButton("ぼかす")
        self.blur_radio.setChecked(True)
        self.keep_radio = QRadioButton("ぼかさない")
        self._mode_group.addButton(self.blur_radio)
        self._mode_group.addButton(self.keep_radio)
        op_row.addWidget(self.blur_radio)
        op_row.addWidget(self.keep_radio)
        op_row.addStretch(1)

        self.preview_check = QCheckBox("仕上がり表示")
        self.preview_check.setToolTip(
            "書き出しと同じ計算で、実際にぼける範囲を赤く重ねて表示します。\n"
            "「ぼかさない」と重なった部分が削られるのを確かめられます。")
        self.preview_check.toggled.connect(lambda _checked: self._seek(self._playhead))
        op_row.addWidget(self.preview_check)
        root.addLayout(op_row)

        # 選択中の枠の操作 (resolve3 §5.5.3)
        selection_row = QHBoxLayout()
        self.selection_label = QLabel("選択中: なし")
        selection_row.addWidget(self.selection_label, 1)
        self.sel_blur_button = QPushButton("ぼかす")
        self.sel_keep_button = QPushButton("ぼかさない")
        self.sel_delete_button = QPushButton("枠を削除")
        self.sel_split_button = QPushButton("別の人物にする")
        self.sel_unsplit_button = QPushButton("元の人物へ戻す")
        for button in (self.sel_blur_button, self.sel_keep_button, self.sel_delete_button,
                       self.sel_split_button, self.sel_unsplit_button):
            button.setAutoDefault(False)
            selection_row.addWidget(button)
        self.sel_blur_button.clicked.connect(lambda: self._apply_mode_to_selection(
            blur_decisions.BLUR))
        self.sel_keep_button.clicked.connect(lambda: self._apply_mode_to_selection(
            blur_decisions.KEEP))
        self.sel_delete_button.clicked.connect(self._delete_selection)
        self.sel_split_button.clicked.connect(self._split_selection)
        self.sel_unsplit_button.clicked.connect(self._unsplit_selection)
        root.addLayout(selection_row)

        bottom_row = QHBoxLayout()
        self.policy_label = QLabel("")
        bottom_row.addWidget(self.policy_label, 1)
        self.clear_path_button = QPushButton("囲みを取り消す")
        self.clear_path_button.setAutoDefault(False)
        self.clear_path_button.clicked.connect(self.canvas.clear_path)
        bottom_row.addWidget(self.clear_path_button)
        close_button = QPushButton("閉じる")
        close_button.setAutoDefault(False)
        close_button.clicked.connect(self.accept)
        theme.mark_primary(close_button)
        bottom_row.addWidget(close_button)
        root.addLayout(bottom_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        self._update_hint()

    # Ctrl+Z / Ctrl+Y でこの画面から Undo / Redo する (resolve3 §5.5.2)
    def _install_shortcuts(self):
        undo = QShortcut(QKeySequence.Undo, self)
        undo.activated.connect(self._undo)
        redo = QShortcut(QKeySequence.Redo, self)
        redo.activated.connect(self._redo)

    def _undo(self):
        if self._controller.can_undo():
            self._controller.undo()
            self._refresh()

    def _redo(self):
        if self._controller.can_redo():
            self._controller.redo()
            self._refresh()

    # ------------------------------------------------------------------
    # 表示の更新
    # ------------------------------------------------------------------

    def _decisions(self):
        return blur_decisions.load(self._timeline)

    # 指定が変わったら呼ぶ: 判定を作り直し、一覧とフレームを描き直す
    def _refresh(self):
        self._plan = BlurPlan(self._timeline, self._analysis, self._decisions(), self._cfg)
        if not self._selection_exists():
            self._selection = None
        self._refresh_lists()
        self._seek(self._playhead)
        self._update_selection_controls()

    def _update_hint(self):
        if self.designate_radio.isChecked():
            text = ("フレームの上をドラッグして人物を囲むと「ぼかす / ぼかさない」を指定します。"
                    "枠をクリックで選択、ダブルクリックで切り替え、右クリックでメニューが出ます。")
        elif self.add_object_radio.isChecked():
            text = "足したい人物・物をドラッグして囲んでください。囲んだ物を追いかけます。"
        else:
            text = "足したい場所 (建物・看板など) をドラッグして囲んでください。"
        self.status_label.setText(text)

    # 現在の再生位置のフレームと枠を出す
    def _seek(self, timeline_sec):
        self._playhead = max(float(timeline_sec), 0.0)
        self.time_label.setText(_format_time(self._playhead))

        located = self._timemap.to_source(self._playhead)
        if located is None:
            self.canvas.clear_frame()
            self.canvas.set_shapes([])
            self.canvas.set_overlay(None)
            return

        media_id, source_sec = located
        media = self._timeline.media_by_id(media_id)
        if media is None:
            self.canvas.clear_frame()
            return

        try:
            result = self._frame_source.frame_at(media, source_sec)
        except Exception:                             # noqa: BLE001 (表示失敗で画面を落とさない)
            _logger.exception("フレーム取得に失敗しました")
            result = None
        if result is None:
            self.canvas.clear_frame()
        else:
            width, height, data = result
            self.canvas.set_frame(width, height, data)

        self.canvas.set_shapes(self._canvas_shapes(media_id, source_sec), self._selected_key())
        self._update_overlay()

    # キャンバスへ渡す枠の一覧 (キャンバス座標)
    def _canvas_shapes(self, media_id, source_sec):
        show_excluded = self.show_excluded_check.isChecked() or (
            self._selection is not None and self._selection[0] == _SEL_EXCLUDED)
        shapes = []
        for shape in self._plan.shapes_at(media_id, source_sec, include_unused=True):
            if shape["excluded"] and not show_excluded:
                continue
            relative = shape["silhouette"] if shape["silhouette"] is not None else shape["outline"]
            polygon = contour.to_absolute(relative, shape["rect"]) if relative is not None else None
            shapes.append({
                "key": self._shape_key(shape),
                "rect": shape["rect"],
                "polygon": polygon,
                "role": shape["role"],
                "main": shape["main"],
                "excluded": shape["excluded"],
                "label": shape["label"] + (" (削除済み)" if shape["excluded"] else ""),
                "_shape": shape,
            })
        return shapes

    # 枠のキー: 人物は tracklet ID、追加した枠は領域 ID (1 つの領域が複数トラックを持つため)
    @staticmethod
    def _shape_key(shape):
        if shape["kind"] == KIND_PERSON:
            return f"{_SEL_TRACK}:{shape['key']}"
        return f"{_SEL_REGION}:{shape['identity']}"

    # 今の選択に対応するキャンバスのキー
    def _selected_key(self):
        if self._selection is None:
            return ""
        kind, value = self._selection
        if kind == _SEL_EXCLUDED:
            return f"{_SEL_TRACK}:{value}"
        if kind in (_SEL_TRACK, _SEL_REGION):
            return f"{kind}:{value}"
        return ""

    # 仕上がり表示: 書き出しと同じ計算の最終マスクを重ねる (resolve3 §5.5.4)
    def _update_overlay(self):
        if not self.preview_check.isChecked():
            self.canvas.set_overlay(None)
            return
        from ...blur import mask_builder             # noqa: PLC0415 (使うときだけ読む)

        try:
            mask = mask_builder.frame_mask(self._timeline, self._analysis, self._decisions(),
                                           self._cfg, self._playhead)
        except Exception:                             # noqa: BLE001 (表示の失敗で画面を落とさない)
            _logger.exception("仕上がり表示を作れませんでした")
            mask = None
        self.canvas.set_overlay(_mask_to_image(mask))

    # 人物・追加した枠・削除した枠の一覧を作り直す
    def _refresh_lists(self):
        plan = self._plan
        labels = plan.labels()
        decisions = self._decisions()

        self.identity_list.clear()
        for identity in plan.identities:
            identity_id = identity["id"]
            role = _identity_role(plan, identity_id)
            text = (f"{labels.get(identity_id, identity_id)}"
                    f"{' (分けた人物)' if identity['split'] else ''}\n"
                    f"{_format_time(identity['total_sec'])} / {_role_text(role)}")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, identity_id)
            thumb = store.decode_thumb(identity.get("thumb"))
            if thumb:
                pixmap = QPixmap()
                if pixmap.loadFromData(thumb):
                    item.setIcon(pixmap.scaled(
                        _THUMB_PX, _THUMB_PX, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.identity_list.addItem(item)
            if self._selection == (_SEL_IDENTITY, identity_id):
                item.setSelected(True)

        untracked = {str(r.get("id")) for r in plan.untracked_regions()}
        self.region_list.clear()
        for region in decisions["regions"]:
            region_id = str(region.get("id"))
            kind = ("人物・物" if blur_decisions.region_kind(region) == blur_decisions.KIND_OBJECT
                    else "場所")
            mode = "ぼかす" if blur_decisions.should_blur_region(region) else "ぼかさない"
            fixed = " / 固定" if str(region.get("follow") or "") == "fixed" else ""
            status = " (追従できていません)" if region_id in untracked else ""
            item = QListWidgetItem(
                f"{region.get('label') or region_id} ({kind}{fixed}) / {mode}{status}")
            item.setData(Qt.UserRole, region_id)
            self.region_list.addItem(item)
            if self._selection == (_SEL_REGION, region_id):
                item.setSelected(True)

        excluded = plan.excluded_entries()
        self.excluded_label.setText(f"削除した枠 ({len(excluded)})")
        self.excluded_list.clear()
        for entry in excluded:
            track_id = str(entry["track"].get("id"))
            item = QListWidgetItem(
                f"{labels.get(entry['identity'], entry['identity'])} の枠 "
                f"({_format_time(entry['start'])}〜{_format_time(entry['end'])})")
            item.setData(Qt.UserRole, track_id)
            self.excluded_list.addItem(item)
            if self._selection == (_SEL_EXCLUDED, track_id):
                item.setSelected(True)
        # 削除した枠が 1 つも無い一覧は場所を取るだけなので隠す
        self.excluded_list.setVisible(bool(excluded))
        self.show_excluded_check.setVisible(bool(excluded))
        self.restore_button.setVisible(bool(excluded))

        self.policy_label.setText(blur_decisions.policy_label(decisions, self._cfg))

    def _step(self, seconds):
        self.slider.setValue(max(int((self._playhead + seconds) * 10), 0))

    # ------------------------------------------------------------------
    # 選択 (resolve3 §5.5.2 / §5.5.3)
    # ------------------------------------------------------------------

    def _on_shape_selected(self, key):
        self._selection = _selection_from_key(key)
        if self._selection is not None and self._selection[0] == _SEL_TRACK:
            entry = self._plan.entry_of(self._selection[1])
            if entry is not None and entry["excluded"]:
                self._selection = (_SEL_EXCLUDED, self._selection[1])
        self._refresh_lists()
        self._seek(self._playhead)
        self._update_selection_controls()

    def _on_identity_clicked(self, item):
        self._selection = (_SEL_IDENTITY, str(item.data(Qt.UserRole) or ""))
        self._seek(self._playhead)
        self._update_selection_controls()

    def _on_region_clicked(self, item):
        region_id = str(item.data(Qt.UserRole) or "")
        self._selection = (_SEL_REGION, region_id)
        # 囲んだ時刻へ移動して、どの枠かが見えるようにする
        region = next((r for r in self._decisions()["regions"]
                       if str(r.get("id")) == region_id), None)
        if region is not None:
            self._seek_to_source(str(region.get("media_id") or ""),
                                 float(region.get("anchor_sec") or 0.0))
        self._update_selection_controls()
        self._seek(self._playhead)

    def _on_excluded_clicked(self, item):
        track_id = str(item.data(Qt.UserRole) or "")
        self._selection = (_SEL_EXCLUDED, track_id)
        entry = self._plan.entry_of(track_id)
        if entry is not None:
            self._seek_to_source(str(entry["track"].get("media_id") or ""),
                                 (entry["start"] + entry["end"]) / 2.0)
        self._update_selection_controls()
        self._seek(self._playhead)

    # 素材の時刻に当たる Timeline の位置へ移動する (出力に含まれていなければ動かない)
    def _seek_to_source(self, media_id, source_sec):
        for clip in self._timeline.base_clips():
            if (str(clip.media_id) == media_id
                    and float(clip.source_in) <= source_sec <= float(clip.source_out)):
                sec = float(clip.timeline_start) + (source_sec - float(clip.source_in))
                self.slider.blockSignals(True)
                self.slider.setValue(int(sec * 10))
                self.slider.blockSignals(False)
                self._playhead = sec
                return

    def _selection_exists(self):
        if self._selection is None or self._plan is None:
            return False
        kind, value = self._selection
        if kind in (_SEL_TRACK, _SEL_EXCLUDED):
            entry = self._plan.entry_of(value)
            return entry is not None and (kind == _SEL_TRACK or entry["excluded"])
        if kind == _SEL_REGION:
            return any(str(r.get("id")) == value for r in self._decisions()["regions"])
        if kind == _SEL_IDENTITY:
            return any(i["id"] == value for i in self._plan.identities)
        return False

    # 選択に合わせてボタンの有効・無効と説明を変える
    def _update_selection_controls(self):
        kind, value = self._selection if self._selection is not None else (None, "")
        labels = self._plan.labels()
        entry = self._plan.entry_of(value) if kind in (_SEL_TRACK, _SEL_EXCLUDED) else None
        is_split = bool(entry is not None and self._split_anchor_of(entry["track"]))

        if kind == _SEL_TRACK and entry is not None:
            text = f"{labels.get(entry['identity'], entry['identity'])} の枠 / {_role_text(entry['role'])}"
        elif kind == _SEL_EXCLUDED and entry is not None:
            text = f"{labels.get(entry['identity'], entry['identity'])} の枠 (削除済み)"
        elif kind == _SEL_REGION:
            text = f"{labels.get(value, value)}"
        elif kind == _SEL_IDENTITY:
            text = f"{labels.get(value, value)} (すべての枠)"
        else:
            text = "なし"
        self.selection_label.setText(f"選択中: {text}")

        can_mode = kind in (_SEL_TRACK, _SEL_REGION, _SEL_IDENTITY)
        self.sel_blur_button.setEnabled(can_mode)
        self.sel_keep_button.setEnabled(can_mode)
        self.sel_delete_button.setEnabled(kind in (_SEL_TRACK, _SEL_REGION, _SEL_IDENTITY))
        self.sel_delete_button.setText(
            "この人物をすべて削除" if kind == _SEL_IDENTITY else "枠を削除")
        self.sel_split_button.setEnabled(kind == _SEL_TRACK and not is_split)
        self.sel_unsplit_button.setVisible(is_split and kind == _SEL_TRACK)
        self.restore_button.setEnabled(kind == _SEL_EXCLUDED)

    # ------------------------------------------------------------------
    # 選択への操作
    # ------------------------------------------------------------------

    # 「ぼかす / ぼかさない」を選択へ反映する
    def _apply_mode_to_selection(self, mode):
        if self._selection is None:
            return
        kind, value = self._selection
        if kind == _SEL_REGION:
            if self._controller.execute(commands.SetBlurRegionMode(value, mode)):
                self.status_label.setText(
                    f"{self._plan.labels().get(value, value)} を{_verb(mode)}。")
            self._refresh()
            return
        identity = value if kind == _SEL_IDENTITY else self._plan.identity_of(value)
        if identity:
            self._apply_to_identities([identity], mode)

    def _on_shape_activated(self, key):
        self._on_shape_selected(key)
        if self._selection is None:
            return
        kind, value = self._selection
        if kind == _SEL_REGION:
            region = next((r for r in self._decisions()["regions"]
                           if str(r.get("id")) == value), None)
            current = blur_decisions.should_blur_region(region)
        elif kind == _SEL_TRACK:
            entry = self._plan.entry_of(value)
            current = entry is not None and entry["role"] == ROLE_BLUR
        else:
            return
        # ダブルクリックで「ぼかす / ぼかさない」を入れ替える
        self._apply_mode_to_selection(blur_decisions.KEEP if current else blur_decisions.BLUR)

    def _on_identity_double_clicked(self, item):
        identity_id = str(item.data(Qt.UserRole) or "")
        self._selection = (_SEL_IDENTITY, identity_id)
        role = _identity_role(self._plan, identity_id)
        self._apply_to_identities(
            [identity_id], blur_decisions.KEEP if role == ROLE_BLUR else blur_decisions.BLUR)

    # 右クリックのメニュー (resolve3 §5.5.2)
    def _on_context_requested(self, key, global_pos):
        self._on_shape_selected(key)
        if self._selection is None:
            return
        kind, value = self._selection
        menu = QMenu(self)
        if kind == _SEL_EXCLUDED:
            menu.addAction("元に戻す", self._restore_selection)
        else:
            menu.addAction("ぼかす", lambda: self._apply_mode_to_selection(blur_decisions.BLUR))
            menu.addAction("ぼかさない", lambda: self._apply_mode_to_selection(blur_decisions.KEEP))
            menu.addSeparator()
            menu.addAction("枠を削除", self._delete_selection)
            if kind == _SEL_TRACK:
                entry = self._plan.entry_of(value)
                if entry is not None and self._split_anchor_of(entry["track"]):
                    menu.addAction("元の人物へ戻す", self._unsplit_selection)
                else:
                    menu.addAction("別の人物にする", self._split_selection)
                if entry is not None and entry["identity"]:
                    identity = entry["identity"]
                    menu.addAction("この人物をすべて削除",
                                   lambda: self._delete_identity(identity))
        menu.exec(global_pos)

    # 選択を削除する (Delete キー / ボタン / メニュー)
    def _delete_selection(self):
        if self._selection is None:
            return
        kind, value = self._selection
        if kind == _SEL_REGION:
            label = self._plan.labels().get(value, value)
            if self._controller.execute(commands.RemoveBlurRegion(value)):
                self.status_label.setText(f"{label} を削除しました。")
            self._selection = None
            self._refresh()
        elif kind == _SEL_TRACK:
            entry = self._plan.entry_of(value)
            if entry is not None:
                self._exclude_tracks([entry], f"{self._plan.labels().get(entry['identity'], '')} の枠")
        elif kind == _SEL_IDENTITY:
            self._delete_identity(value)

    # 人物の枠をすべて削除する
    def _delete_identity(self, identity_id):
        entries = [e for e in self._plan.entries
                   if e["kind"] == KIND_PERSON and e["identity"] == identity_id and not e["excluded"]]
        if entries:
            self._exclude_tracks(entries, f"{self._plan.labels().get(identity_id, identity_id)} の枠すべて")

    # 検出枠を除外する。主役が変わるなら確かめる (resolve3 §5.5.3)
    def _exclude_tracks(self, entries, what):
        anchors = [blur_decisions.track_anchor(e["track"]) for e in entries]
        preview = self._decisions()
        for anchor in anchors:
            preview = blur_decisions.with_excluded(preview, anchor)
        if not self._confirm_main_change(preview):
            return
        if self._controller.execute(commands.ExcludeBlurTracks(anchors)):
            self.status_label.setText(
                f"{what} を削除しました。ぼかしにも「ぼかさない」にも使いません。"
                "「削除した枠」から元に戻せます。")
        self._selection = None
        self._refresh()

    def _restore_selection(self):
        if self._selection is None or self._selection[0] != _SEL_EXCLUDED:
            self.status_label.setText("元に戻す枠を「削除した枠」から選んでください。")
            return
        entry = self._plan.entry_of(self._selection[1])
        anchor = self._excluded_anchor_of(entry["track"]) if entry is not None else None
        if anchor is None:
            return
        if self._controller.execute(commands.RestoreBlurTrack(anchor)):
            self.status_label.setText("枠を元に戻しました。")
        self._selection = (_SEL_TRACK, self._selection[1])
        self._refresh()

    def _split_selection(self):
        if self._selection is None or self._selection[0] != _SEL_TRACK:
            return
        entry = self._plan.entry_of(self._selection[1])
        if entry is None or entry["kind"] != KIND_PERSON:
            return
        anchor = blur_decisions.track_anchor(entry["track"])
        preview = blur_decisions.with_split(self._decisions(), anchor)
        if not self._confirm_main_change(preview):
            return
        if self._controller.execute(commands.SplitBlurTrack(anchor)):
            self._refresh()
            new_identity = self._plan.identity_of(entry["track"].get("id"))
            self.status_label.setText(
                f"この枠を {self._plan.labels().get(new_identity, new_identity)} として分けました。"
                "分けた人物へ「ぼかす / ぼかさない」を指定できます。")

    def _unsplit_selection(self):
        if self._selection is None or self._selection[0] != _SEL_TRACK:
            return
        entry = self._plan.entry_of(self._selection[1])
        anchor = self._split_anchor_of(entry["track"]) if entry is not None else None
        if anchor is None:
            return
        preview = blur_decisions.without_split(self._decisions(), anchor)
        if not self._confirm_main_change(preview):
            return
        if self._controller.execute(commands.UnsplitBlurTrack(anchor)):
            self.status_label.setText("枠を元の人物へ戻しました。")
        self._refresh()

    # 指定の変更で主役が変わるなら、実行前に確かめる (主役は暗黙に守られるため)
    def _confirm_main_change(self, new_decisions):
        after = BlurPlan(self._timeline, self._analysis, new_decisions, self._cfg)
        if after.main_id == self._plan.main_id:
            return True
        before_label = self._plan.labels().get(self._plan.main_id, "なし")
        after_label = after.labels().get(after.main_id, "なし").replace(" (主役)", "")
        answer = QMessageBox.question(
            self, "主役が変わります",
            f"この操作で、一番映っている人物 (主役) が {before_label.replace(' (主役)', '')} から "
            f"{after_label} に変わります。\n主役は明示的に「ぼかす」にしない限りぼかされません。"
            "続けますか？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    # この tracklet に当たっている指定のアンカー (分割 / 削除)
    def _split_anchor_of(self, track):
        return _matching_anchor(self._decisions()["splits"], track, self._tolerance())

    def _excluded_anchor_of(self, track):
        return _matching_anchor(self._decisions()["excluded"], track, self._tolerance())

    def _tolerance(self):
        sample_fps = float(self._analysis.get("sample_fps") or self._cfg["analysis"]["sample_fps"])
        return 1.5 / max(sample_fps, 0.01)

    # ------------------------------------------------------------------
    # 囲む操作 (R5 / resolve3 §5.5.2)
    # ------------------------------------------------------------------

    def _current_mode(self):
        return blur_decisions.BLUR if self.blur_radio.isChecked() else blur_decisions.KEEP

    def _on_path_drawn(self, points):
        mode = self._current_mode()
        if self.add_object_radio.isChecked():
            self._add_region(points, mode, blur_decisions.KIND_OBJECT)
            self.canvas.clear_path()
            return
        if self.add_place_radio.isChecked():
            self._add_region(points, mode, blur_decisions.KIND_PLACE)
            self.canvas.clear_path()
            return

        # 指定する: 囲みに入った人物へ指定する。人物がいなければ追加した枠へ指定する
        shapes = [s["_shape"] for s in self.canvas.shapes()
                  if not s["_shape"]["excluded"]]
        boxes = [{"identity": s["identity"], "rect": s["rect"]}
                 for s in shapes if s["kind"] == KIND_PERSON and s["identity"]]
        persons = blur_decisions.identities_in_path(
            points, boxes, self._cfg["spec"]["hit_ratio"])
        if persons:
            self._apply_to_identities(persons, mode)
        else:
            region_boxes = [{"identity": s["identity"], "rect": s["rect"]}
                            for s in shapes if s["kind"] != KIND_PERSON]
            regions = blur_decisions.identities_in_path(
                points, region_boxes, self._cfg["spec"]["hit_ratio"])
            for region_id in regions:
                self._controller.execute(commands.SetBlurRegionMode(region_id, mode))
            if regions:
                self._refresh()
                self.status_label.setText(f"囲んだ枠を{_verb(mode)}。")
            else:
                self.status_label.setText(
                    "囲みの中に人物や枠がありません。新しく足すときは「操作」で"
                    "「人物・物を追加」か「場所を追加」を選んでから囲んでください。")
        self.canvas.clear_path()

    # 人物へ指定を反映する。主役をぼかすときだけ確認を出す (§5.6.3-4)。
    def _apply_to_identities(self, identity_ids, mode):
        labels = self._plan.labels()
        main_id = self._plan.main_id
        if mode == blur_decisions.BLUR and main_id in identity_ids:
            answer = QMessageBox.question(
                self, "確認",
                "一番映っている人物です。ぼかしますか？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                identity_ids = [i for i in identity_ids if i != main_id]
                if not identity_ids:
                    return

        for identity_id in identity_ids:
            self._controller.execute(commands.SetBlurDecision(identity_id, mode))

        self._refresh()
        self.status_label.setText(self._summary(identity_ids, mode, labels))

    # 反映の内容を要約する (§5.6.3-5)
    def _summary(self, identity_ids, mode, labels):
        names = "・".join(labels.get(i, i) for i in identity_ids)
        tracks = [e["track"] for e in self._plan.entries
                  if e["kind"] == KIND_PERSON and e["identity"] in identity_ids]
        spans = len(tracks)
        total = sum(float(t.get("end_sec", 0)) - float(t.get("start_sec", 0)) for t in tracks)
        sections = blur_decisions.count_sections(self._timeline, tracks)
        return (f"{names} を{_verb(mode)}。 該当: {sections} セクション / {spans} 区間 / "
                f"合計 {_format_time(total)}")

    # 囲みを新しい枠として登録し、追従を作る (resolve3 §3.6 / §5.7)
    def _add_region(self, points, mode, kind):
        located = self._timemap.to_source(self._playhead)
        if located is None:
            self.status_label.setText("この位置には素材がないため枠を追加できません。")
            return
        media_id, source_sec = located

        decisions = self._decisions()
        same_kind = [r for r in decisions["regions"] if blur_decisions.region_kind(r) == kind]
        name = "追加" if kind == blur_decisions.KIND_OBJECT else "場所"
        region = {
            # 一度使った番号は使わない (resolve3 §2.4 (a))
            "id": blur_decisions.next_region_id(decisions, self._analysis),
            "kind": kind,
            "mode": mode,
            "label": f"{name} {len(same_kind) + 1}",
            "media_id": media_id,
            "anchor_sec": round(float(source_sec), 3),
            # 正規化座標で持つ = 出力解像度が変わっても指定が生きる (§5.2.2)
            "path": [[round(px, 5), round(py, 5)] for px, py in
                     geometry.canvas_path_to_normalized(
                         points, self._timeline.width, self._timeline.height)],
            "follow": (self._cfg["region"]["follow"] if kind == blur_decisions.KIND_PLACE
                       else "track"),
        }
        if not self._controller.execute(commands.AddBlurRegion(region)):
            return

        self._track_added(region)
        self._refresh()
        if region["id"] not in {str(r.get("id")) for r in self._plan.untracked_regions()}:
            self._selection = (_SEL_REGION, region["id"])
            self._refresh()
            self.status_label.setText(f"{region['label']} を追加しました。")
            return

        # 追いかけられなかった: 黙って捨てず、固定にするか取り消すかを選ばせる (resolve3 §3.6)
        where = ("この素材の全区間" if kind == blur_decisions.KIND_PLACE
                 else f"前後 {self._cfg['manual']['fixed_span_sec']:g} 秒")
        box = QMessageBox(self)
        box.setWindowTitle("追いかけられませんでした")
        box.setText(f"{region['label']} を追いかけられませんでした。")
        fixed_button = box.addButton(f"この位置に固定で置く ({where})", QMessageBox.AcceptRole)
        box.addButton("取り消す", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is fixed_button:
            self._controller.execute(commands.SetBlurRegionFollow(region["id"], "fixed"))
            fixed_region = next((r for r in self._decisions()["regions"]
                                 if str(r.get("id")) == region["id"]), region)
            self._track_added(fixed_region)
            self._selection = (_SEL_REGION, region["id"])
            self.status_label.setText(f"{region['label']} をこの位置に固定で置きました。")
        else:
            self._controller.execute(commands.RemoveBlurRegion(region["id"]))
            self.status_label.setText(f"{region['label']} を取り消しました。")
        self._refresh()

    # 追加した枠の追従を走らせ、解析結果へ足してキャッシュを更新する
    def _track_added(self, region):
        from ...blur import manual_tracker, region_tracker   # noqa: PLC0415 (使うときだけ読む)

        progress = QProgressDialog("枠を追いかけています…", "キャンセル", 0, 100, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        decisions = self._decisions()
        builder = (manual_tracker.build_manual_tracks
                   if blur_decisions.region_kind(region) == blur_decisions.KIND_OBJECT
                   else region_tracker.build_region_tracks)
        try:
            added = builder(
                self._timeline, self._settings, self._analysis, decisions, self._cfg,
                on_progress=lambda ratio: _pump(progress, ratio),
                cancel=progress.wasCanceled,
            )
        except Exception as error:                  # noqa: BLE001 (画面を落とさない / §5.9)
            _logger.exception("枠の追従に失敗しました: %s", error)
            added = 0
        finally:
            progress.close()

        if self._cache_path:
            store.save(self._cache_path, self._analysis)
        return added

    # 選んだ 2 人を同じ人物として統合する (§5.6.6)
    def _merge_identities(self):
        selected = [str(i.data(Qt.UserRole)) for i in self.identity_list.selectedItems()]
        if len(selected) != 2:
            self.status_label.setText("統合する人物を一覧から Ctrl を押しながら 2 人選んでください。")
            return
        if self._controller.execute(commands.MergeBlurIdentities(selected[0], selected[1])):
            self.status_label.setText(
                "2 人を同じ人物として扱います (次に解析し直したときに反映されます)。")

    # ------------------------------------------------------------------
    # 後始末
    # ------------------------------------------------------------------

    # 終了時 (閉じる/Esc/×) にフレーム取得を閉じる。
    # closeEvent は「閉じる」ボタン (accept) では呼ばれないため done で行う。
    # 閉じ忘れると素材を開いたままになり、パイプライン終了時に中間ファイルを消せない。
    def done(self, code):
        try:
            self._frame_source.close()
        except Exception:               # noqa: BLE001 (閉じる処理で落とさない)
            _logger.debug("フレーム取得の後始末に失敗しました", exc_info=True)
        super().done(code)


# キャンバスのキー ("track:t3" / "region:r2") を選択へ直す
def _selection_from_key(key):
    kind, _sep, value = str(key or "").partition(":")
    if not value or kind not in (_SEL_TRACK, _SEL_REGION):
        return None
    return kind, value


# 人物全体の役割 (一覧の表示用)。枠ごとの役割は同じ人物なら同じになる。
def _identity_role(plan, identity_id):
    for entry in plan.entries:
        if entry["kind"] == KIND_PERSON and entry["identity"] == identity_id and not entry["excluded"]:
            return entry["role"]
    return None


def _role_text(role):
    if role == ROLE_BLUR:
        return "ぼかす"
    if role == ROLE_KEEP:
        return "ぼかさない"
    return "対象外"


def _verb(mode):
    return "ぼかします" if mode == blur_decisions.BLUR else "ぼかしません"


# アンカーの一覧から、この tracklet に当たるものを返す (無ければ None)
def _matching_anchor(anchors, track, tolerance):
    for anchor in anchors or []:
        if blur_decisions.anchor_matches(anchor, track, tolerance):
            return anchor
    return None


# マスク (PIL の L 画像) を赤い半透明の QImage にする
def _mask_to_image(mask):
    if mask is None:
        return None
    alpha = np.asarray(mask, dtype=np.float32) * _OVERLAY_ALPHA
    height, width = alpha.shape[:2]
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[:, :, 0] = _OVERLAY_RGB[0]
    rgba[:, :, 1] = _OVERLAY_RGB[1]
    rgba[:, :, 2] = _OVERLAY_RGB[2]
    rgba[:, :, 3] = np.clip(alpha, 0, 255).astype(np.uint8)
    image = QImage(rgba.data, width, height, width * 4, QImage.Format_RGBA8888)
    return image.copy()


def _format_time(seconds):
    value = max(int(float(seconds or 0.0)), 0)
    return f"{value // 3600}:{value % 3600 // 60:02d}:{value % 60:02d}"


# 進捗ダイアログを進め、キャンセル操作を拾えるようにする
# (重い処理の途中でも画面が固まらないよう、保留中のイベントを処理する)
def _pump(progress, ratio):
    progress.setValue(int(max(min(float(ratio), 1.0), 0.0) * 100))
    QCoreApplication.processEvents()
