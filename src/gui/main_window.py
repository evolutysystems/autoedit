# GUI 専用ランチャ (字幕編集画面を伴うパイプライン実行)
# resolve3 §7.3・§10-1 に対応
# 入力動画の選択 / 実行ボタン / 進捗表示 / 字幕編集画面の橋渡しを担う。
# パイプラインはワーカースレッドで実行し、フルテロップ後にメインスレッドで
# 字幕編集画面 (モーダル) を開く。
#
# request17 / flow17 R0: メイン画面をタブ化した。現行のクリップ機能は ClipTabWidget へ
# 無改変で移設し、アーカイブ切り抜き用タブ (ArchiveTabWidget) を「準備中」で追加する。
# MainWindow は QTabWidget のホスト兼アプリ級の自動更新チェックを担う。
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
    from src.settings.settings_window import (
        SettingsWindow,
        load_settings,
        register_fonts_in_dir,
        resolve_fonts_dir,
    )
    from src.utils import updater
    from src.utils.logger import get_logger
    from src.version import __version__
else:
    from ..exceptions import PipelineCancelled
    from ..pipeline.pipeline_runner import run_pipeline
    from ..settings.settings_window import (
        SettingsWindow,
        load_settings,
        register_fonts_in_dir,
        resolve_fonts_dir,
    )
    from ..utils import updater
    from ..utils.logger import get_logger
    from ..version import __version__
    from .subtitle_editor_dialog import SubtitleEditorDialog
    from .volume_threshold_dialog import VolumeThresholdDialog

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

_logger = get_logger(__name__)

# 動画ファイルフィルタ (settings_window と整合)
_VIDEO_FILE_FILTER = "動画ファイル (*.mp4 *.mov *.avi *.mkv *.flv *.wmv);;すべてのファイル (*)"

# ドロップ受理対象の動画拡張子 (resolve12)。_VIDEO_FILE_FILTER と整合させる。
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv"}

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

    def __init__(self, parent_window=None, default_font="", default_size=None,
                 font_families=None):
        super().__init__()
        self._parent_window = parent_window
        # テロップ個別フォント/サイズの既定値・選択肢 (resolve16 §4.4)
        self._default_font = default_font or ""
        self._default_size = default_size
        self._font_families = font_families
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
            dialog = SubtitleEditorDialog(
                items, parent=self._parent_window,
                default_font=self._default_font,
                default_size=self._default_size,
                font_families=self._font_families,
            )
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


# 起動時の更新チェックをワーカースレッドで行う (request_autoupdate.md §6.1)
# UI をブロックしないよう GitHub Releases 問い合わせを別スレッドで実行する。
class UpdateCheckWorker(QThread):

    # 新版検知時のみ latest 情報 {"tag","installer_url"} を通知する
    update_available = Signal(object)

    def __init__(self, current_version, parent=None):
        super().__init__(parent)
        self._current_version = current_version

    def run(self):
        try:
            latest = updater.find_update(self._current_version)
        except Exception as e:  # noqa: BLE001 (更新チェック失敗は致命ではない)
            _logger.info("更新チェックに失敗 (無視して続行): %s", e)
            return
        if latest:
            self.update_available.emit(latest)


# インストーラのダウンロードをワーカースレッドで行う
class UpdateDownloadWorker(QThread):

    progress = Signal(int)       # 0-100 (総サイズ不明時は -1 を通知)
    finished_ok = Signal(str)    # DL 済みインストーラのパス
    failed = Signal(str)

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self._url = url

    def run(self):
        try:
            path = updater.download_installer(self._url, on_progress=self._on_progress)
            self.finished_ok.emit(path)
        except Exception as e:  # noqa: BLE001 (DL 失敗は GUI へ通知)
            _logger.exception("更新のダウンロードに失敗")
            self.failed.emit(str(e))

    # urlretrieve の reporthook: (block_num, block_size, total_size)
    def _on_progress(self, block_num, block_size, total_size):
        if total_size and total_size > 0:
            downloaded = block_num * block_size
            percent = int(min(downloaded * 100 // total_size, 100))
            self.progress.emit(percent)
        else:
            self.progress.emit(-1)  # 総サイズ不明 → 不確定表示


# クリップ用タブ (現行のクリップ自動編集フロー一式)
# request17 / flow17 R0: 旧 MainWindow の本体 (入力選択・実行・進捗・スピナー・
# ドラッグ&ドロップ・字幕編集/音量解析の橋渡し) をそのまま移設したもの。挙動は不変。
class ClipTabWidget(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = load_settings()
        self._worker = None
        self._bridge = None
        # 音量解析ダイアログの橋渡し参照 (resolve7)
        self._volume_bridge = None
        # 設定画面の参照を保持する (ガベージコレクトによる即時クローズを防ぐ)
        self._settings_window = None
        self._build_ui()
        # このタブ上で動画ファイルのドロップを受け付ける (resolve12)
        self.setAcceptDrops(True)

    # 画面構築
    def _build_ui(self):
        root = QVBoxLayout(self)

        # 入力動画選択
        input_row = QHBoxLayout()
        input_row.addWidget(QLabel("入力動画:"))
        self.input_edit = QLineEdit()
        # 動画ファイルを直接ドラッグ&ドロップできる旨を案内する (resolve12)
        self.input_edit.setPlaceholderText("動画ファイルをここにドラッグ&ドロップ、または「参照...」")
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

    # ===== ドラッグ&ドロップによる入力動画選択 (resolve12) =====

    # ドラッグされたものが単一のローカル動画ファイルのときのみ受理を通知する
    def dragEnterEvent(self, event):
        if self._is_acceptable_drop(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    # ドロップされた動画ファイルパスを入力欄へ設定する
    def dropEvent(self, event):
        path = self._dropped_video_path(event)
        if path:
            self.input_edit.setText(path)
            event.acceptProposedAction()
        else:
            event.ignore()

    # ドロップ内容が受理可能な動画ファイルか判定する (実行中でないこと・拡張子)
    def _is_acceptable_drop(self, event):
        # 実行中は入力書き換えを避けるためドロップを受け付けない
        if self._worker is not None and self._worker.isRunning():
            return False
        return self._dropped_video_path(event) is not None

    # MIME からローカル動画ファイルパスを1件取り出す (非対応なら None)
    def _dropped_video_path(self, event):
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            local = url.toLocalFile()
            if local and os.path.splitext(local)[1].lower() in _VIDEO_EXTENSIONS:
                return local
        return None

    # 実行ボタン押下: パイプラインをワーカースレッドで起動する
    def _on_run(self):
        input_path = self.input_edit.text().strip()
        if not input_path or not os.path.exists(input_path):
            QMessageBox.warning(self, "入力エラー", "存在する入力動画を指定してください。")
            return

        # 設定を最新化 (settings_window で変更された可能性に備える)
        self._settings = load_settings()

        # 追加フォント(settings/fonts)を Qt へ登録し、編集画面の一覧へ反映する (resolve16 §4.2)
        register_fonts_in_dir(resolve_fonts_dir(self._settings))
        subtitle_cfg = self._settings.get("subtitle", {})

        # 字幕編集画面フック (メインスレッドでダイアログを開く橋渡し)
        # テロップ個別フォント/サイズの既定値・選択肢を渡す (resolve16 §4.4)
        self._bridge = SubtitleReviewBridge(
            parent_window=self,
            default_font=subtitle_cfg.get("font_family", ""),
            default_size=subtitle_cfg.get("font_size", None),
            font_families=list(QFontDatabase.families()),
        )
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


# アーカイブ切り抜き用タブ (request17 / flow17 R0: 準備中プレースホルダ)
# 採点・Twitch 取得・結果画面・切り抜きは後続リリース (R1 以降) で実装する。
# R0 では機能を一切持たず「準備中」を表示するのみ (誤操作でパイプラインが走らないこと)。
# 露出制御用の archive.enabled は後続段階で使用する (現段階は常に準備中)。
class ArchiveTabWidget(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    # 準備中メッセージのみを中央に表示する
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.addStretch(1)
        title = QLabel("アーカイブ切り抜き（準備中）")
        title.setAlignment(Qt.AlignCenter)
        root.addWidget(title)
        note = QLabel(
            "Twitch アーカイブからの採点・切り抜き機能は準備中です。\n"
            "今後のアップデートで順次提供します。"
        )
        note.setAlignment(Qt.AlignCenter)
        root.addWidget(note)
        root.addStretch(1)


# GUI ランチャ本体 (タブホスト)
# request17 / flow17 R0: メイン画面を QTabWidget 化し、クリップ用/アーカイブ切り抜き用を
# タブで切り分ける。アプリ級の自動更新チェックは本ウィンドウが担う。
class MainWindow(QWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("切り抜き自動編集 実行")
        # 自動更新チェックの ON/OFF 判定に使用する設定
        self._settings = load_settings()
        # 自動更新ワーカー参照 (GC 防止)
        self._update_check_worker = None
        self._update_download_worker = None
        self._update_progress = None
        self._build_ui()

    # タブを構築する (現行クリップ機能 + アーカイブ切り抜き準備中)
    def _build_ui(self):
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        # クリップ用タブ (現行機能を無改変移設)
        self.clip_tab = ClipTabWidget()
        self.tabs.addTab(self.clip_tab, "クリップ用")
        # アーカイブ切り抜き用タブ (R0: 準備中)
        self.archive_tab = ArchiveTabWidget()
        self.tabs.addTab(self.archive_tab, "アーカイブ切り抜き用")
        root.addWidget(self.tabs)

    # ===== 自動更新 (request_autoupdate.md §6) =====

    # 起動時の更新チェックを開始する (設定 ON 時のみ / 非ブロッキング)
    def start_update_check(self):
        if not self._settings.get("general", {}).get("auto_update_check", True):
            return
        self._update_check_worker = UpdateCheckWorker(__version__, parent=self)
        self._update_check_worker.update_available.connect(self._on_update_available)
        self._update_check_worker.start()

    # 新版検知: 確認ダイアログを表示し、同意時にダウンロードを開始する
    def _on_update_available(self, latest):
        tag = latest.get("tag", "")
        answer = QMessageBox.question(
            self, "更新の確認",
            f"新しいバージョン {tag} が公開されています。\n"
            "今すぐ更新しますか？（更新中はアプリが再起動されます）",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return

        # ダウンロード進捗ダイアログ (キャンセル不可: 途中中断で不整合を避ける)
        self._update_progress = QProgressDialog(
            "更新プログラムをダウンロードしています...", None, 0, 100, self
        )
        self._update_progress.setWindowTitle("更新")
        self._update_progress.setWindowModality(Qt.WindowModal)
        self._update_progress.setAutoClose(False)
        self._update_progress.setValue(0)
        self._update_progress.show()

        self._update_download_worker = UpdateDownloadWorker(
            latest["installer_url"], parent=self
        )
        self._update_download_worker.progress.connect(self._on_update_progress)
        self._update_download_worker.finished_ok.connect(self._on_update_downloaded)
        self._update_download_worker.failed.connect(self._on_update_download_failed)
        self._update_download_worker.start()

    # ダウンロード進捗 (百分率。-1 は総サイズ不明=不確定バー)
    def _on_update_progress(self, percent):
        if self._update_progress is None:
            return
        if percent < 0:
            self._update_progress.setRange(0, 0)  # 不確定 (ビジー) 表示
        else:
            self._update_progress.setRange(0, 100)
            self._update_progress.setValue(percent)

    # ダウンロード完了: インストーラを起動しアプリを終了する
    def _on_update_downloaded(self, installer_path):
        if self._update_progress is not None:
            self._update_progress.close()
        QMessageBox.information(
            self, "更新",
            "更新プログラムを起動します。アプリを終了します。",
        )
        try:
            updater.launch_installer(installer_path)
        except Exception as e:  # noqa: BLE001 (起動失敗も GUI へ通知)
            _logger.exception("更新プログラムの起動に失敗")
            QMessageBox.warning(self, "更新", f"更新プログラムの起動に失敗しました。\n{e}")
            return
        # インストーラが本体を上書きできるよう、アプリを終了する
        QApplication.quit()

    # ダウンロード失敗: 通常起動を継続 (更新は次回に持ち越し)
    def _on_update_download_failed(self, message):
        if self._update_progress is not None:
            self._update_progress.close()
        QMessageBox.warning(
            self, "更新",
            f"更新のダウンロードに失敗しました。次回起動時に再試行します。\n{message}",
        )


# アプリアイコン(app.ico)の実体パスを凍結/非凍結の双方で解決する
# 非凍結: このファイル(src/gui/main_window.py)と同階層の app.ico。
# 凍結(PyInstaller): datas で同梱した _internal/src/gui/app.ico を sys._MEIPASS 基準で解決する
#   (ffmpeg 同梱と同じ sys._MEIPASS 方式 / HowToRelease §3.2)。未配置なら None を返す。
def _resolve_app_icon_path():
    if getattr(sys, "frozen", False):
        base = os.path.join(getattr(sys, "_MEIPASS", ""), "src", "gui")
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    icon_path = os.path.join(base, "app.ico")
    return icon_path if os.path.exists(icon_path) else None


# エントリーポイント
def main():
    app = QApplication(sys.argv)
    # 実行中ウィンドウ/タスクバーのアイコンを設定する (exe 埋め込みアイコンとは別管理)。
    # QApplication へ設定すると全トップレベルウィンドウの既定アイコンになる。
    # 未配置(None)なら従来どおり Qt 既定アイコンで起動する (後方互換)。
    icon_path = _resolve_app_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))
    window = MainWindow()
    window.show()
    # 起動時の更新チェック (設定 ON かつ凍結ビルド時のみ実際に走る)
    window.start_update_check()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
