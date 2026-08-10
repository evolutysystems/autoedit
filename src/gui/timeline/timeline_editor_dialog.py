# Timeline 編集画面 (docs/request/ver3/resolve.md §5.2)
# 上部にプレビュー、下部に Timeline を置いたモーダルダイアログ。
# 「決定」で Timeline の内容を反映した動画を書き出し、「キャンセル」で処理を中断する。
#
# 現行の字幕編集画面 (SubtitleEditorDialog) は削除せず残してあり、
# setting.json の timeline.enabled=false でそちらへ戻せる (R2)。
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ...export import resolve_export
from ...timeline.model import AudioClip, SubtitleClip
from ...utils.logger import get_logger
from .. import theme
from ..subtitle_editor_dialog import RESOLVE_EXPORT_BUTTON_TEXT, run_resolve_export
from .preview_panel import PreviewPanel
from .timeline_controller import TimelineController
from .timeline_view import TimelinePanel, format_time_precise

_logger = get_logger(__name__)

# 字幕の役割 (既存 subtitle_editor_dialog と同じ選択肢)
_ROLE_CHOICES = [("配信者", "streamer"), ("サブ", "sub"), ("コメント", "comment")]

# フォント一覧の先頭に置く「指定なし」= setting.json の字幕フォントに従う
_FONT_DEFAULT_LABEL = "(設定のフォント)"
# 一覧の各項目をそのフォント自身で描くときの文字サイズ (字幕編集画面と同じ)
_FONT_PREVIEW_POINT_SIZE = 12


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
        # 背景グラデーションを敷く (この上に Timeline の半透明な下地が乗る / resolve3 §3-2)
        theme.install_window_background(self)

        self.controller = TimelineController(timeline, settings, parent=self)
        ui_cfg = self.controller.cfg["ui"]
        self.resize(ui_cfg["window_width"], ui_cfg["window_height"])

        self._build_ui(work_dir, asr_audio_path, ui_cfg)
        self._register_shortcuts()

        self.controller.selection_changed.connect(self._on_selection_changed)
        self.controller.timeline_changed.connect(self._on_timeline_changed)
        self.preview.playing_changed.connect(self._on_playing_changed)
        # Timeline 側で実行できなかった操作の案内をプレビュー下へ出す
        self.timeline_panel.view.status_message.connect(self.preview.set_status)
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

        # 「決定」は主要動作 (アクセント塗り)、「キャンセル」は処理を中断する破壊的動作
        # として輪郭ボタンにする (resolve3 §5.2-2 / §5.2-3)
        self.decide_button = QPushButton("決定")
        # 既定ボタンにしない: Enter / Space の取りこぼしで書き出しが始まると
        # 長い処理が意図せず走ってしまうため、必ずクリックで実行させる。
        self.decide_button.setAutoDefault(False)
        self.decide_button.setToolTip("Timeline の内容を反映した動画を書き出します")
        self.decide_button.clicked.connect(self.accept)
        theme.mark_primary(self.decide_button)
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        theme.mark_danger(self.cancel_button)
        button_row.addWidget(self.decide_button)
        button_row.addWidget(self.cancel_button)
        root.addLayout(button_row)

        self._update_history_buttons()

    # ------------------------------------------------------------------
    # ショートカット (§6.6 / §6.7)
    # ------------------------------------------------------------------

    # キー割り当ては setting.json (timeline.shortcuts) から読む (resolve2 §5.5-2)
    # 登録したショートカットは文字入力欄にフォーカスがある間すべて無効化する
    # (回答 Q6 / _ShortcutGuard)。Backspace・Delete・矢印・Ctrl+Z なども対象で、
    # 入力欄では本来の文字編集として働く。
    def _register_shortcuts(self):
        keys = self.controller.cfg["shortcuts"]
        registered = []
        used = {}

        def bind(action, handler):
            sequence = keys.get(action, "")
            if not sequence:
                return None          # 空文字は「割り当てなし」(S など)
            key = QKeySequence(sequence)
            if key.isEmpty():
                _logger.warning(
                    "ショートカットのキー指定が不正です: timeline.shortcuts.%s=%r",
                    action, sequence)
                return None
            if sequence in used:
                _logger.warning(
                    "ショートカットのキーが重複しています: %r (%s と %s)",
                    sequence, used[sequence], action)
            used[sequence] = action
            shortcut = QShortcut(key, self)
            shortcut.activated.connect(handler)
            registered.append(shortcut)
            return shortcut

        # 編集
        bind("split", lambda: self.controller.split_at_playhead())
        bind("split_alt", lambda: self.controller.split_at_playhead())
        bind("ripple_trim_before", self._ripple_trim_before)
        bind("ripple_trim_after", self._ripple_trim_after)
        # 削除 (3 種)
        bind("delete", self._delete_default)
        bind("delete_alternate", self._delete_alternate)
        bind("delete_plain", self._delete_plain)
        # 再生
        bind("play_pause", self.preview.toggle_play)
        bind("play_fast_forward", self._play_fast_forward)
        bind("play_fast_backward", self._play_fast_backward)
        # 履歴・移動・ズーム
        bind("undo", self._undo)
        bind("redo", self._redo)
        bind("redo_alt", self._redo)
        bind("step_backward", lambda: self.controller.step_playhead(-1))
        bind("step_forward", lambda: self.controller.step_playhead(1))
        bind("go_start", lambda: self.controller.set_playhead(0.0))
        bind("go_end", lambda: self.controller.set_playhead(
            self.controller.timeline.duration_sec()))
        bind("zoom_in", lambda: self.controller.zoom_by(1.25))
        bind("zoom_in_alt", lambda: self.controller.zoom_by(1.25))
        bind("zoom_out", lambda: self.controller.zoom_by(1 / 1.25))

        self._shortcut_guard = _ShortcutGuard(registered, self)

    # Delete キー単独: 設定 (timeline.ripple_delete) に従う (既定 = リップル削除)
    def _delete_default(self):
        self.controller.delete_selected(ripple=self.controller.cfg["ripple_delete"])

    # Shift+Delete: 常にもう一方
    def _delete_alternate(self):
        self.controller.delete_selected(
            ripple=not self.controller.cfg["ripple_delete"])

    # BackSpace: 設定に関わらず「選択中のノードを削除するだけ」(R12)
    # 後続を詰めない = 跡は空白として残る。
    def _delete_plain(self):
        self.controller.delete_selected(ripple=False)

    # A: 再生ヘッドより前をリップル削除
    def _ripple_trim_before(self):
        self._run_ripple_trim("before")

    # D: 再生ヘッドより後ろをリップル削除
    def _ripple_trim_after(self):
        self._run_ripple_trim("after")

    # 対象が無ければ画面へ案内を出す (エラーにはしない / resolve2 §7)
    def _run_ripple_trim(self, side):
        if not self.controller.ripple_trim_to_playhead(side):
            self.preview.set_status("再生ヘッド上にクリップがありません")

    # E: 倍速再生 (トグル)
    def _play_fast_forward(self):
        self.preview.toggle_rate(+self.controller.cfg["preview"]["playback_rate"])

    # Q: 倍速逆再生 (トグル・音声なし)
    def _play_fast_backward(self):
        self.preview.toggle_rate(-self.controller.cfg["preview"]["playback_rate"])

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
        try:
            self.timeline_panel.shutdown()
        except Exception:  # noqa: BLE001 (同上)
            _logger.exception("Timeline の後片付けに失敗しました")
        super().done(code)


# 文字入力欄にフォーカスがある間、登録済みショートカットをすべて無効化する
# (resolve2 §5.5-3 / 回答 Q6:「入力欄にフォーカスがあるときはショートカットキーとしての
#  役割を果たさないようにしてほしい」)
# 防ぐ事故の例:
#   ・「わ」と打とうとして W でクリップが分割される
#   ・Space が打てない
#   ・BackSpace / Delete で文字ではなくクリップが消える
#   ・矢印キーで文字カーソルではなく再生ヘッドが動く
#   ・Ctrl+Z で入力ではなくタイムライン編集が取り消される
class _ShortcutGuard(QObject):

    _EDITORS = (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox)

    def __init__(self, shortcuts, parent=None):
        super().__init__(parent)
        self._shortcuts = list(shortcuts)
        application = QApplication.instance()
        if application is not None:
            application.focusChanged.connect(self._on_focus_changed)

    # キー入力を本来の意味で使う欄かどうか
    # コンボボックスは編集不可のものも含める。一覧から選ぶ操作では頭文字で
    # 項目を手繰るのが普通で (フォントは 400 件近くある)、W や A がショートカットへ
    # 吸われるとその文字で始まるフォントを選べなくなるため。
    def _is_editor(self, widget):
        return isinstance(widget, self._EDITORS) or isinstance(widget, QComboBox)

    def _on_focus_changed(self, _old, new):
        editing = self._is_editor(new)
        for shortcut in self._shortcuts:
            shortcut.setEnabled(not editing)


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

        # インスペクタは文字と入力欄を載せる面のため濃いめのガラスにする (resolve3 §3-4)。
        # 分割ウィンドウの一区画として周囲と接するため角丸は付けない (resolve4 E1)。
        theme.mark_panel(self, strong=True, rounded=False)

        self.title_label = QLabel("選択なし")
        # 太字は QSS ではなくフォントで指定する (インライン指定の撤去 / §5.3)
        title_font = QFont(self.title_label.font())
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        root.addWidget(self.title_label)

        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)
        theme.mark_note(self.info_label)
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

        # フォントは名前の直接入力ではなく一覧から選ぶ (設定画面・字幕編集画面と同じ形式)。
        # 先頭の空項目は「設定のフォントに従う」= font 未指定を表す。
        self.font_combo = QComboBox()
        self.font_combo.addItem(_FONT_DEFAULT_LABEL, "")
        for family in QFontDatabase.families():
            self.font_combo.addItem(family, family)
            # 各項目を自フォントで描画してプレビュー代わりにする (resolve16 §4.1)
            self.font_combo.setItemData(
                self.font_combo.count() - 1,
                QFont(family, _FONT_PREVIEW_POINT_SIZE), Qt.FontRole)
        # activated = 利用者が選び直したときだけ。currentIndexChanged だと
        # 一覧を矢印キーで見て回るだけで Undo 履歴が埋まってしまう。
        self.font_combo.activated.connect(self._on_font_changed)
        form.addRow("フォント", self.font_combo)

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
                self._set_font(clip.font or "")
                self.size_spin.setValue(float(clip.font_size or 0))
        finally:
            self._updating = False

    # フォントを選択状態にする。一覧に無い指定 (別 PC で作った案件・未導入のフォント)
    # は項目を足してから選ぶ。黙って「設定のフォント」へ戻すと指定が失われるため。
    def _set_font(self, family):
        if not family:
            self.font_combo.setCurrentIndex(0)
            return
        index = self.font_combo.findData(family)
        if index < 0:
            self.font_combo.addItem(family, family)
            index = self.font_combo.count() - 1
        self.font_combo.setCurrentIndex(index)

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
        self._controller.edit_subtitle(
            self._clip.id, font=self.font_combo.currentData() or "")

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
