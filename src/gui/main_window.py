# GUI 専用ランチャ (字幕編集画面を伴うパイプライン実行)
# resolve3 §7.3・§10-1 に対応
# 入力動画の選択 / 実行ボタン / 進捗表示 / 字幕編集画面の橋渡しを担う。
# パイプラインはワーカースレッドで実行し、フルテロップ後にメインスレッドで
# 字幕編集画面 (モーダル) を開く。
import os
import sys
import threading

# パッケージ実行・単独スクリプト実行の両対応 (main.py と同方針)
if __package__ is None or __package__ == "":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from src.exceptions import PipelineCancelled
    from src.gui.subtitle_editor_dialog import SubtitleEditorDialog
    from src.gui.volume_threshold_dialog import VolumeThresholdDialog
    from src.pipeline.pipeline_runner import run_pipeline
    from src.settings.settings_window import SettingsWindow, load_settings
    from src.utils.logger import get_logger
else:
    from ..exceptions import PipelineCancelled
    from ..pipeline.pipeline_runner import run_pipeline
    from ..settings.settings_window import SettingsWindow, load_settings
    from ..utils.logger import get_logger
    from .subtitle_editor_dialog import SubtitleEditorDialog
    from .volume_threshold_dialog import VolumeThresholdDialog

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
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

_logger = get_logger(__name__)

# 動画ファイルフィルタ (settings_window と整合)
_VIDEO_FILE_FILTER = "動画ファイル (*.mp4 *.mov *.avi *.mkv *.flv *.wmv);;すべてのファイル (*)"

# 稼働証明スピナーのコマ (Claude Code 風の回転記号) と更新間隔
# 表示崩れ環境向けに ASCII 版 ["|", "/", "-", "\\"] へ差し替え可能
_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_SPINNER_INTERVAL_MS = 120


# ワーカースレッドとメインスレッドの橋渡し (字幕編集画面の表示)
# ワーカースレッドから callable として呼ばれ、メインスレッドでダイアログを開く。
# resolve3 §7.3 の SubtitleReviewBridge に対応。
class SubtitleReviewBridge(QObject):

    # メインスレッドへダイアログ表示を依頼するシグナル (items を渡す)
    review_requested = Signal(list)

    def __init__(self, parent_window=None):
        super().__init__()
        self._parent_window = parent_window
        # ワーカースレッドを待機させるためのイベントと結果共有領域
        self._event = threading.Event()
        self._result = None
        # メインスレッドのスロットでダイアログを開く (キュー接続で必ずメインスレッド実行)
        self.review_requested.connect(self._on_review_requested, Qt.QueuedConnection)

    # ワーカースレッドから呼ばれる (run_pipeline の subtitle_review_callback)
    # items: [{"start","end","text","use"}] / 戻り値: 編集結果 or None(キャンセル)
    def __call__(self, items):
        self._event.clear()
        self._result = None
        # メインスレッドへ表示依頼 (キュー接続のため呼び出しは即時返る)
        self.review_requested.emit(items)
        # ユーザー操作が終わるまでワーカースレッドをブロックする
        self._event.wait()
        return self._result

    # メインスレッドで実行されるスロット: ダイアログを開いて結果を共有領域へ格納
    def _on_review_requested(self, items):
        try:
            dialog = SubtitleEditorDialog(items, parent=self._parent_window)
            if dialog.exec() == SubtitleEditorDialog.Accepted:
                self._result = dialog.result_items()
            else:
                # キャンセル / × クローズ → None (中断扱い)
                self._result = None
        finally:
            # 例外有無に関わらずワーカーを再開させる (デッドロック防止)
            self._event.set()


# 音量解析の閾値確認ダイアログをワーカースレッド→メインスレッドで橋渡しする
# resolve7 §5.3 に対応。SubtitleReviewBridge と同じ機構。
class VolumeThresholdBridge(QObject):

    # メインスレッドへダイアログ表示を依頼するシグナル (解析情報 dict を渡す)
    analysis_requested = Signal(object)

    def __init__(self, parent_window=None):
        super().__init__()
        self._parent_window = parent_window
        # ワーカースレッドを待機させるためのイベントと結果共有領域
        self._event = threading.Event()
        self._result = None
        self.analysis_requested.connect(self._on_requested, Qt.QueuedConnection)

    # ワーカースレッドから呼ばれる (run_pipeline の volume_analysis_callback)
    # info: {"initial_db", "measured_db", "region_count"}
    # 戻り値: 確定dB(int) / None(変更しない)
    def __call__(self, info):
        self._event.clear()
        self._result = None
        self.analysis_requested.emit(info)
        # ユーザー操作が終わるまでワーカースレッドをブロックする
        self._event.wait()
        return self._result

    # メインスレッドで実行されるスロット: ダイアログを開いて結果を共有領域へ格納
    def _on_requested(self, info):
        try:
            dialog = VolumeThresholdDialog(
                initial_db=info.get("initial_db", 0),
                measured_db=info.get("measured_db"),
                region_count=info.get("region_count", 0),
                parent=self._parent_window,
            )
            if dialog.exec() == VolumeThresholdDialog.Accepted:
                self._result = dialog.result_value()  # 確定dB(int)
            else:
                # 変更しない / × クローズ → None (既定値で続行)
                self._result = None
        finally:
            # 例外有無に関わらずワーカーを再開させる (デッドロック防止)
            self._event.set()


# 失敗メッセージを整形する (resolve 20260630 対策C)。
# FFmpegError 等が stderr 末尾 (stderr_tail) を持つ場合は実エラー行の要点を併記し、
# 「returncode だけ見えて原因が分からない」状況を避けて切り分けを容易にする。
def _format_failure_message(error):
    message = str(error)
    stderr_tail = getattr(error, "stderr_tail", None)
    if stderr_tail:
        tail_lines = [ln for ln in str(stderr_tail).splitlines() if ln.strip()]
        excerpt = "\n".join(tail_lines[-6:])  # 末尾の実エラー行のみ抜粋
        if excerpt:
            message = f"{message}\n\nFFmpeg エラー詳細:\n{excerpt}"
    return message


# パイプラインをワーカースレッドで実行する
class PipelineWorker(QThread):

    # 進捗 (0.0-1.0, ラベル)
    progress = Signal(float, str)
    # 正常終了 (出力パス)
    finished_ok = Signal(str)
    # ユーザーキャンセルによる中断
    cancelled = Signal()
    # 異常終了 (メッセージ)
    failed = Signal(str)

    def __init__(self, input_path, settings, review_callback,
                 volume_callback=None, parent=None):
        super().__init__(parent)
        self._input_path = input_path
        self._settings = settings
        self._review_callback = review_callback
        self._volume_callback = volume_callback

    # スレッド本体
    def run(self):
        try:
            output = run_pipeline(
                self._input_path,
                self._settings,
                progress_cb=self._emit_progress,
                subtitle_review_callback=self._review_callback,
                volume_analysis_callback=self._volume_callback,
            )
            self.finished_ok.emit(output)
        except PipelineCancelled:
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001 (GUI へ集約通知するため広く捕捉)
            _logger.exception("パイプライン実行に失敗")
            self.failed.emit(_format_failure_message(e))

    # run_pipeline からの進捗コールバック (ワーカースレッド) をシグナルへ橋渡し
    def _emit_progress(self, ratio, label):
        self.progress.emit(float(ratio), str(label))


# GUI ランチャ本体
class MainWindow(QWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("切り抜き自動編集 実行")
        self._settings = load_settings()
        self._worker = None
        self._bridge = None
        # 音量解析ダイアログの橋渡し参照 (resolve7)
        self._volume_bridge = None
        # 設定画面の参照を保持する (ガベージコレクトによる即時クローズを防ぐ)
        self._settings_window = None
        self._build_ui()

    # 画面構築
    def _build_ui(self):
        root = QVBoxLayout(self)

        # 入力動画選択
        input_row = QHBoxLayout()
        input_row.addWidget(QLabel("入力動画:"))
        self.input_edit = QLineEdit()
        # 既定の入力ディレクトリがあればプレースホルダーに使う
        video_dir = self._settings.get("general", {}).get("video_directory", "")
        if video_dir:
            self.input_edit.setPlaceholderText(video_dir)
        input_row.addWidget(self.input_edit)
        self.browse_button = QPushButton("参照...")
        self.browse_button.clicked.connect(self._on_browse)
        input_row.addWidget(self.browse_button)
        root.addLayout(input_row)

        # 設定画面を開くボタン (settings_window.SettingsWindow を参照する)
        self.settings_button = QPushButton("設定...")
        self.settings_button.clicked.connect(self._on_open_settings)
        root.addWidget(self.settings_button)

        # 実行ボタン
        self.run_button = QPushButton("実行")
        self.run_button.clicked.connect(self._on_run)
        root.addWidget(self.run_button)

        # 進捗バー + ステータス
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        root.addWidget(self.progress_bar)
        self.status_label = QLabel("待機中")
        root.addWidget(self.status_label)

        # 稼働証明スピナー: 実行中のみ status_label 先頭で回転させる
        # 進捗率が出ない工程 (音声認識等) でも「動いている」ことを示す
        self._spinner_index = 0
        self._spinner_message = "待機中"
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(_SPINNER_INTERVAL_MS)
        self._spinner_timer.timeout.connect(self._tick_spinner)

    # スピナーを1コマ進めて status_label を更新する (QTimer 駆動)
    def _tick_spinner(self):
        frame = _SPINNER_FRAMES[self._spinner_index % len(_SPINNER_FRAMES)]
        self._spinner_index += 1
        self.status_label.setText(f"{frame} {self._spinner_message}")

    # 設定画面を開く (別ウィンドウとして表示する)
    def _on_open_settings(self):
        # 既に開いている場合は前面に出すだけ
        if self._settings_window is not None and self._settings_window.isVisible():
            self._settings_window.raise_()
            self._settings_window.activateWindow()
            return
        self._settings_window = SettingsWindow()
        self._settings_window.show()

    # 入力動画をファイルダイアログで選択する
    def _on_browse(self):
        start_dir = self._settings.get("general", {}).get("video_directory", "")
        path, _ = QFileDialog.getOpenFileName(
            self, "入力動画を選択", start_dir, _VIDEO_FILE_FILTER
        )
        if path:
            self.input_edit.setText(path)

    # 実行ボタン押下: パイプラインをワーカースレッドで起動する
    def _on_run(self):
        input_path = self.input_edit.text().strip()
        if not input_path or not os.path.exists(input_path):
            QMessageBox.warning(self, "入力エラー", "存在する入力動画を指定してください。")
            return

        # 設定を最新化 (settings_window で変更された可能性に備える)
        self._settings = load_settings()

        # 字幕編集画面フック (メインスレッドでダイアログを開く橋渡し)
        self._bridge = SubtitleReviewBridge(parent_window=self)
        # 音量解析・カット閾値確認フック (resolve7)
        self._volume_bridge = VolumeThresholdBridge(parent_window=self)

        self._worker = PipelineWorker(
            input_path, self._settings, self._bridge,
            volume_callback=self._volume_bridge, parent=self,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.failed.connect(self._on_failed)

        self._spinner_message = "処理開始..."
        self._set_running(True)
        self._worker.start()

    # 実行中の UI 状態を切り替える
    def _set_running(self, running):
        self.run_button.setEnabled(not running)
        self.browse_button.setEnabled(not running)
        self.input_edit.setEnabled(not running)
        # 実行中のみスピナーを回す
        if running:
            self._spinner_timer.start()
        else:
            self._spinner_timer.stop()

    # 進捗更新 (メインスレッド)
    # ラベルはスピナーが描画するためメッセージ更新のみ行う (ちらつき防止)
    def _on_progress(self, ratio, label):
        self.progress_bar.setValue(int(ratio * 100))
        self._spinner_message = label

    # 正常終了
    def _on_finished_ok(self, output_path):
        self._set_running(False)
        self.progress_bar.setValue(100)
        self.status_label.setText(f"完了: {output_path}")
        QMessageBox.information(self, "完了", f"処理が完了しました。\n{output_path}")

    # ユーザーキャンセルによる中断 (異常終了ではない)
    def _on_cancelled(self):
        self._set_running(False)
        self.status_label.setText("中断しました (字幕編集をキャンセル)")
        QMessageBox.information(self, "中断", "字幕編集がキャンセルされたため処理を中断しました。")

    # 異常終了
    def _on_failed(self, message):
        self._set_running(False)
        self.status_label.setText("エラーで停止しました")
        QMessageBox.critical(self, "エラー", f"処理に失敗しました。\n{message}")


# エントリーポイント
def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
