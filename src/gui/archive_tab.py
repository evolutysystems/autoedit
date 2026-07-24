# アーカイブ切り抜き用タブ (flow17 R1: 方式A中核採点 + 切り抜き+焼き込み)
# ローカル mp4 を入力 → 採点開始 → TOP5 を簡易確認 → 完了で切り抜き+字幕焼き込み。
# 取得(Twitch)・採点グラフ・プレビュー・任意スコアラは後続リリース(R2/R3/R4+)で追加する。
import os

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..archive import clip_writer, pipeline
from ..settings.settings_window import (
    load_settings,
    register_fonts_in_dir,
    resolve_fonts_dir,
)
from ..utils.logger import get_logger
from .archive_result_dialog import ArchiveResultDialog

_logger = get_logger(__name__)

# 稼働証明スピナー (main_window と同種。循環 import を避けるため本ファイルで定義)
_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_SPINNER_INTERVAL_MS = 120

# 入力に使う動画フィルタ (main_window と整合)
_VIDEO_FILE_FILTER = "動画ファイル (*.mp4 *.mov *.avi *.mkv *.flv *.wmv);;すべてのファイル (*)"


# 採点(analyze)をワーカースレッドで実行する
class ArchiveAnalyzeWorker(QThread):
    progress = Signal(float, str)
    finished_ok = Signal(object)   # analyze の戻り値 dict
    failed = Signal(str)

    def __init__(self, input_path, settings, parent=None):
        super().__init__(parent)
        self._input_path = input_path
        self._settings = settings

    def run(self):
        try:
            result = pipeline.analyze(self._input_path, self._settings,
                                      progress_cb=self._emit)
            self.finished_ok.emit(result)
        except Exception as e:  # noqa: BLE001 (GUI へ集約通知)
            _logger.exception("アーカイブ採点に失敗")
            self.failed.emit(str(e))

    def _emit(self, ratio, label):
        self.progress.emit(float(ratio), str(label))


# 切り抜き+字幕焼き込みをワーカースレッドで実行する
class ArchiveClipWorker(QThread):
    progress = Signal(float, str)
    finished_ok = Signal(object)   # 出力パスのリスト
    failed = Signal(str)

    def __init__(self, input_path, settings, clips, review_callback=None, parent=None):
        super().__init__(parent)
        self._input_path = input_path
        self._settings = settings
        self._clips = clips
        self._review_callback = review_callback

    def run(self):
        try:
            outputs = clip_writer.write_clips(
                self._input_path, self._settings, self._clips,
                progress_cb=self._emit, review_callback=self._review_callback)
            self.finished_ok.emit(outputs)
        except Exception as e:  # noqa: BLE001 (GUI へ集約通知)
            _logger.exception("切り抜き+焼き込みに失敗")
            self.failed.emit(str(e))

    def _emit(self, ratio, label):
        self.progress.emit(float(ratio), str(label))


# アーカイブ切り抜き用タブ
class ArchiveTabWidget(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = load_settings()
        self._enabled = bool(self._settings.get("archive", {}).get("enabled", False))
        self._analyze_worker = None
        self._clip_worker = None
        self._review_bridge = None  # テロップ編集画面の橋渡し (GC 防止のため保持)
        self._pending_input = None  # 採点対象の入力パス (採点→切り抜きで引き継ぐ)
        if self._enabled:
            self._build_ui()
        else:
            self._build_disabled_ui()

    # 無効時 (archive.enabled=false) は準備中表示にする
    def _build_disabled_ui(self):
        root = QVBoxLayout(self)
        root.addStretch(1)
        note = QLabel("アーカイブ切り抜きは無効です (設定で有効化してください)。")
        note.setAlignment(Qt.AlignCenter)
        root.addWidget(note)
        root.addStretch(1)

    # 機能 UI を構築する
    def _build_ui(self):
        root = QVBoxLayout(self)

        root.addWidget(QLabel(
            "ローカル動画を採点し、上位の見どころを切り抜いて字幕を焼き込みます。\n"
            "採点方式: A (音量・無音の中核採点)。Twitch 取得は今後のリリースで対応します。"
        ))

        # 入力動画 (ローカル mp4)
        input_row = QHBoxLayout()
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("採点する動画ファイルを選択、または「参照...」")
        input_row.addWidget(self.input_edit)
        self.browse_button = QPushButton("参照...")
        self.browse_button.clicked.connect(self._on_browse)
        input_row.addWidget(self.browse_button)
        root.addLayout(input_row)

        # 採点開始
        self.analyze_button = QPushButton("採点開始")
        self.analyze_button.clicked.connect(self._on_analyze)
        root.addWidget(self.analyze_button)

        # 進捗 + ステータス
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        root.addWidget(self.progress_bar)
        self.status_label = QLabel("待機中")
        root.addWidget(self.status_label)

        # 稼働スピナー
        self._spinner_index = 0
        self._spinner_message = "待機中"
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(_SPINNER_INTERVAL_MS)
        self._spinner_timer.timeout.connect(self._tick_spinner)

    def _tick_spinner(self):
        frame = _SPINNER_FRAMES[self._spinner_index % len(_SPINNER_FRAMES)]
        self._spinner_index += 1
        self.status_label.setText(f"{frame} {self._spinner_message}")

    def _on_browse(self):
        start_dir = self._settings.get("general", {}).get("video_directory", "")
        path, _ = QFileDialog.getOpenFileName(self, "採点する動画を選択", start_dir, _VIDEO_FILE_FILTER)
        if path:
            self.input_edit.setText(path)

    # 採点開始
    def _on_analyze(self):
        input_path = self.input_edit.text().strip()
        if not input_path or not os.path.exists(input_path):
            QMessageBox.warning(self, "入力エラー", "存在する動画ファイルを指定してください。")
            return

        self._settings = load_settings()  # 最新化
        self._pending_input = input_path

        self._analyze_worker = ArchiveAnalyzeWorker(input_path, self._settings, parent=self)
        self._analyze_worker.progress.connect(self._on_progress)
        self._analyze_worker.finished_ok.connect(self._on_analyze_done)
        self._analyze_worker.failed.connect(self._on_failed)

        self._spinner_message = "採点準備中…"
        self._set_running(True)
        self._analyze_worker.start()

    # 採点完了 → 結果ダイアログ → 完了で切り抜き
    def _on_analyze_done(self, result):
        self._set_running(False)
        clips = result.get("clips", []) if isinstance(result, dict) else []
        if not clips:
            QMessageBox.information(self, "採点結果", "切り抜き候補が見つかりませんでした。")
            self.status_label.setText("候補なし")
            return

        dialog = ArchiveResultDialog(clips, parent=self)
        if dialog.exec() != ArchiveResultDialog.Accepted:
            self.status_label.setText("キャンセルしました")
            return

        confirmed = [c for c in dialog.result_clips() if c.get("use", True)]
        if not confirmed:
            QMessageBox.information(self, "切り抜き", "使用する候補が選択されていません。")
            self.status_label.setText("待機中")
            return

        # 追加フォント(settings/fonts)を Qt へ登録し編集画面の一覧へ反映する (resolve16 §4.2)
        register_fonts_in_dir(resolve_fonts_dir(self._settings))
        subtitle_cfg = self._settings.get("subtitle", {})

        # クリップごとに現行クリップ用と同じテロップ編集画面を出す橋渡し (resolve18 §4.3)
        # 循環 import を避けるため main_window の既存ブリッジを遅延 import で流用する。
        # 音量ダイアログは出さない (回答①) ため VolumeThresholdBridge は使わない。
        from .main_window import SubtitleReviewBridge
        self._review_bridge = SubtitleReviewBridge(
            parent_window=self,
            default_font=subtitle_cfg.get("font_family", ""),
            default_size=subtitle_cfg.get("font_size", None),
            font_families=list(QFontDatabase.families()),
        )

        # 切り抜き+編集+焼き込み+結合を開始
        self._clip_worker = ArchiveClipWorker(
            self._pending_input, self._settings, confirmed,
            review_callback=self._review_bridge, parent=self)
        self._clip_worker.progress.connect(self._on_progress)
        self._clip_worker.finished_ok.connect(self._on_clip_done)
        self._clip_worker.failed.connect(self._on_failed)
        self._spinner_message = "切り抜き開始…"
        self._set_running(True)
        self._clip_worker.start()

    # 切り抜き完了 (結合1本を出力)
    def _on_clip_done(self, outputs):
        self._set_running(False)
        self.progress_bar.setValue(100)
        if not outputs:
            self.status_label.setText("出力なし (全てスキップ)")
            QMessageBox.information(self, "完了", "出力するクリップがありませんでした。")
            return
        self.status_label.setText(f"完了: {outputs[-1]}")
        joined = "\n".join(outputs)
        QMessageBox.information(self, "完了", f"動画を出力しました。\n{joined}")

    # 異常終了
    def _on_failed(self, message):
        self._set_running(False)
        self.status_label.setText("エラーで停止しました")
        QMessageBox.critical(self, "エラー", f"処理に失敗しました。\n{message}")

    def _on_progress(self, ratio, label):
        self.progress_bar.setValue(int(ratio * 100))
        self._spinner_message = label

    def _set_running(self, running):
        self.analyze_button.setEnabled(not running)
        self.browse_button.setEnabled(not running)
        self.input_edit.setEnabled(not running)
        if running:
            self._spinner_timer.start()
        else:
            self._spinner_timer.stop()
