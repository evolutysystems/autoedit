# ぼかし指定画面 (ver5 resolve2 §5.6)
#
# 解析済みの人物・領域を一覧し、フレームの上をドラッグして囲むことで
# 「ぼかす / ぼかさない」を指定する (R5)。
#
# 指定は**人物 ID に対して**行うため、全セクションへ同時に効く (R6)。
# 「全セクションを見直す」処理は解析の時点で済んでおり (§5.6.4)、
# この画面は該当件数を数えて見せるだけでよい。
#
# 指定の変更はすべて commands.Command として積むため、Timeline 編集画面の
# 「元に戻す」からぼかし指定も戻せる (§5.6.5)。
from PySide6.QtCore import QCoreApplication, QThread, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ...blur import decisions as blur_decisions
from ...blur import geometry, store
from ...blur.config import config as blur_config
from ...timeline import commands
from ...timeline.frame_source import create_frame_source
from ...timeline.timemap import TimeMap
from ...utils.logger import get_logger
from .. import theme
from .blur_canvas import BlurCanvas

_logger = get_logger(__name__)

# 一覧に出す見本の大きさ (px)
_THUMB_PX = 64


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
    # cache_path  : 解析結果の置き場 (領域の追従を足したら書き戻す)
    def __init__(self, controller, analysis, parent=None, cache_path=None):
        super().__init__(parent)
        self.setWindowTitle("ぼかし指定")
        self._controller = controller
        self._cache_path = cache_path
        self._timeline = controller.timeline
        self._settings = controller.settings
        self._analysis = analysis or {}
        self._cfg = blur_config(self._settings)
        self._main_id = store.main_identity_id(self._analysis)
        self._timemap = TimeMap.from_timeline(self._timeline)
        self._playhead = 0.0
        theme.install_window_background(self)

        self._frame_source = create_frame_source(self._settings)
        self.resize(1100, 760)
        self._build_ui()
        self._refresh_identities()
        self._seek(0.0)

    # ------------------------------------------------------------------
    # 画面構築 (§5.6.2)
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)

        upper = QHBoxLayout()
        self.canvas = BlurCanvas(self._timeline.width, self._timeline.height, parent=self)
        self.canvas.path_drawn.connect(self._on_path_drawn)
        self.canvas.box_clicked.connect(self._on_box_clicked)
        upper.addWidget(self.canvas, 4)

        side = QVBoxLayout()
        side.addWidget(QLabel("人物"))
        self.identity_list = QListWidget()
        self.identity_list.setIconSize(QPixmap(_THUMB_PX, _THUMB_PX).size())
        self.identity_list.itemDoubleClicked.connect(self._on_identity_double_clicked)
        side.addWidget(self.identity_list, 3)

        side.addWidget(QLabel("領域 (建物など)"))
        self.region_list = QListWidget()
        side.addWidget(self.region_list, 1)

        region_row = QHBoxLayout()
        self.remove_region_button = QPushButton("領域を削除")
        self.remove_region_button.setAutoDefault(False)
        self.remove_region_button.clicked.connect(self._remove_region)
        region_row.addWidget(self.remove_region_button)
        self.merge_button = QPushButton("同じ人物にする")
        self.merge_button.setAutoDefault(False)
        self.merge_button.setToolTip(
            "着替えなどで別人物として分かれてしまった 2 人を選んで統合します。\n"
            "統合した指定は解析をやり直しても残ります。")
        self.merge_button.clicked.connect(self._merge_identities)
        region_row.addWidget(self.merge_button)
        side.addLayout(region_row)

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

        # 指定モード
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("指定モード:"))
        self._mode_group = QButtonGroup(self)
        self.blur_radio = QRadioButton("ぼかす")
        self.blur_radio.setChecked(True)
        self.keep_radio = QRadioButton("ぼかさない")
        self._mode_group.addButton(self.blur_radio)
        self._mode_group.addButton(self.keep_radio)
        mode_row.addWidget(self.blur_radio)
        mode_row.addWidget(self.keep_radio)
        mode_row.addStretch(1)

        self.policy_label = QLabel(blur_decisions.policy_label(self._decisions(), self._cfg))
        mode_row.addWidget(self.policy_label)

        self.clear_path_button = QPushButton("囲みを取り消す")
        self.clear_path_button.setAutoDefault(False)
        self.clear_path_button.clicked.connect(self.canvas.clear_path)
        mode_row.addWidget(self.clear_path_button)

        close_button = QPushButton("閉じる")
        close_button.setAutoDefault(False)
        close_button.clicked.connect(self.accept)
        theme.mark_primary(close_button)
        mode_row.addWidget(close_button)
        root.addLayout(mode_row)

        self.status_label = QLabel("フレームの上をドラッグして対象を囲んでください。")
        root.addWidget(self.status_label)

    # ------------------------------------------------------------------
    # 表示の更新
    # ------------------------------------------------------------------

    def _decisions(self):
        return blur_decisions.load(self._timeline)

    # 現在の再生位置のフレームと検出枠を出す
    def _seek(self, timeline_sec):
        self._playhead = max(float(timeline_sec), 0.0)
        self.time_label.setText(_format_time(self._playhead))

        located = self._timemap.to_source(self._playhead)
        if located is None:
            self.canvas.clear_frame()
            self.canvas.set_boxes([])
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

        self.canvas.set_boxes(self._boxes_at(media, media_id, source_sec))

    # その時刻に生きているトラックを検出枠へ直す (キャンバス座標)
    def _boxes_at(self, media, media_id, source_sec):
        transform = geometry.source_to_canvas_transform(
            media, self._timeline.width, self._timeline.height)
        decisions = self._decisions()
        labels = self._identity_labels()

        boxes = []
        for track in self._analysis.get("tracks", []):
            if str(track.get("media_id") or "") != str(media_id):
                continue
            rect = _sample_rect_at(track, source_sec)
            if rect is None:
                continue
            identity = str(track.get("identity") or "")
            kind = str(track.get("kind", "person"))
            if kind == "person":
                blur = blur_decisions.should_blur_identity(
                    identity, decisions, self._cfg, self._main_id)
                label = labels.get(identity, identity)
            else:
                region = next((r for r in decisions["regions"]
                               if str(r.get("id")) == identity), None)
                blur = blur_decisions.should_blur_region(region)
                label = str((region or {}).get("label") or identity)

            boxes.append({
                "identity": identity,
                "rect": geometry.source_rect_to_canvas(rect, transform),
                "mode": "blur" if blur else "keep",
                "main": identity == self._main_id,
                "label": label,
            })
        return boxes

    def _identity_labels(self):
        labels = {}
        for index, identity in enumerate(self._analysis.get("identities", []), 1):
            identity_id = str(identity.get("id"))
            name = f"人物 {index}"
            if identity.get("main"):
                name += " (主役)"
            labels[identity_id] = name
        return labels

    # 人物・領域の一覧を作り直す
    def _refresh_identities(self):
        decisions = self._decisions()
        labels = self._identity_labels()

        self.identity_list.clear()
        for identity in self._analysis.get("identities", []):
            identity_id = str(identity.get("id"))
            blur = blur_decisions.should_blur_identity(
                identity_id, decisions, self._cfg, self._main_id)
            text = (f"{labels.get(identity_id, identity_id)}\n"
                    f"{_format_time(float(identity.get('total_sec', 0.0)))} / "
                    f"{'ぼかす' if blur else 'ぼかさない'}")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, identity_id)
            thumb = store.decode_thumb(identity.get("thumb"))
            if thumb:
                pixmap = QPixmap()
                if pixmap.loadFromData(thumb):
                    item.setIcon(pixmap.scaled(
                        _THUMB_PX, _THUMB_PX, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.identity_list.addItem(item)

        self.region_list.clear()
        for region in decisions["regions"]:
            mode = "ぼかす" if blur_decisions.should_blur_region(region) else "ぼかさない"
            item = QListWidgetItem(f"{region.get('label') or region.get('id')} / {mode}")
            item.setData(Qt.UserRole, str(region.get("id")))
            self.region_list.addItem(item)

        self.policy_label.setText(blur_decisions.policy_label(decisions, self._cfg))

    def _step(self, seconds):
        self.slider.setValue(max(int((self._playhead + seconds) * 10), 0))

    # ------------------------------------------------------------------
    # 囲む操作 (R5 / §5.6.3)
    # ------------------------------------------------------------------

    def _on_path_drawn(self, points):
        mode = blur_decisions.BLUR if self.blur_radio.isChecked() else blur_decisions.KEEP
        boxes = [{"identity": b["identity"], "rect": b["rect"]}
                 for b in self.canvas.boxes() if b.get("identity")]
        hit = blur_decisions.identities_in_path(
            points, boxes, self._cfg["spec"]["hit_ratio"])
        persons = [i for i in hit if not i.startswith("r")]

        if persons:
            self._apply_to_identities(persons, mode)
        else:
            # 人物が 0 人 = 領域 (建物など) として登録する (§5.6.3-4)
            self._add_region(points, mode)
        self.canvas.clear_path()

    def _on_box_clicked(self, identity_id):
        if not identity_id:
            return
        mode = blur_decisions.BLUR if self.blur_radio.isChecked() else blur_decisions.KEEP
        self._apply_to_identities([identity_id], mode)

    def _on_identity_double_clicked(self, item):
        identity_id = str(item.data(Qt.UserRole) or "")
        decisions = self._decisions()
        blur = blur_decisions.should_blur_identity(
            identity_id, decisions, self._cfg, self._main_id)
        # ダブルクリックで「ぼかす / ぼかさない」を入れ替える
        self._apply_to_identities(
            [identity_id], blur_decisions.KEEP if blur else blur_decisions.BLUR)

    # 人物へ指定を反映する。主役をぼかすときだけ確認を出す (§5.6.3-4)。
    def _apply_to_identities(self, identity_ids, mode):
        labels = self._identity_labels()
        if mode == blur_decisions.BLUR and self._main_id in identity_ids:
            answer = QMessageBox.question(
                self, "確認",
                "一番映っている人物です。ぼかしますか？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                identity_ids = [i for i in identity_ids if i != self._main_id]
                if not identity_ids:
                    return

        for identity_id in identity_ids:
            self._controller.execute(commands.SetBlurDecision(identity_id, mode))

        self._refresh_identities()
        self._seek(self._playhead)
        self.status_label.setText(self._summary(identity_ids, mode, labels))

    # 反映の内容を要約する (§5.6.3-5)
    def _summary(self, identity_ids, mode, labels):
        names = "・".join(labels.get(i, i) for i in identity_ids)
        tracks = [t for t in self._analysis.get("tracks", [])
                  if str(t.get("identity") or "") in identity_ids]
        spans = len(tracks)
        total = sum(float(t.get("end_sec", 0)) - float(t.get("start_sec", 0)) for t in tracks)
        sections = blur_decisions.count_sections(self._timeline, tracks)
        verb = "ぼかします" if mode == blur_decisions.BLUR else "ぼかしません"
        return (f"{names} を{verb}。 該当: {sections} セクション / {spans} 区間 / "
                f"合計 {_format_time(total)}")

    # 囲みを領域として登録する (R7)
    def _add_region(self, points, mode):
        located = self._timemap.to_source(self._playhead)
        if located is None:
            self.status_label.setText("この位置には素材がないため領域を登録できません。")
            return
        media_id, source_sec = located

        decisions = self._decisions()
        region = {
            "id": blur_decisions.next_region_id(decisions),
            "mode": mode,
            "label": f"領域 {len(decisions['regions']) + 1}",
            "media_id": media_id,
            "anchor_sec": round(float(source_sec), 3),
            # 正規化座標で持つ = 出力解像度が変わっても指定が生きる (§5.2.2)
            "path": [[round(px, 5), round(py, 5)] for px, py in
                     geometry.canvas_path_to_normalized(
                         points, self._timeline.width, self._timeline.height)],
            "follow": self._cfg["region"]["follow"],
        }
        if not self._controller.execute(commands.AddBlurRegion(region)):
            return

        self._refresh_identities()
        # 領域は指定の時点で追従が要る (§5.6.4)。囲んだ直後に短い処理を走らせ、
        # 得られた軌跡を kind="region" のトラックとして解析結果へ足す。
        added = self._track_region(region)
        self._seek(self._playhead)
        if added:
            self.status_label.setText(
                f"{region['label']} を登録し、{added} 区間へ追従させました。")
        else:
            self.status_label.setText(
                f"{region['label']} を登録しましたが、追従できた区間がありません。"
                "ズームが入る素材では setting.json の blur.region.follow を "
                "fixed にすると固定でぼかせます。")

    # 領域の追従を走らせ、解析結果へ足してキャッシュを更新する
    def _track_region(self, region):
        from ...blur import region_tracker         # noqa: PLC0415 (機能 OFF なら読まない)

        progress = QProgressDialog("領域を追従しています…", "キャンセル", 0, 100, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        try:
            added = region_tracker.build_region_tracks(
                self._timeline, self._settings, self._analysis, self._decisions(),
                self._cfg,
                on_progress=lambda ratio: _pump(progress, ratio),
                cancel=progress.wasCanceled,
            )
        except Exception as error:                  # noqa: BLE001 (画面を落とさない / §5.9)
            _logger.exception("領域の追従に失敗しました: %s", error)
            added = 0
        finally:
            progress.close()

        if added and self._cache_path:
            store.save(self._cache_path, self._analysis)
        return added

    def _remove_region(self):
        item = self.region_list.currentItem()
        if item is None:
            self.status_label.setText("削除する領域を一覧から選んでください。")
            return
        region_id = str(item.data(Qt.UserRole) or "")
        if self._controller.execute(commands.RemoveBlurRegion(region_id)):
            self._refresh_identities()
            self._seek(self._playhead)

    # 選んだ 2 人を同じ人物として統合する (§5.6.6)
    def _merge_identities(self):
        selected = [str(i.data(Qt.UserRole)) for i in self.identity_list.selectedItems()]
        if len(selected) != 2:
            self.status_label.setText("統合する人物を一覧から 2 人選んでください。")
            return
        if self._controller.execute(commands.MergeBlurIdentities(selected[0], selected[1])):
            self.status_label.setText(
                "2 人を同じ人物として扱います (次の解析から反映されます)。")

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


# トラックの samples から、その時刻の矩形を線形補間で求める。
# 範囲外なら None (その時刻には映っていない)。
def _sample_rect_at(track, source_sec):
    samples = track.get("samples") or []
    if not samples:
        return None
    if source_sec < float(samples[0]["t"]) or source_sec > float(samples[-1]["t"]):
        return None

    previous = samples[0]
    for sample in samples:
        if float(sample["t"]) >= source_sec:
            span = float(sample["t"]) - float(previous["t"])
            ratio = (source_sec - float(previous["t"])) / span if span > 1e-9 else 0.0
            return (
                previous["x"] + (sample["x"] - previous["x"]) * ratio,
                previous["y"] + (sample["y"] - previous["y"]) * ratio,
                previous["w"] + (sample["w"] - previous["w"]) * ratio,
                previous["h"] + (sample["h"] - previous["h"]) * ratio,
            )
        previous = sample
    return (previous["x"], previous["y"], previous["w"], previous["h"])


def _format_time(seconds):
    value = max(int(float(seconds or 0.0)), 0)
    return f"{value // 3600}:{value % 3600 // 60:02d}:{value % 60:02d}"


# 進捗ダイアログを進め、キャンセル操作を拾えるようにする
# (重い処理の途中でも画面が固まらないよう、保留中のイベントを処理する)
def _pump(progress, ratio):
    progress.setValue(int(max(min(float(ratio), 1.0), 0.0) * 100))
    QCoreApplication.processEvents()
