# Timeline 編集画面 (docs/request/ver3/resolve.md §5.2)
# 上部にプレビュー、下部に Timeline を置いたモーダルダイアログ。
# 「決定」で Timeline の内容を反映した動画を書き出し、「キャンセル」で処理を中断する。
#
# 現行の字幕編集画面 (SubtitleEditorDialog) は削除せず残してあり、
# setting.json の timeline.enabled=false でそちらへ戻せる (R2)。
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...export import resolve_export
from ...timeline.model import AudioClip, SubtitleClip
from ...utils.logger import get_logger
from ..subtitle_editor_dialog import RESOLVE_EXPORT_BUTTON_TEXT, run_resolve_export
from .preview_panel import PreviewPanel
from .timeline_controller import TimelineController
from .timeline_view import TimelinePanel, format_time_precise

_logger = get_logger(__name__)

# 字幕の役割 (既存 subtitle_editor_dialog と同じ選択肢)
_ROLE_CHOICES = [("配信者", "streamer"), ("サブ", "sub"), ("コメント", "comment")]


class TimelineEditorDialog(QDialog):

    # timeline      : 編集対象 (builder が組んだもの)
    # settings      : setting.json
    # work_dir      : プレビュー用一時ファイルの置き場 (PipelineContext.working_dir)
    # asr_audio_path: 認識用に生成済みの音声 (プレビュー初回再生に再利用する / §6.4-5)
    def __init__(self, timeline, settings, work_dir, asr_audio_path=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Timeline 編集")
        self._timeline = timeline
        self._settings = settings

        self.controller = TimelineController(timeline, settings, parent=self)
        ui_cfg = self.controller.cfg["ui"]
        self.resize(ui_cfg["window_width"], ui_cfg["window_height"])

        self._build_ui(work_dir, asr_audio_path, ui_cfg)
        self._register_shortcuts()

        self.controller.selection_changed.connect(self._on_selection_changed)
        self.controller.timeline_changed.connect(self._on_timeline_changed)
        self.preview.playing_changed.connect(self._on_playing_changed)
        self._on_selection_changed(None)

    # ------------------------------------------------------------------
    # 画面構築 (§5.2)
    # ------------------------------------------------------------------

    def _build_ui(self, work_dir, asr_audio_path, ui_cfg):
        root = QVBoxLayout(self)

        # 上部: プレビュー + インスペクタ
        upper = QSplitter(Qt.Horizontal)
        self.preview = PreviewPanel(self.controller, work_dir, asr_audio_path, parent=self)
        upper.addWidget(self.preview)
        self.inspector = _InspectorPanel(self.controller, parent=self)
        upper.addWidget(self.inspector)
        upper.setStretchFactor(0, 4)
        upper.setStretchFactor(1, 1)

        # 下部: Timeline
        self.timeline_panel = TimelinePanel(self.controller, parent=self)

        self._splitter = QSplitter(Qt.Vertical)
        self._splitter.addWidget(upper)
        self._splitter.addWidget(self.timeline_panel)
        ratio = float(ui_cfg["split_ratio"])
        self._splitter.setStretchFactor(0, max(int(ratio * 100), 1))
        self._splitter.setStretchFactor(1, max(int((1 - ratio) * 100), 1))
        root.addWidget(self._splitter, 1)

        # 下部ボタン
        button_row = QHBoxLayout()
        self.undo_button = QPushButton("元に戻す")
        self.undo_button.setToolTip("Ctrl+Z")
        self.undo_button.clicked.connect(self._undo)
        self.redo_button = QPushButton("やり直す")
        self.redo_button.setToolTip("Ctrl+Y")
        self.redo_button.clicked.connect(self._redo)
        button_row.addWidget(self.undo_button)
        button_row.addWidget(self.redo_button)
        button_row.addStretch(1)

        # DaVinci Resolve 出力 (既存ボタンをそのまま流用する / resolve20)
        self.export_button = None
        if resolve_export.is_enabled(self._settings):
            self.export_button = QPushButton(RESOLVE_EXPORT_BUTTON_TEXT)
            self.export_button.setToolTip(
                "現在の Timeline を DaVinci Resolve 用プロジェクトファイル (.fcpxml) "
                "として出力します。")
            self.export_button.clicked.connect(self._on_export_resolve)
            button_row.addWidget(self.export_button)

        self.decide_button = QPushButton("決定")
        # 既定ボタンにしない: Enter / Space の取りこぼしで書き出しが始まると
        # 長い処理が意図せず走ってしまうため、必ずクリックで実行させる。
        self.decide_button.setAutoDefault(False)
        self.decide_button.setToolTip("Timeline の内容を反映した動画を書き出します")
        self.decide_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        button_row.addWidget(self.decide_button)
        button_row.addWidget(self.cancel_button)
        root.addLayout(button_row)

        self._update_history_buttons()

    # ------------------------------------------------------------------
    # ショートカット (§6.6 / §6.7)
    # ------------------------------------------------------------------

    def _register_shortcuts(self):
        def bind(sequence, handler):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(handler)
            return shortcut

        bind("Ctrl+B", lambda: self.controller.split_at_playhead())
        bind("Delete", self._delete_default)
        bind("Shift+Delete", self._delete_alternate)
        bind("Ctrl+Z", self._undo)
        bind("Ctrl+Y", self._redo)
        bind("Ctrl+Shift+Z", self._redo)
        bind("Space", self.preview.toggle_play)
        bind("Left", lambda: self.controller.step_playhead(-1))
        bind("Right", lambda: self.controller.step_playhead(1))
        bind("Home", lambda: self.controller.set_playhead(0.0))
        bind("End", lambda: self.controller.set_playhead(
            self.controller.timeline.duration_sec()))
        bind("Ctrl++", lambda: self.controller.zoom_by(1.25))
        bind("Ctrl+=", lambda: self.controller.zoom_by(1.25))
        bind("Ctrl+-", lambda: self.controller.zoom_by(1 / 1.25))

    # Delete キー単独: 設定 (timeline.ripple_delete) に従う (既定 = 空白を残す)
    def _delete_default(self):
        self.controller.delete_selected(ripple=self.controller.cfg["ripple_delete"])

    # Shift+Delete: 常にもう一方
    def _delete_alternate(self):
        self.controller.delete_selected(
            ripple=not self.controller.cfg["ripple_delete"])

    def _undo(self):
        self.controller.undo()
        self._update_history_buttons()

    def _redo(self):
        self.controller.redo()
        self._update_history_buttons()

    def _update_history_buttons(self):
        self.undo_button.setEnabled(self.controller.can_undo())
        self.redo_button.setEnabled(self.controller.can_redo())
        undo_label = self.controller.undo_label()
        redo_label = self.controller.redo_label()
        self.undo_button.setText(f"元に戻す: {undo_label}" if undo_label else "元に戻す")
        self.redo_button.setText(f"やり直す: {redo_label}" if redo_label else "やり直す")

    # ------------------------------------------------------------------
    # 状態変化
    # ------------------------------------------------------------------

    def _on_timeline_changed(self):
        self._update_history_buttons()
        self.inspector.refresh()

    def _on_selection_changed(self, clip):
        self.inspector.show_clip(clip)

    # 再生中は編集操作を受け付けない (状態の競合を避ける / §4-8)
    def _on_playing_changed(self, playing):
        self.timeline_panel.setEnabled(not playing)
        self.inspector.setEnabled(not playing)
        self.undo_button.setEnabled(not playing and self.controller.can_undo())
        self.redo_button.setEnabled(not playing and self.controller.can_redo())

    # ------------------------------------------------------------------
    # DaVinci Resolve 出力
    # ------------------------------------------------------------------

    def _on_export_resolve(self):
        run_resolve_export(
            self,
            lambda confirm: resolve_export.export_timeline(
                self.controller.timeline, self._settings, overwrite_confirm=confirm),
        )

    # ------------------------------------------------------------------
    # 終了
    # ------------------------------------------------------------------

    # 編集結果の Timeline を返す
    def result_timeline(self):
        return self.controller.timeline

    def accept(self):
        timeline = self.controller.timeline
        if not timeline.base_clips():
            QMessageBox.warning(
                self, "書き出せません",
                "映像クリップが 1 つもありません。クリップを残してから決定してください。")
            return
        _logger.info(
            "Timeline 編集確定: V %d クリップ / オーバーレイ %d 件 / 字幕 %d 件"
            "（使用 %d 件）/ ギャップ %d 件",
            len(timeline.base_clips()),
            len([e for e in timeline.overlay_elements()
                 if not isinstance(e, SubtitleClip)]),
            len(timeline.subtitle_clips(only_used=False)),
            len(timeline.subtitle_clips()),
            len(timeline.base_gaps()),
        )
        super().accept()

    # 編集済みのまま閉じようとしたら確認する (誤操作でパイプラインを中断させない)
    def reject(self):
        if self.controller.is_dirty():
            answer = QMessageBox.question(
                self, "編集を破棄しますか",
                "Timeline の編集内容が破棄され、処理も中断されます。よろしいですか?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        super().reject()

    # 終了時に再生を止め、スレッドと一時ファイルを片づける (§10)
    def done(self, code):
        try:
            self.preview.shutdown()
        except Exception:  # noqa: BLE001 (後片付けの失敗で終了を妨げない)
            _logger.exception("プレビューの後片付けに失敗しました")
        super().done(code)


# フォーカスが外れたときにだけ確定を通知する複数行入力欄
# (1 打鍵ごとに編集コマンドを積んで Undo 履歴を埋め尽くさないため)
class _CommitOnFocusOutEdit(QPlainTextEdit):

    editing_finished = Signal()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.editing_finished.emit()


# ==================================================================
# インスペクタ (選択要素の詳細編集)
# ==================================================================

class _InspectorPanel(QWidget):

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._clip = None
        self._updating = False
        self.setMinimumWidth(220)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        self.title_label = QLabel("選択なし")
        self.title_label.setStyleSheet("font-weight:bold;")
        root.addWidget(self.title_label)

        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet("color:#888;")
        root.addWidget(self.info_label)

        # 字幕用の編集欄
        self.subtitle_widget = QWidget()
        form = QFormLayout(self.subtitle_widget)
        form.setContentsMargins(0, 6, 0, 0)
        self.text_edit = _CommitOnFocusOutEdit()
        self.text_edit.setPlaceholderText("字幕テキスト (改行できます)")
        self.text_edit.setFixedHeight(80)
        # 1 文字ごとにコマンドを積むと Undo 履歴が使い物にならなくなるため、
        # 入力欄からフォーカスが外れたときにまとめて確定する。
        self.text_edit.editing_finished.connect(self._on_text_changed)
        form.addRow("字幕", self.text_edit)

        self.role_combo = QComboBox()
        for label, value in _ROLE_CHOICES:
            self.role_combo.addItem(label, value)
        self.role_combo.currentIndexChanged.connect(self._on_role_changed)
        form.addRow("役割", self.role_combo)

        self.font_edit = QLineEdit()
        self.font_edit.setPlaceholderText("空欄=設定のフォント")
        self.font_edit.editingFinished.connect(self._on_font_changed)
        form.addRow("フォント", self.font_edit)

        self.size_spin = QDoubleSpinBox()
        self.size_spin.setDecimals(0)
        self.size_spin.setRange(0, 999)
        self.size_spin.setSpecialValueText("既定")
        self.size_spin.editingFinished.connect(self._on_size_changed)
        form.addRow("サイズ", self.size_spin)
        root.addWidget(self.subtitle_widget)

        self.reset_position_button = QPushButton("位置を既定へ戻す")
        self.reset_position_button.setToolTip(
            "ドラッグで動かした位置を捨てて、設定の配置・余白に従わせます")
        self.reset_position_button.clicked.connect(self._reset_position)
        root.addWidget(self.reset_position_button)

        root.addStretch(1)
        self.subtitle_widget.setVisible(False)
        self.reset_position_button.setVisible(False)

    # 選択要素を表示する
    def show_clip(self, clip):
        self._clip = clip
        self._updating = True
        try:
            if clip is None:
                self.title_label.setText("選択なし")
                self.info_label.setText(
                    "Timeline のクリップ、またはプレビュー上の画像・字幕を選ぶと"
                    "ここに詳細が出ます。")
                self.subtitle_widget.setVisible(False)
                self.reset_position_button.setVisible(False)
                return
            is_subtitle = isinstance(clip, SubtitleClip)
            self.subtitle_widget.setVisible(is_subtitle)
            self.reset_position_button.setVisible(True)
            self.title_label.setText(self._title_for(clip))
            self.info_label.setText(self._info_for(clip))
            if is_subtitle:
                self.text_edit.setPlainText(clip.text.replace("\\N", "\n"))
                index = self.role_combo.findData(clip.role)
                self.role_combo.setCurrentIndex(max(index, 0))
                self.font_edit.setText(clip.font or "")
                self.size_spin.setValue(float(clip.font_size or 0))
        finally:
            self._updating = False

    # 表示を作り直す (編集後)
    def refresh(self):
        clip_id = getattr(self._clip, "id", None)
        if clip_id is None:
            return
        self.show_clip(self._controller.timeline.clip_by_id(clip_id))

    def _title_for(self, clip):
        if isinstance(clip, SubtitleClip):
            return "字幕クリップ"
        if isinstance(clip, AudioClip):
            return "音声クリップ"
        origin = clip.origin_type()
        return {"opening": "オープニング", "ending": "エンディング",
                "silence_cut": "本編クリップ",
                "user_media": "追加メディア"}.get(origin, "クリップ")

    def _info_for(self, clip):
        timeline = self._controller.timeline
        if isinstance(clip, AudioClip):
            start = clip.timeline_start(timeline)
            return (f"{clip.id} / リンク: {clip.link_clip}\n"
                    f"開始 {format_time_precise(start or 0)}\n"
                    "※ 時刻は映像クリップから導出されます")
        lines = [
            f"{clip.id}",
            f"開始 {format_time_precise(clip.timeline_start)}",
            f"尺 {clip.duration:.2f} 秒",
        ]
        if not isinstance(clip, SubtitleClip):
            media = timeline.media_by_id(clip.media_id)
            if media is not None:
                import os
                lines.append(f"素材 {os.path.basename(media.path)}")
            lines.append(f"素材内 {clip.source_in:.2f}〜{clip.source_out:.2f} 秒")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 編集
    # ------------------------------------------------------------------

    def _on_text_changed(self):
        if self._updating or not isinstance(self._clip, SubtitleClip):
            return
        text = self.text_edit.toPlainText().replace("\n", "\\N")
        self._controller.edit_subtitle(self._clip.id, text=text)

    def _on_role_changed(self):
        if self._updating or not isinstance(self._clip, SubtitleClip):
            return
        self._controller.edit_subtitle(
            self._clip.id, role=self.role_combo.currentData())

    def _on_font_changed(self):
        if self._updating or not isinstance(self._clip, SubtitleClip):
            return
        self._controller.edit_subtitle(self._clip.id, font=self.font_edit.text().strip())

    def _on_size_changed(self):
        if self._updating or not isinstance(self._clip, SubtitleClip):
            return
        value = int(self.size_spin.value())
        self._controller.edit_subtitle(
            self._clip.id, font_size=value if value > 0 else None)

    # ドラッグで付いた位置指定を捨てる (字幕は設定の配置へ戻る)
    def _reset_position(self):
        if self._clip is None:
            return
        self._controller.move_overlay(self._clip.id, None, None)
