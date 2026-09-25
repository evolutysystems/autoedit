# 縦動画プロジェクトの作成画面 (ver5 resolve9 §5.6)
#
# 選んだクリップの絵を見ながら「ソースのどこを切り抜くか」を決め、
# 縦 (既定 1080x1920) のクリップ用プロジェクトを書き出す。
#
# 右側の「仕上がり」は取得済みのフレームを Qt で切り出して並べるだけで、
# FFmpeg は起動しない。配置の計算は出力と同じ crop.preview_rects を通すため、
# ここで見た構図がそのまま書き出される (§4-3)。
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSlider,
    QVBoxLayout,
)

from ...timeline import crop, project_io, vertical_builder
from ...timeline.frame_source import create_frame_source
from ...utils.logger import get_logger
from .. import theme
from .crop_canvas import CropCanvas

_logger = get_logger(__name__)

# 仕上がりプレビューの表示高 (px)
_PREVIEW_HEIGHT = 520

# 背景の選択肢 (表示名, 値)
_BACKGROUNDS = (("ぼかし", crop.BG_BLUR), ("黒", crop.BG_BLACK))


class VerticalProjectDialog(QDialog):

    # controller : Timeline 編集画面のコントローラ
    # clips      : 対象のベースクリップ (timeline_start 昇順)
    # settings   : setting.json
    def __init__(self, controller, clips, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("縦動画プロジェクトの作成")
        self._controller = controller
        self._timeline = controller.timeline
        self._settings = settings
        self._cfg = crop.config(settings)
        self._clips = list(clips or [])
        self._media = self._resolve_media()
        self._canvas = self._portrait_canvas()
        self._frame = None           # (width, height, bytes)
        self._saved_path = ""        # 作成できたら保存先が入る

        theme.install_window_background(self)
        self._frame_source = create_frame_source(self._settings)
        self._dest_path = self._default_path()

        self.resize(1180, 820)
        self._build_ui()
        self._refresh_frame()

    # 作成したプロジェクトのパス (キャンセルなら空文字)
    def saved_path(self):
        return self._saved_path

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)

        # ── 切り抜き方と背景
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("切り抜き方:"))
        self.single_radio = QRadioButton("全体")
        self.split_radio = QRadioButton("分割")
        self.single_radio.setChecked(True)
        self.single_radio.setToolTip("枠 1 つ。余った上下は背景で埋めます。")
        self.split_radio.setToolTip("枠 2 つ。上下に積んで縦画面をぴったり埋めます。")
        self.single_radio.toggled.connect(self._on_mode_changed)
        top_row.addWidget(self.single_radio)
        top_row.addWidget(self.split_radio)
        top_row.addSpacing(16)

        self.background_label = QLabel("背景:")
        top_row.addWidget(self.background_label)
        self.background_combo = QComboBox()
        for label, value in _BACKGROUNDS:
            self.background_combo.addItem(label, value)
        index = self.background_combo.findData(self._cfg["background"])
        self.background_combo.setCurrentIndex(max(index, 0))
        self.background_combo.currentIndexChanged.connect(self._update_preview)
        self.background_combo.setToolTip(
            "「全体」で余った上下の埋め方です。ぼかしは書き出しでのみ反映されます。")
        top_row.addWidget(self.background_combo)
        top_row.addStretch(1)
        root.addLayout(top_row)

        # ── 左: ソース / 右: 仕上がり
        middle = QHBoxLayout()
        self.canvas = CropCanvas(parent=self)
        self.canvas.setup(self._source_size(), self._canvas, self._cfg["max_scale"])
        self.canvas.set_mode(crop.MODE_SINGLE)
        self.canvas.frames_changed.connect(self._update_preview)
        middle.addWidget(self.canvas, 3)

        preview_box = QVBoxLayout()
        preview_box.addWidget(QLabel("仕上がり"))
        self.preview_label = QLabel()
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumHeight(_PREVIEW_HEIGHT)
        self.preview_label.setMinimumWidth(int(_PREVIEW_HEIGHT * self._canvas[0]
                                                / max(self._canvas[1], 1)))
        theme.mark_preview_canvas(self.preview_label)
        preview_box.addWidget(self.preview_label, 1)
        self.preview_note = QLabel("")
        theme.mark_note(self.preview_note)
        self.preview_note.setWordWrap(True)
        preview_box.addWidget(self.preview_note)
        middle.addLayout(preview_box, 2)
        root.addLayout(middle, 1)

        # ── 時刻 (どのコマで枠を決めるか)
        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("表示する位置:"))
        self.time_slider = QSlider(Qt.Horizontal)
        self.time_slider.setMinimum(0)
        self.time_slider.setMaximum(max(int(self._total_sec() * 10), 1))
        self.time_slider.setValue(0)
        self.time_slider.sliderReleased.connect(self._refresh_frame)
        time_row.addWidget(self.time_slider, 1)
        self.time_label = QLabel("0.0s")
        time_row.addWidget(self.time_label)
        root.addLayout(time_row)

        # ── 対象と保存先
        self.target_label = QLabel(
            f"対象: {len(self._clips)} クリップ / 合計 {self._total_sec():.1f} 秒")
        theme.mark_note(self.target_label)
        root.addWidget(self.target_label)

        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("保存先:"))
        self.dest_label = QLabel(self._dest_path)
        self.dest_label.setWordWrap(True)
        dest_row.addWidget(self.dest_label, 1)
        change_button = QPushButton("変更...")
        change_button.clicked.connect(self._on_change_dest)
        dest_row.addWidget(change_button)
        root.addLayout(dest_row)

        # ── ボタン
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        cancel_button = QPushButton("キャンセル")
        cancel_button.clicked.connect(self.reject)
        button_row.addWidget(cancel_button)
        self.create_button = QPushButton("作成")
        theme.mark_primary(self.create_button, solid=True)
        self.create_button.clicked.connect(self._on_create)
        button_row.addWidget(self.create_button)
        root.addLayout(button_row)

        self._update_preview()

    # ------------------------------------------------------------------
    # 素材・時刻
    # ------------------------------------------------------------------

    # 対象クリップが参照している素材 (先頭のものを使う)
    def _resolve_media(self):
        for clip in self._clips:
            media = self._timeline.media_by_id(getattr(clip, "media_id", ""))
            if media is not None:
                return media
        return None

    def _source_size(self):
        if self._media is None:
            return (self._timeline.width, self._timeline.height)
        return (int(self._media.width or self._timeline.width),
                int(self._media.height or self._timeline.height))

    def _portrait_canvas(self):
        from ...modules import output_profile          # noqa: PLC0415 (循環回避)

        profile = output_profile._portrait_profile((self._settings or {}).get("vertical", {}))
        return (int(profile["width"]), int(profile["height"]))

    def _total_sec(self):
        return sum(float(clip.duration) for clip in self._clips)

    # スライダの位置 → (素材, 素材内の時刻)
    def _source_at(self, offset_sec):
        cursor = 0.0
        for clip in self._clips:
            if offset_sec < cursor + clip.duration or clip is self._clips[-1]:
                media = self._timeline.media_by_id(clip.media_id)
                inside = min(max(offset_sec - cursor, 0.0), max(clip.duration - 0.01, 0.0))
                return media, float(clip.source_in) + inside
            cursor += clip.duration
        return None, 0.0

    # ------------------------------------------------------------------
    # 表示の更新
    # ------------------------------------------------------------------

    def _refresh_frame(self):
        offset = self.time_slider.value() / 10.0
        self.time_label.setText(f"{offset:.1f}s")
        media, source_sec = self._source_at(offset)
        if media is None:
            self.canvas.clear_frame()
            self._frame = None
            self._update_preview()
            return

        try:
            result = self._frame_source.frame_at(media, source_sec)
        except Exception:                 # noqa: BLE001 (表示できなくても画面は開いたまま)
            _logger.exception("フレームの取得に失敗しました")
            result = None

        self._frame = result
        if result is None:
            self.canvas.clear_frame()
        else:
            self.canvas.set_frame(*result)
        self._update_preview()

    def _on_mode_changed(self, _checked=False):
        mode = crop.MODE_SINGLE if self.single_radio.isChecked() else crop.MODE_SPLIT
        self.canvas.set_mode(mode)
        single = mode == crop.MODE_SINGLE
        self.background_label.setEnabled(single)
        self.background_combo.setEnabled(single)

    def _background(self):
        return self.background_combo.currentData() or crop.BG_BLUR

    def _layout(self):
        return crop.make_layout(self.canvas.mode(), self.canvas.frames(), self._media,
                                self._canvas[0], self._canvas[1], self._background())

    # 右側の「仕上がり」を描き直す
    def _update_preview(self, *_args):
        canvas_w, canvas_h = self._canvas
        image = QImage(canvas_w, canvas_h, QImage.Format_RGB888)
        image.fill(Qt.black)

        source = self._frame_pixmap()
        if source is not None:
            painter = QPainter(image)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            for rect in crop.preview_rects(self._layout(), canvas_w, canvas_h):
                sx, sy, sw, sh = rect["src"]
                dx, dy, dw, dh = rect["dest"]
                piece = source.copy(sx, sy, sw, sh)
                painter.drawPixmap(dx, dy, dw, dh, piece)
            painter.end()

        scaled = QPixmap.fromImage(image).scaled(
            self.preview_label.width() or _PREVIEW_HEIGHT // 2,
            self.preview_label.height() or _PREVIEW_HEIGHT,
            Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.preview_label.setPixmap(scaled)

        if self.canvas.mode() == crop.MODE_SINGLE and self._background() == crop.BG_BLUR:
            self.preview_note.setText(
                "上下の余りは、書き出しでソースをぼかした背景で埋まります "
                "(ここでは黒で表示しています)。")
        else:
            self.preview_note.setText("")

    def _frame_pixmap(self):
        if not self._frame:
            return None
        width, height, data = self._frame
        image = QImage(data, int(width), int(height), int(width) * 3, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(image.copy())
        source_w, source_h = self._source_size()
        if pixmap.width() != source_w or pixmap.height() != source_h:
            pixmap = pixmap.scaled(source_w, source_h, Qt.IgnoreAspectRatio,
                                   Qt.SmoothTransformation)
        return pixmap

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------

    def _default_path(self):
        source = self._timeline.source or {}
        input_path = str(source.get("input_path") or source.get("media_path") or "")
        if not input_path:
            input_path = "vertical.mp4"
        return project_io.default_project_path(
            self._settings, input_path, name_suffix=self._cfg["project_suffix"])

    def _on_change_dest(self):
        suffix = (self._settings.get("timeline", {}) or {}).get(
            "project_suffix", ".timeline.json") or ".timeline.json"
        path, _filter = QFileDialog.getSaveFileName(
            self, "縦動画プロジェクトの保存先", self._dest_path,
            f"Timeline プロジェクト (*{suffix});;すべてのファイル (*)")
        if path:
            self._dest_path = path
            self.dest_label.setText(path)

    def _on_create(self):
        if self._media is None:
            QMessageBox.warning(self, "作成できません",
                                "対象クリップの素材が見つかりません。")
            return
        if os.path.exists(self._dest_path):
            answer = QMessageBox.question(
                self, "上書きしますか",
                f"すでにファイルがあります。上書きしますか？\n{self._dest_path}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return

        try:
            vertical, warnings = vertical_builder.build(
                self._timeline, [clip.id for clip in self._clips], self._layout(),
                self._settings, close_gaps=self._cfg["close_gaps"])
            project_io.save(vertical, self._dest_path, generator="vertical",
                            project_path=self._dest_path)
        except Exception as e:            # noqa: BLE001 (画面へ集約通知)
            _logger.exception("縦動画プロジェクトの作成に失敗")
            QMessageBox.critical(self, "作成できません",
                                 f"縦動画プロジェクトを作成できませんでした。\n{e}")
            return

        self._saved_path = self._dest_path
        message = f"縦動画プロジェクトを作成しました。\n{self._dest_path}"
        if warnings:
            message += "\n\n" + "\n".join(f"・{text}" for text in warnings)
        message += "\n\n「クリップ用」タブの「続きから」で開けます。"
        QMessageBox.information(self, "作成しました", message)
        self.accept()

    def done(self, code):
        try:
            self._frame_source.close()
        except Exception:                 # noqa: BLE001 (後始末の失敗は無視する)
            _logger.debug("フレーム取得の後始末に失敗", exc_info=True)
        super().done(code)
