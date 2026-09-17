# Timeline 編集画面 (docs/request/ver3/resolve.md §5.2)
# 上部にプレビュー、下部に Timeline を置いたモーダルダイアログ。
# 「決定」で Timeline の内容を反映した動画を書き出し、「キャンセル」で処理を中断する。
#
# 現行の字幕編集画面 (SubtitleEditorDialog) は削除せず残してあり、
# setting.json の timeline.enabled=false でそちらへ戻せる (R2)。
import os
import shutil
from datetime import datetime

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont, QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
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

from ...exceptions import TimelineError
from ...export import resolve_export
from ...settings.settings_window import save_settings
from ...timeline import commands, media_sidecar, project_io
from ...timeline.model import AudioClip, SubtitleClip
from ...utils.logger import get_logger
from ...version import __version__
from .. import project_thumbnail, theme
from ..color_field import ColorField
from ..subtitle_editor_dialog import RESOLVE_EXPORT_BUTTON_TEXT, run_resolve_export
from .preview_items import native_scale
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
    # project_path  : 保存先のプロジェクトファイル (未保存なら None / resolve7 §5.7)
    # created_at    : 読み込んだプロジェクトの初回作成時刻 (上書き保存で引き継ぐ)
    # mode          : "pipeline" (パイプラインの途中) / "resume" (保存済みを開き直した)
    def __init__(self, timeline, settings, work_dir, asr_audio_path=None, parent=None,
                 project_path=None, created_at=None, mode="pipeline"):
        super().__init__(parent)
        self.setWindowTitle("Timeline 編集")
        self._timeline = timeline
        self._settings = settings
        self._project_path = project_path or None
        self._created_at = created_at or None
        self._mode = str(mode or "pipeline")
        # 背景グラデーションを敷く (この上に Timeline の半透明な下地が乗る / resolve3 §3-2)
        theme.install_window_background(self)

        self.controller = TimelineController(timeline, settings, parent=self)
        ui_cfg = self.controller.cfg["ui"]
        self._project_cfg = self.controller.cfg["project"]
        # 保存 UI を出すか (アーカイブ用は素材が一時ファイルのため出さない / resolve7 §3-6)
        self._save_enabled = (self._project_save_enabled()
                              and bool(self._project_cfg["save_button"]))
        self.resize(ui_cfg["window_width"], ui_cfg["window_height"])

        self._build_ui(work_dir, asr_audio_path, ui_cfg)
        self._register_shortcuts()
        # 自動保存 (timeline.autosave_sec > 0 のときだけ動く / resolve7 §5.9)
        self._autosave_timer = None
        self._start_autosave()

        # 起動用音声の先読みを 1 回だけ行うための印 (resolve6 §5.9)
        self._prefetched = False

        # トラッキングぼかしの解析 (ver5 resolve2 §3.6 案 2)。
        # 画面を開いた直後に背後で走らせ、終わったらボタンを有効にする。
        self._blur_worker = None
        self._blur_analysis = None
        self._blur_cache_path = None
        self._work_dir = work_dir
        self._start_blur_analysis()

        self.controller.selection_changed.connect(self._on_selection_changed)
        self.controller.timeline_changed.connect(self._on_timeline_changed)
        # ぼかし対象の目印をプレビューへ出す (§5.7)。機能 OFF なら何も起きない。
        self.controller.playhead_moved.connect(self._update_blur_markers)
        self.controller.saved_state_changed.connect(self._update_window_title)
        self.preview.playing_changed.connect(self._on_playing_changed)
        # Timeline 側で実行できなかった操作の案内をプレビュー下へ出す
        self.timeline_panel.view.status_message.connect(self.preview.set_status)
        self._on_selection_changed(None)
        self._update_window_title()

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
        # 役割変更の結果 (トラック移動の可否) をプレビュー下へ出す (ver3 resolve11 §5.8)
        self.inspector.status_message.connect(self.preview.set_status)
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

        # 主要部 (プレビュー・インスペクタ・Timeline) を 1 つの器へまとめてから置く。
        # 派生画面がこの器を包んだり (_wrap_content)、上へ行を足したり
        # (content.layout().insertWidget(0, …)) できるようにするため (resolve5 §5.5)。
        self.content = QWidget()
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(self._splitter, 1)
        root.addWidget(self._wrap_content(self.content), 1)

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

        # プロジェクトの保存 (resolve7 §5.7)。保存できる画面 = 開ける画面に限る。
        self.save_button = None
        self.save_as_button = None
        if self._save_enabled:
            self.save_button = QPushButton("保存")
            self.save_button.setAutoDefault(False)
            self.save_button.setToolTip(
                "現在の Timeline をプロジェクトファイルへ保存します (Ctrl+S)\n"
                "保存したものは main_window の「編集の続き」から開き直せます")
            self.save_button.clicked.connect(lambda: self.save_project())
            button_row.addWidget(self.save_button)

            self.save_as_button = QPushButton("名前を付けて保存...")
            self.save_as_button.setAutoDefault(False)
            self.save_as_button.setToolTip("保存先を選んで保存します (Ctrl+Shift+S)")
            self.save_as_button.clicked.connect(lambda: self.save_project(ask=True))
            button_row.addWidget(self.save_as_button)

        # ぼかし指定 (ver5 resolve2 §5.6.1)。
        # blur.enabled が False のときは**ボタンを出さない** (R1 / R9)。
        self.blur_button = None
        if self._blur_enabled():
            self.blur_button = QPushButton("ぼかし指定...")
            self.blur_button.setAutoDefault(False)
            self.blur_button.setEnabled(False)
            self.blur_button.clicked.connect(self._open_blur_spec)
            button_row.addWidget(self.blur_button)

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
        self.decide_button = QPushButton(self._decide_button_text())
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
    # 派生画面向けのフック (resolve5 §5.5)
    # 既定は現行の画面と完全に同じ結果になる値を返す。
    # ------------------------------------------------------------------

    # 主要部を包む器を返す。アーカイブ用はここで QMainWindow に包み、
    # 採点グラフをドックとして載せる (resolve5 §3-7)。
    def _wrap_content(self, content):
        return content

    # 決定ボタンの文言。アーカイブ用は「完了（切り抜き＋字幕焼き込み）」にする。
    def _decide_button_text(self):
        return "決定"

    # プロジェクトの保存 UI を出すか。
    # resolve7 §3-6 ではアーカイブ用を False にしていたが、resolve9 で素材の復旧
    # (VOD からの切り直し + 音声サイドカー) を用意したため両方 True になった。
    def _project_save_enabled(self):
        return True

    # プロジェクトの種別 ("clip" / "archive" / resolve9 §3-4)。
    # 履歴の積み先 (recent / recent_archive) と一覧の絞り込みに使う。
    def _project_kind(self):
        return project_io.KIND_CLIP

    # 保存先の既定パス。アーカイブ用は元動画名の後ろへ ".archive" を挟み、
    # 同じ元動画から作ったクリップ用と名前が衝突しないようにする (resolve9 §3-5)。
    def _default_project_path(self):
        return project_io.default_project_path(
            self._settings,
            (self.controller.timeline.source or {}).get("input_path", ""))

    # ウィンドウタイトルの見出し (保存対応の画面はこの後ろへプロジェクト名を出す)
    def _title_base(self):
        return "Timeline 編集"

    # keep_media / keep_audio の対象になる素材を返す (resolve9 §5.4)。
    # 既定は本編素材 1 件。アーカイブ用は V1 が参照する全メディアを返す。
    def _media_to_copy(self):
        timeline = self.controller.timeline
        source = timeline.source or {}
        media = timeline.media_by_id(str(source.get("media_id", "") or ""))
        return [media] if media is not None else []

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
        # コピー＆ペースト (ver3 resolve10 §5.5)
        # 貼り付けは 1 種類だけ。Ctrl+Shift+V は割り当てない (rev2 R-1)。
        # bind() 経由で登録するため、字幕テキスト欄にフォーカスがある間は
        # _ShortcutGuard が無効化し、本来の文字コピー＆ペーストとして働く。
        bind("copy", self._copy)
        bind("paste", self._paste)
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
        # 保存 (resolve7 §5.7)。保存 UI を出さない画面では割り当てない。
        if self._save_enabled:
            bind("save", lambda: self.save_project())
            bind("save_as", lambda: self.save_project(ask=True))

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

    # Ctrl+C: 選択中のノードをコピーする (ver3 resolve10 §5.5)
    def _copy(self):
        count = self.controller.copy_selected()
        self.preview.set_status(
            f"{count} 件のノードをコピーしました" if count
            else "コピーするノードが選択されていません")

    # Ctrl+V: 再生ヘッド位置へ貼り付ける (干渉した既存ノードは右へずれる)
    def _paste(self):
        result = self.controller.paste()
        if result["empty"]:
            self.preview.set_status("コピーされたノードがありません")
            return
        if result["pasted"] == 0:
            self.preview.set_status(
                "貼り付けできませんでした (トラックのロックや素材の欠落を確認してください)")
            return
        message = f"{result['pasted']} 件を貼り付けました"
        if result["shifted"] > 0:
            message += f" (既存ノードを {result['shifted']:.2f} 秒ぶん右へ移動)"
        if result["skipped"]:
            message += f" ({result['skipped']} 件は貼れませんでした)"
        self.preview.set_status(message)

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

    # 画面が出た直後に先頭の音声を先読みしておく (resolve6 §5.9)。
    # 認識用音声を再利用できるクリップ用では何も起きない (既に手元にあるため)。
    # アーカイブ用は再利用が効かないので、ここで作っておくと ▶ の待ちが消える。
    def showEvent(self, event):
        super().showEvent(event)
        if self._prefetched:
            return
        self._prefetched = True
        try:
            self.preview.prefetch_initial()
        except Exception:  # noqa: BLE001 (先読みの失敗で画面を開けなくしない)
            _logger.exception("プレビュー音声の先読みに失敗しました (再生時に作り直します)")

    # ------------------------------------------------------------------
    # トラッキングぼかし (ver5 resolve2 §5.6.1)
    # ------------------------------------------------------------------

    # 機能が有効か (設定だけを見る。モデルの有無は解析を始めるときに確かめる)
    def _blur_enabled(self):
        from ...blur.config import is_enabled       # noqa: PLC0415 (機能 OFF なら読まない)

        return is_enabled(self._settings)

    # 解析を背後で始める。モデルが無ければボタンを無効のままにして理由を出す。
    def _start_blur_analysis(self):
        if self.blur_button is None:
            return
        from ...blur import models, store           # noqa: PLC0415
        from ...blur.config import config           # noqa: PLC0415
        from .blur_spec_dialog import BlurAnalysisWorker   # noqa: PLC0415

        cfg = config(self._settings)
        available, reason = models.availability(cfg)
        if not available:
            # モデルが見つからない: ボタンは出すが無効。理由をツールチップに出す (§5.6.1)
            self.blur_button.setEnabled(False)
            self.blur_button.setToolTip(reason)
            _logger.warning("ぼかし機能を無効にします: %s", reason)
            return
        if not cfg["analysis"]["auto_start"]:
            self.blur_button.setEnabled(True)
            self.blur_button.setToolTip("押すと解析を始めます")
            return

        self._blur_cache_path = store.cache_path_for(self._project_path, self._work_dir)
        self.blur_button.setText("ぼかし解析中… 0%")
        self._blur_worker = BlurAnalysisWorker(
            self.controller.timeline, self._settings, self._blur_cache_path, parent=self)
        self._blur_worker.progress.connect(self._on_blur_progress)
        self._blur_worker.finished_analysis.connect(self._on_blur_analysis_done)
        # プレビュー再生を邪魔しないよう優先度を下げる (§5.3.5)
        self._blur_worker.start(QThread.LowPriority)

    def _on_blur_progress(self, ratio, _label):
        if self.blur_button is not None:
            self.blur_button.setText(f"ぼかし解析中… {int(ratio * 100)}%")

    def _on_blur_analysis_done(self, analysis):
        self._blur_worker = None
        if self.blur_button is None:
            return
        self._blur_analysis = analysis
        if not analysis:
            self.blur_button.setText("ぼかし指定...")
            self.blur_button.setEnabled(False)
            self.blur_button.setToolTip(
                "ぼかしの解析ができなかったため、ぼかし指定は使えません。"
                "ログに理由が残っています。Timeline の編集と書き出しは続けられます。")
            return

        # 解析結果の在りかと指紋を指定へ書き留める。
        # これが無いと書き出しのときに解析結果を見つけられない (§5.4 prepare)。
        from ...blur import decisions as blur_decisions   # noqa: PLC0415
        from ...blur.config import config           # noqa: PLC0415

        # 前回と違う解析結果で、人物への明示指定がある = 人物の番号が変わって指定が別人を指すことがある
        # (ver5 resolve3 §10 #8)。解析のたびに作業を止めないよう、ボタンの表示で知らせる。
        previous = blur_decisions.load(self.controller.timeline)
        needs_review = bool(previous["identities"] and previous["fingerprint"]
                            and previous["fingerprint"] != analysis.get("fingerprint", ""))

        self.controller.execute(commands.SetBlurAnalysis(
            self._blur_cache_path, analysis.get("fingerprint", ""),
            project_path=self._project_path,
            default_policy=config(self._settings)["default_policy"]))

        self._update_blur_markers()
        count = len(analysis.get("identities", []))
        self.blur_button.setEnabled(True)
        if needs_review:
            _logger.info("ぼかしの解析をやり直したため、人物への指定の確認を案内します")
            self.blur_button.setText("ぼかし指定... (確認してください)")
            self.blur_button.setToolTip(
                f"検出した人物: {count} 人\n"
                "素材や設定が変わったため、ぼかしの解析をやり直しました。"
                "人物の番号が変わり、「ぼかす / ぼかさない」の指定が別の人を指している場合があります。"
                "見本画像を見て確認してください。")
        else:
            self.blur_button.setText("ぼかし指定...")
            self.blur_button.setToolTip(f"検出した人物: {count} 人")

    # ぼかし指定画面を開く (R4)
    def _open_blur_spec(self):
        if not self._blur_analysis:
            QMessageBox.information(
                self, "ぼかし指定",
                "ぼかしの解析がまだ終わっていません。しばらく待ってからお試しください。")
            return
        from .blur_spec_dialog import BlurSpecDialog     # noqa: PLC0415

        dialog = BlurSpecDialog(
            self.controller, self._blur_analysis, parent=self,
            cache_path=self._blur_cache_path)
        dialog.exec()
        self._update_history_buttons()
        # 指定画面で変えた指定・追加した枠をプレビューの目印へ反映する
        self._update_blur_markers()
        # 指定画面を開いた = 確認した。案内の表示を戻す
        self.blur_button.setText("ぼかし指定...")

    # プレビューへぼかし対象の目印を出す (§5.7)
    # 実際のぼかしはしない (1 枚ずつ取得しているため、画像処理を足すと重くなる)。
    # 何をぼかすかは指定画面・出力と同じ blur.plan で決める (ver5 resolve3 §5.9)。
    # 削除した枠・指定に無い領域の古い追従は出さない。
    def _update_blur_markers(self, _timeline_sec=None):
        if not self._blur_analysis or not hasattr(self.preview, "set_blur_markers"):
            return
        from ...blur import contour                       # noqa: PLC0415
        from ...blur import decisions as blur_decisions   # noqa: PLC0415
        from ...blur.config import config                 # noqa: PLC0415
        from ...blur.plan import ROLE_BLUR, BlurPlan      # noqa: PLC0415

        cfg = config(self._settings)
        if not cfg["preview_marker"]:
            return

        resolved = self.controller.source_at_playhead()
        if resolved is None:
            self.preview.set_blur_markers([])
            return
        media, source_sec = resolved

        timeline = self.controller.timeline
        plan = BlurPlan(timeline, self._blur_analysis, blur_decisions.load(timeline), cfg)
        markers = []
        for shape in plan.shapes_at(media.id, source_sec):
            if shape["role"] != ROLE_BLUR:
                continue
            relative = shape["silhouette"] if shape["silhouette"] is not None else shape["outline"]
            markers.append({
                "rect": shape["rect"],
                "polygon": (contour.to_absolute(relative, shape["rect"])
                            if relative is not None else None),
                "label": shape["label"] if shape["kind"] != "person" else "ぼかし",
            })
        self.preview.set_blur_markers(markers)

    # 解析スレッドを必ず止めてから閉じる (§5.3.5)
    def _stop_blur_analysis(self):
        worker = self._blur_worker
        if worker is None:
            return
        worker.cancel()
        worker.wait()
        self._blur_worker = None

    def _on_timeline_changed(self):
        self._update_history_buttons()
        self.inspector.refresh()
        self._update_window_title()

    def _on_selection_changed(self, clip):
        self.inspector.show_clip(clip)

    # 再生中は編集操作を受け付けない (状態の競合を避ける / §4-8)
    def _on_playing_changed(self, playing):
        self.timeline_panel.setEnabled(not playing)
        self.inspector.setEnabled(not playing)
        self.undo_button.setEnabled(not playing and self.controller.can_undo())
        self.redo_button.setEnabled(not playing and self.controller.can_redo())

    # ------------------------------------------------------------------
    # プロジェクトの保存 (resolve7 §5.7)
    # ------------------------------------------------------------------

    # 画面のタイトル。保存対応の画面だけ「プロジェクト名 + 未保存の印」を出す。
    def _update_window_title(self):
        if not self._save_enabled:
            return
        name = (os.path.basename(self._project_path) if self._project_path
                else "未保存のプロジェクト")
        mark = "*" if self.controller.is_modified() else ""
        self.setWindowTitle(f"{self._title_base()} — {name}{mark}")

    # 現在の Timeline をプロジェクトファイルへ書き出す
    # path 省略時は現在のプロジェクトパス、それも無ければ既定のパスを使う。
    # 戻り値: 保存できたら True (失敗しても画面は閉じない / 閉じる確認からも使うため)
    def save_project(self, path=None, ask=False):
        target = path or self._project_path or self._default_project_path()
        if ask:
            suffix = self.controller.cfg["project_suffix"]
            target, _ = QFileDialog.getSaveFileName(
                self, "Timeline を保存", target,
                f"Timeline プロジェクト (*{suffix});;すべてのファイル (*)")
            if not target:
                return False
        if self._created_at is None:
            # 初回保存の時刻を覚え、2 回目以降の上書きで作成時刻が動かないようにする
            self._created_at = datetime.now().isoformat(timespec="seconds")
        if self._project_cfg["keep_media"]:
            # 素材の複製は JSON を書く前に行う (複製先を指した状態で書き出すため)
            self._copy_media_beside_project(target)
        elif self._project_cfg["keep_audio"]:
            # 正規化済みの音声だけ残す (resolve9 §3-1 案D)。映像は開くときに
            # 元動画から切り直すため、これだけで復元が数分から数十秒になる。
            self._export_audio_sidecars(target)
        try:
            # 初回作成時刻は引き継ぐ (上書きのたびに created_at が変わらないように)
            project_io.save(self.controller.timeline, target,
                            generator=f"Stretheus {__version__}",
                            created_at=self._created_at, project_path=target)
        except (TimelineError, OSError) as e:
            QMessageBox.warning(self, "保存できません", str(e))
            return False
        self._project_path = target
        self.controller.mark_saved()
        self._update_window_title()
        self._remember_recent(target)
        # 本体へ保存できた時点で自動保存ファイルは役目を終える
        self._discard_autosave()
        # 一覧に出すサムネイル (失敗しても保存は成功扱い / resolve9 §5.15)
        project_thumbnail.ensure(target, self._settings, timeline=self.controller.timeline)
        self.preview.set_status(f"保存しました: {os.path.basename(target)}")
        return True

    # 正規化済み素材から音声だけを <プロジェクト名>.media/ へ残す (resolve9 §5.5-a)
    # 映像は -c:v copy で作られているため保存する必要が無く、音声だけで復元できる。
    # 失敗しても保存そのものは続ける (次に開くときは正規化のやり直しになるだけ)。
    def _export_audio_sidecars(self, project_path):
        media_list = [m for m in self._media_to_copy()
                      if m is not None and m.path and os.path.exists(m.path)]
        if not media_list:
            return
        folder = self._media_dir(project_path)
        ffmpeg_cfg = self._settings.get("ffmpeg", {})
        saved = 0
        try:
            os.makedirs(folder, exist_ok=True)
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self.preview.set_status("音声を保存しています…")
                for media in media_list:
                    dest = media_sidecar.sidecar_path(folder, media.id, ffmpeg_cfg)
                    # 既に同じ素材から作ったものがあれば作り直さない
                    if os.path.exists(dest) and os.path.getsize(dest) > 0:
                        saved += 1
                        continue
                    if media_sidecar.export_audio(media.path, dest, ffmpeg_cfg):
                        saved += 1
            finally:
                QApplication.restoreOverrideCursor()
        except OSError:
            _logger.exception("音声サイドカーの保存に失敗しました (保存は続行します)")
            return
        if saved:
            _logger.info("音声サイドカーを %d 件保存しました: %s", saved, folder)

    # 本編素材 (正規化後の中間ファイル) をプロジェクトの隣へ複製する
    # (resolve7 §3-5 案B / timeline.project.keep_media)
    # 中間ファイルは実行の終わりに消えるため、複製しておくと次に開くときに
    # 作り直し (1 パスぶんの待ち時間) が要らなくなる。動画 1 本ぶんのディスクを使う。
    # 元動画・OP/ED・追加画像は利用者のファイルで消えないため複製しない。
    def _copy_media_beside_project(self, project_path):
        timeline = self.controller.timeline
        source = dict(timeline.source or {})
        input_path = str(source.get("input_path", "") or "")
        folder = self._media_dir(project_path)
        body_id = str(source.get("media_id", "") or "")

        for media in self._media_to_copy():
            if media is None or not media.path or not os.path.exists(media.path):
                continue
            if input_path and os.path.abspath(media.path) == os.path.abspath(input_path):
                continue        # 元動画そのもの = 消えないため複製しない
            # 複製先の名前にメディア ID を付ける (アーカイブ用は全クリップが
            # normalized.mp4 という同名のため、そのままでは衝突する / resolve9 §5.4)
            dest = os.path.join(folder, f"{media.id}_{os.path.basename(media.path)}")
            try:
                os.makedirs(folder, exist_ok=True)
                # 同じ内容が既にあるなら複製し直さない (保存のたびに数百 MB を書かない)
                if not (os.path.exists(dest)
                        and os.path.getsize(dest) == os.path.getsize(media.path)):
                    QApplication.setOverrideCursor(Qt.WaitCursor)
                    try:
                        self.preview.set_status("素材を複製しています…")
                        shutil.copy2(media.path, dest)
                    finally:
                        QApplication.restoreOverrideCursor()
                    _logger.info("素材をプロジェクトの隣へ複製しました: %s", dest)
            except OSError:
                # 複製に失敗しても保存そのものは続ける (元のパスを指したまま書き出す)
                _logger.exception("素材の複製に失敗しました (保存は続行します)")
                continue
            media.path = dest
            if media.id == body_id:
                source["media_path"] = dest
                timeline.source = source

    # 複製先フォルダ (<プロジェクト名> + timeline.project.media_dir_suffix)
    def _media_dir(self, project_path):
        return project_io.media_dir_path(
            project_path, self.controller.cfg["project_suffix"],
            self._project_cfg["media_dir_suffix"])

    # ------------------------------------------------------------------
    # 自動保存 (resolve7 §5.9 / timeline.autosave_sec)
    # ------------------------------------------------------------------
    # 既定は autosave_sec=0 (無効) のため、設定しなければ従来どおり何も起きない。
    # 書き先は本体とは別ファイル (<プロジェクト>.autosave.json) で、本体は上書きしない。
    # 次に開くとき本体より新しい autosave があれば main_window が復元を尋ねる。

    def _start_autosave(self):
        interval = int(self.controller.cfg["autosave_sec"] or 0)
        if not self._save_enabled or interval <= 0:
            return
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(interval * 1000)
        self._autosave_timer.timeout.connect(self._on_autosave)
        self._autosave_timer.start()
        _logger.info("Timeline の自動保存を %d 秒ごとに行います", interval)

    # 自動保存ファイルのパス (保存先が決まっていなければ既定のパスから組む)
    def _autosave_path(self):
        base = self._project_path or project_io.default_project_path(
            self._settings, (self.controller.timeline.source or {}).get("input_path", ""))
        return project_io.autosave_path(base, self._project_cfg["autosave_suffix"])

    # 未保存の編集があるときだけ書く
    def _on_autosave(self):
        if not self.controller.is_modified():
            return
        path = self._autosave_path()
        if not path:
            return
        try:
            project_io.save(self.controller.timeline, path,
                            generator=f"Stretheus {__version__}",
                            created_at=self._created_at,
                            project_path=self._project_path or path)
        except (TimelineError, OSError):
            # 自動保存の失敗で編集を止めない (次の周期で作り直す)
            _logger.warning("Timeline の自動保存に失敗しました (編集は続行します)")

    # 自動保存ファイルを片づける (本体へ保存できた / 決定した時点で役目が終わる)
    def _discard_autosave(self):
        path = self._autosave_path()
        if not path or not os.path.exists(path):
            return
        try:
            os.remove(path)
        except OSError:
            _logger.warning("自動保存ファイルを削除できませんでした: %s", path)

    # 最近使ったプロジェクトを setting.json へ積む (main_window の「編集の続き」に出す)
    # 種別ごとに積み先を分ける (クリップ用 = recent / アーカイブ用 = recent_archive)。
    # タブごとの一覧・コンボにその種別のものだけを並べるため (resolve9 §3-4)。
    def _remember_recent(self, path):
        try:
            project_cfg = self._settings.setdefault("timeline", {}).setdefault("project", {})
            key = ("recent_archive" if self._project_kind() == project_io.KIND_ARCHIVE
                   else "recent")
            recent = [p for p in (project_cfg.get(key) or []) if p and p != path]
            recent.insert(0, path)
            limit = max(int(self._project_cfg["recent_limit"]), 1)
            project_cfg[key] = recent[:limit]
            save_settings(self._settings)
        except Exception:  # noqa: BLE001 (履歴の保存に失敗しても編集は続けられる)
            _logger.warning("最近使ったプロジェクトの記録に失敗しました (処理は続行します)")

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

    # 編集結果と保存先をまとめて返す (resolve7 §5.10)。
    # 「名前を付けて保存」で保存先を変えていた場合、確定後の保存もそちらへ行かせる。
    def result_payload(self):
        return {"timeline": self.controller.timeline,
                "project_path": self._project_path}

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
        # 決定後はパイプラインが本体を上書き保存するため、自動保存は不要になる
        if self._save_enabled:
            self._discard_autosave()
        self._stop_blur_analysis()
        super().accept()

    # 編集済みのまま閉じようとしたら確認する (誤操作でパイプラインを中断させない)
    # × ボタンも QDialog の既定で reject() に落ちるため同じ経路を通る。
    def reject(self):
        if not self._confirm_close():
            return
        self._stop_blur_analysis()
        super().reject()

    # 閉じてよければ True。保存できる画面では 3 択で確認する (resolve7 §5.7 / C5)。
    def _confirm_close(self):
        if not self._save_enabled:
            # 保存できない画面は従来どおりの 2 択 (破棄しますか)
            if not self.controller.is_dirty():
                return True
            answer = QMessageBox.question(
                self, "編集を破棄しますか",
                "Timeline の編集内容が破棄され、処理も中断されます。よろしいですか?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            return answer == QMessageBox.Yes
        if not self._project_cfg["confirm_on_close"] or not self.controller.is_modified():
            return True

        box = QMessageBox(self)
        box.setWindowTitle("保存していない編集があります")
        box.setText("Timeline に保存していない編集があります。")
        # パイプライン実行中は閉じると処理も止まる。再編集中は保存済みファイルが残る。
        box.setInformativeText(
            "閉じると編集内容は失われ、処理も中断されます。" if self._mode == "pipeline"
            else "閉じると保存していない編集は失われます。")
        save = box.addButton("保存して閉じる", QMessageBox.AcceptRole)
        box.addButton("保存せずに閉じる", QMessageBox.DestructiveRole)
        cancel = box.addButton("編集に戻る", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)      # 誤操作で消えないよう既定は「戻る」
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel:
            return False
        if clicked is save:
            return self.save_project()    # 保存に失敗したら閉じない
        return True

    # 終了時に再生を止め、スレッドと一時ファイルを片づける (§10)
    def done(self, code):
        if self._autosave_timer is not None:
            self._autosave_timer.stop()
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

    # 操作の結果をプレビュー下のステータスへ出す (Timeline の view と同じ規約)
    status_message = Signal(str)

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

        # 文字色・縁の色は「このクリップだけの上書き」(resolve6 §3-1)。
        # 空欄なら設定の役割色 (配信者/サブ/コメント) に従う。
        # 複数選択しているときは選択中の字幕すべてへ適用する (resolve6 §3-10)。
        swatch_width = int(self._controller.cfg["ui"]["subtitle_color_swatch_width_px"])
        self.color_field = ColorField(with_alpha=False, swatch_width_px=swatch_width)
        self.color_field.setToolTip(
            "選択中の字幕の文字色です。空欄にすると設定の役割色に戻ります\n"
            "複数選択しているときは選択中すべてに適用されます")
        self.color_field.color_committed.connect(self._on_color_changed)
        form.addRow("文字色", self.color_field)

        # 縁は ASS 形式 (&HAABBGGRR) のため透明度も指定できる
        self.outline_color_field = ColorField(
            with_alpha=True, swatch_width_px=swatch_width)
        self.outline_color_field.setToolTip(
            "選択中の字幕の縁 (アウトライン) の色です。空欄にすると設定の役割色に戻ります\n"
            "複数選択しているときは選択中すべてに適用されます")
        self.outline_color_field.color_committed.connect(self._on_outline_color_changed)
        form.addRow("縁の色", self.outline_color_field)

        # 複数選択のときだけ出す案内 (単一選択の見た目を変えないため既定は非表示)
        self.multi_note_label = QLabel("")
        self.multi_note_label.setWordWrap(True)
        theme.mark_note(self.multi_note_label)
        self.multi_note_label.setVisible(False)
        form.addRow("", self.multi_note_label)
        root.addWidget(self.subtitle_widget)

        # オーバーレイ (画像・動画) 用の編集欄 (resolve7 §5.5)。
        # 字幕用と同じ作りの「もう 1 つの器」として持ち、表示・非表示を切り替える。
        overlay_cfg = self._controller.cfg["overlay"]
        self.overlay_widget = QWidget()
        overlay_form = QFormLayout(self.overlay_widget)
        overlay_form.setContentsMargins(0, 6, 0, 0)

        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setDecimals(1)
        self.scale_spin.setRange(float(overlay_cfg["min_scale"]) * 100.0,
                                 float(overlay_cfg["max_scale"]) * 100.0)
        self.scale_spin.setSingleStep(float(overlay_cfg["scale_step_percent"]))
        self.scale_spin.setSuffix(" %")
        self.scale_spin.setToolTip(
            "キャンバス幅に対する大きさです (100% = 画面の横幅いっぱい)\n"
            "縦横比は常に保たれます")
        # 1 打鍵ごとにコマンドを積まないよう、確定したときだけ反映する
        self.scale_spin.editingFinished.connect(self._on_scale_changed)
        overlay_form.addRow("大きさ", self.scale_spin)

        self.scale_note_label = QLabel("")
        theme.mark_note(self.scale_note_label)
        overlay_form.addRow("", self.scale_note_label)

        scale_buttons = QWidget()
        scale_row = QHBoxLayout(scale_buttons)
        scale_row.setContentsMargins(0, 0, 0, 0)
        self.native_size_button = QPushButton("原寸")
        self.native_size_button.setToolTip("素材のピクセル数どおりの大きさにします")
        self.native_size_button.clicked.connect(self._apply_native_scale)
        scale_row.addWidget(self.native_size_button)
        self.fit_width_button = QPushButton("画面幅に合わせる")
        self.fit_width_button.setToolTip("キャンバスの横幅いっぱい (100%) にします")
        self.fit_width_button.clicked.connect(
            lambda: self._apply_scale(1.0))
        scale_row.addWidget(self.fit_width_button)
        overlay_form.addRow("", scale_buttons)
        root.addWidget(self.overlay_widget)

        self.reset_position_button = QPushButton("位置を既定へ戻す")
        self.reset_position_button.setToolTip(
            "ドラッグで動かした位置を捨てて、設定の配置・余白に従わせます")
        self.reset_position_button.clicked.connect(self._reset_position)
        root.addWidget(self.reset_position_button)

        root.addStretch(1)
        self.subtitle_widget.setVisible(False)
        self.overlay_widget.setVisible(False)
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
                self.overlay_widget.setVisible(False)
                self.reset_position_button.setVisible(False)
                return
            is_subtitle = isinstance(clip, SubtitleClip)
            self.subtitle_widget.setVisible(is_subtitle)
            is_overlay = self._is_overlay_clip(clip)
            self.overlay_widget.setVisible(is_overlay)
            if is_overlay:
                self._show_overlay_scale(clip)
            self.reset_position_button.setVisible(True)
            self.title_label.setText(self._title_for(clip))
            self.info_label.setText(self._info_for(clip))
            if is_subtitle:
                self.text_edit.setPlainText(clip.text.replace("\\N", "\n"))
                index = self.role_combo.findData(clip.role)
                self.role_combo.setCurrentIndex(max(index, 0))
                self._set_font(clip.font or "")
                self.size_spin.setValue(float(clip.font_size or 0))
                # 色は先頭 1 件の値を出す (複数選択で値が違っても「複数値」は持たない)
                self.color_field.set_value(clip.color)
                self.outline_color_field.set_value(clip.outline_color)
                self._update_multi_note()
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

    # 選択中の字幕クリップ ID を返す (色の一括適用の対象 / resolve6 §3-10)
    # 字幕以外 (映像・音声クリップ) が混ざっていてもここで振り分ける。
    def _selected_subtitle_ids(self):
        timeline = self._controller.timeline
        ids = [i for i in self._controller.selected_ids()
               if isinstance(timeline.clip_by_id(i), SubtitleClip)]
        # 選択が空のまま表示だけしている場合 (プレビュー側の選択など) に備える
        if not ids and isinstance(self._clip, SubtitleClip):
            ids = [self._clip.id]
        return ids

    # 複数選択のときだけ「色は選択中すべてに効く」ことを案内する
    def _update_multi_note(self):
        count = len(self._selected_subtitle_ids())
        self.multi_note_label.setVisible(count > 1)
        if count > 1:
            self.multi_note_label.setText(
                f"※ 文字色・縁の色は選択中の {count} 件すべてに適用されます"
                "（字幕・役割・フォント・サイズはこの 1 件のみ）")

    def _title_for(self, clip):
        if isinstance(clip, SubtitleClip):
            count = len(self._selected_subtitle_ids())
            return "字幕クリップ" if count <= 1 else f"字幕クリップ（{count} 件選択）"
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
                lines.append(f"素材 {os.path.basename(media.path)}")
            lines.append(f"素材内 {clip.source_in:.2f}〜{clip.source_out:.2f} 秒")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 大きさ (オーバーレイ / resolve7 §5.5)
    # ------------------------------------------------------------------

    # 「大きさ」欄を出す対象か。
    # 対象は オーバーレイの映像・画像クリップ だけで、
    #   ・字幕      … 大きさはフォントサイズで表す (二重の指定手段を作らない)
    #   ・音声      … 見た目を持たない
    #   ・ベース映像 (本編・OP・ED) … renderer が transform を見ないため
    #                                 変えても出力が変わらない = 嘘になる
    # は出さない。
    def _is_overlay_clip(self, clip):
        if clip is None or isinstance(clip, (SubtitleClip, AudioClip)):
            return False
        if not hasattr(clip, "transform"):
            return False
        timeline = self._controller.timeline
        track = timeline.track_of_clip(clip.id)
        base = timeline.base_video_track()
        if track is None:
            return False
        return base is None or track.id != base.id

    # 現在の拡大率を欄へ出し、実寸の目安を添える
    def _show_overlay_scale(self, clip):
        scale = float(clip.transform.scale or 1.0)
        self.scale_spin.setValue(scale * 100.0)
        timeline = self._controller.timeline
        media = timeline.media_by_id(clip.media_id)
        width_px = int(round(timeline.width * scale))
        if media is not None and media.width and media.height:
            height_px = int(round(width_px * float(media.height) / float(media.width)))
            self.scale_note_label.setText(f"{width_px} × {height_px} px 相当")
        else:
            self.scale_note_label.setText(f"幅 {width_px} px 相当")
        self.native_size_button.setEnabled(native_scale(timeline, clip) is not None)

    def _on_scale_changed(self):
        if self._updating or not self._is_overlay_clip(self._clip):
            return
        self._apply_scale(self.scale_spin.value() / 100.0)

    def _apply_scale(self, scale):
        if self._clip is None or scale is None:
            return
        self._controller.resize_overlay(self._clip.id, scale)

    # 素材のピクセル数どおりの大きさへ戻す
    def _apply_native_scale(self):
        if self._clip is None:
            return
        self._apply_scale(native_scale(self._controller.timeline, self._clip))

    # ------------------------------------------------------------------
    # 編集
    # ------------------------------------------------------------------

    def _on_text_changed(self):
        if self._updating or not isinstance(self._clip, SubtitleClip):
            return
        text = self.text_edit.toPlainText().replace("\n", "\\N")
        self._controller.edit_subtitle(self._clip.id, text=text)

    # 役割を変えるとコメント用トラック (S2) への移動も一緒に行う (ver3 resolve11 §5.8)。
    # 移動できなくても役割の変更は通る (出力は役割が決めるため見た目は正しく出る)。
    def _on_role_changed(self):
        if self._updating or not isinstance(self._clip, SubtitleClip):
            return
        result = self._controller.change_subtitle_role(
            self._clip.id, self.role_combo.currentData())
        if not result["changed"]:
            return
        if result["moved"]:
            track = self._controller.timeline.track_of_clip(self._clip.id)
            self.status_message.emit(
                f"コメントトラック({track.id})へ移しました" if track
                else "コメントトラックへ移しました")
        elif result["blocked"]:
            self.status_message.emit(
                "同じ時間にコメントがあるため、役割だけ変更しました")

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

    # 色は選択中の字幕すべてへ適用する (1 コマンド = Undo 1 手 / resolve6 §3-10)
    def _on_color_changed(self, value):
        if self._updating:
            return
        ids = self._selected_subtitle_ids()
        if ids:
            self._controller.edit_subtitles(ids, color=value)

    def _on_outline_color_changed(self, value):
        if self._updating:
            return
        ids = self._selected_subtitle_ids()
        if ids:
            self._controller.edit_subtitles(ids, outline_color=value)

    # ドラッグで付いた位置指定を捨てる (字幕は設定の配置へ戻る)
    def _reset_position(self):
        if self._clip is None:
            return
        self._controller.move_overlay(self._clip.id, None, None)
