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
import tempfile
import threading
from datetime import datetime

# パッケージ実行・単独スクリプト実行の両対応 (main.py と同方針)
if __package__ is None or __package__ == "":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from src.exceptions import PipelineCancelled
    from src.gui.archive_tab import ArchiveTabWidget
    from src.gui.project_library_dialog import ProjectLibraryDialog
    from src.gui.project_resume_row import ProjectResumeRow
    from src.gui.subtitle_editor_dialog import SubtitleEditorDialog
    from src.gui.timeline.missing_media_dialog import MediaRelinkBridge
    from src.gui.timeline.timeline_editor_dialog import TimelineEditorDialog
    from src.gui.volume_threshold_dialog import VolumeThresholdDialog
    from src.pipeline.pipeline_runner import (
        is_timeline_mode,
        run_from_project,
        run_pipeline,
    )
    from src.settings.settings_window import (
        SettingsWindow,
        load_settings,
        register_fonts_in_dir,
        resolve_fonts_dir,
    )
    from src.timeline import project_io
    from src.timeline.builder import timeline_config
    from src.gui import theme
    from src.utils import updater
    from src.utils.logger import get_logger
    from src.version import __version__
else:
    from ..exceptions import PipelineCancelled
    from ..pipeline.pipeline_runner import (
        is_timeline_mode,
        run_from_project,
        run_pipeline,
    )
    from ..settings.settings_window import (
        SettingsWindow,
        load_settings,
        register_fonts_in_dir,
        resolve_fonts_dir,
    )
    from ..timeline import project_io
    from ..timeline.builder import timeline_config
    from ..utils import updater
    from ..utils.logger import get_logger
    from ..version import __version__
    from . import theme
    from .archive_tab import ArchiveTabWidget
    from .project_library_dialog import ProjectLibraryDialog
    from .project_resume_row import ProjectResumeRow
    from .subtitle_editor_dialog import SubtitleEditorDialog
    from .timeline.missing_media_dialog import MediaRelinkBridge
    from .timeline.timeline_editor_dialog import TimelineEditorDialog
    from .volume_threshold_dialog import VolumeThresholdDialog

from PySide6.QtCore import QObject, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFontDatabase, QIcon, QPalette
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

# 保存済みプロジェクトのファイルフィルタ・一覧の特殊項目は
# 共有ウィジェット側 (gui/project_resume_row.py) へ移した (ver3 resolve9 §5.9)

# 稼働証明スピナーのコマ (Claude Code 風の回転記号) と更新間隔
# 表示崩れ環境向けに ASCII 版 ["|", "/", "-", "\\"] へ差し替え可能
_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_SPINNER_INTERVAL_MS = 120

# ボタンのアイコン化 (設定/実行を文言ではなくアイコン表示にする)
# 画像素材を追加せず Unicode 記号を描画して QIcon 化する。
# 記号・サイズ・描画処理は theme へ移した (ver3 resolve4 §3-5)。
# アーカイブ用タブからも同じ再生アイコンを使うため、双方から import できる場所に置く必要がある
# (archive_tab から main_window は import できない = 循環するため)。


# ワーカースレッドとメインスレッドの橋渡し (字幕編集画面の表示)
# ワーカースレッドから callable として呼ばれ、メインスレッドでダイアログを開く。
# resolve3 §7.3 の SubtitleReviewBridge に対応。
class SubtitleReviewBridge(QObject):

    # メインスレッドへダイアログ表示を依頼するシグナル (items を渡す)
    review_requested = Signal(list)

    def __init__(self, parent_window=None, default_font="", default_size=None,
                 font_families=None, show_theme_field=False, theme_placeholder=""):
        super().__init__()
        self._parent_window = parent_window
        # テロップ個別フォント/サイズの既定値・選択肢 (resolve16 §4.4)
        self._default_font = default_font or ""
        self._default_size = default_size
        self._font_families = font_families
        # テーマ欄 (resolve19)。アーカイブ切り抜き経路のみ show_theme_field=True。
        # 通常クリップ用は既定 False のためテーマ欄も consume_theme も使わず従来同一。
        self._show_theme_field = bool(show_theme_field)
        self._theme_placeholder = theme_placeholder or ""
        self._last_theme = ""  # 直近クリップで入力されたテーマ (consume_theme で1回消費)
        # Resolve 出力用の付随情報 (元入力パス・編集点・設定 / resolve20 §5.3)。
        # subtitle_generator が set_export_context で注入し、ダイアログ終了時に破棄する。
        self._export_context = None
        # 動画プレビュー用の付随情報 (無音カット後動画・実効字幕設定 / resolve23 §5.3)。
        # subtitle_generator が set_preview_context で注入し、ダイアログ終了時に破棄する。
        self._preview_context = None
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

    # Resolve 出力用の付随情報を受け取る (subtitle_generator._attach_export_context から)
    def set_export_context(self, payload):
        self._export_context = payload

    # 動画プレビュー用の付随情報を受け取る (subtitle_generator._attach_preview_context から)
    def set_preview_context(self, payload):
        self._preview_context = payload

    # メインスレッドで実行されるスロット: ダイアログを開いて結果を共有領域へ格納
    def _on_review_requested(self, items):
        try:
            dialog = SubtitleEditorDialog(
                items, parent=self._parent_window,
                default_font=self._default_font,
                default_size=self._default_size,
                font_families=self._font_families,
                # テーマ欄はアーカイブ経路のみ表示 (resolve19)
                show_theme_field=self._show_theme_field,
                theme_placeholder=self._theme_placeholder,
                # DaVinci Resolve 出力の材料 (未注入なら出力ボタン非表示 / resolve20 §5.3)
                export_context=self._export_context,
                # 動画プレビューの材料 (未注入ならプレビュー欄非表示 / resolve23 §5.3)
                preview_context=self._preview_context,
            )
            if dialog.exec() == SubtitleEditorDialog.Accepted:
                self._result = dialog.result_items()
                # 入力テーマを保存 (clip_writer が consume_theme で取り出す / resolve19)
                self._last_theme = dialog.theme_value()
            else:
                # キャンセル / × クローズ → None (中断扱い)
                self._result = None
                self._last_theme = ""  # キャンセル時はテーマ無効
        finally:
            # ダイアログ終了時に編集点・元パスを破棄する (resolve20 §5.3 寿命管理)
            self._export_context = None
            # プレビュー材料も同じ寿命で破棄する (中間動画パスを持ち越さない / resolve23)
            self._preview_context = None
            # 例外有無に関わらずワーカーを再開させる (デッドロック防止)
            self._event.set()

    # 直近クリップのテーマを返して空にする (1回消費 / resolve19)。
    # run_pipeline は 1 クリップずつ同期実行のため、必ず「直前に処理したクリップ」の値を返す。
    def consume_theme(self):
        theme = self._last_theme
        self._last_theme = ""
        return theme


# Timeline 編集画面をワーカースレッド→メインスレッドで橋渡しする (ver3)
# SubtitleReviewBridge と同じ機構 (threading.Event によるブロッキング同期)。
# 既存の SubtitleReviewBridge は無改変で併存し、timeline.enabled=false のときは
# 従来どおりそちらが使われる (R2)。
class TimelineReviewBridge(QObject):

    # メインスレッドへ画面表示を依頼するシグナル (payload dict を渡す)
    review_requested = Signal(object)

    def __init__(self, parent_window=None):
        super().__init__()
        self._parent_window = parent_window
        self._event = threading.Event()
        self._result = None
        self.review_requested.connect(self._on_review_requested, Qt.QueuedConnection)

    # ワーカースレッドから呼ばれる (run_pipeline の timeline_review_callback)
    # payload: {"timeline","settings","project_path","asr_audio_path"}
    # 戻り値: 編集後の Timeline / None (キャンセル)
    def __call__(self, payload):
        self._event.clear()
        self._result = None
        self.review_requested.emit(payload)
        # ユーザー操作が終わるまでワーカースレッドをブロックする
        self._event.wait()
        return self._result

    # メインスレッドで実行されるスロット
    def _on_review_requested(self, payload):
        try:
            timeline = payload["timeline"]
            # プレビュー用一時ファイルは中間ファイルと同じ作業ディレクトリへ置き、
            # パイプライン終了時の cleanup で一緒に消えるようにする (回答 Q10)
            work_dir = payload.get("working_dir") or tempfile.gettempdir()
            dialog = TimelineEditorDialog(
                timeline, payload["settings"], work_dir,
                asr_audio_path=payload.get("asr_audio_path"),
                parent=self._parent_window,
                # 保存・再編集 (ver3 resolve7 §5.7 / §5.10)
                project_path=payload.get("project_path"),
                created_at=payload.get("created_at"),
                mode=payload.get("mode", "pipeline"),
            )
            if dialog.exec() == TimelineEditorDialog.Accepted:
                # Timeline と保存先をまとめて返す (「名前を付けて保存」で変えた先を伝える)
                self._result = dialog.result_payload()
            else:
                self._result = None  # キャンセル / × クローズ → 中断扱い
        except Exception:  # noqa: BLE001 (画面生成の失敗でワーカーを固めない)
            _logger.exception("Timeline 編集画面の表示に失敗しました")
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
                 volume_callback=None, timeline_callback=None, parent=None):
        super().__init__(parent)
        self._input_path = input_path
        self._settings = settings
        self._review_callback = review_callback
        self._volume_callback = volume_callback
        self._timeline_callback = timeline_callback

    # スレッド本体
    def run(self):
        try:
            output = run_pipeline(
                self._input_path,
                self._settings,
                progress_cb=self._emit_progress,
                subtitle_review_callback=self._review_callback,
                volume_analysis_callback=self._volume_callback,
                timeline_review_callback=self._timeline_callback,
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


# 保存済みプロジェクトからの再編集をワーカースレッドで実行する (ver3 resolve7 §5.11)
# 進捗・完了・キャンセル・失敗の通知は PipelineWorker と同じ形にして、
# 画面側の受け口をそのまま共通で使えるようにする。
class ProjectResumeWorker(QThread):

    progress = Signal(float, str)
    finished_ok = Signal(str)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, project_path, settings, timeline_callback,
                 relink_callback=None, restore_path=None, parent=None):
        super().__init__(parent)
        self._project_path = project_path
        self._settings = settings
        self._timeline_callback = timeline_callback
        self._relink_callback = relink_callback
        # 自動保存から復元する場合の読み込み元 (保存先は project_path のまま)
        self._restore_path = restore_path

    def run(self):
        try:
            output = run_from_project(
                self._project_path,
                self._settings,
                progress_cb=self._emit_progress,
                timeline_review_callback=self._timeline_callback,
                media_relink_callback=self._relink_callback,
                restore_path=self._restore_path,
            )
            self.finished_ok.emit(output)
        except PipelineCancelled:
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001 (GUI へ集約通知するため広く捕捉)
            _logger.exception("保存済みプロジェクトの再編集に失敗")
            self.failed.emit(_format_failure_message(e))

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

    # 実行状態の変化を親へ通知する (ver3 resolve4 §5.7-4 / 回答 Q1)
    # タブ外へ移した設定ボタンを無効化するために使う。タブ自身の無効化は従来どおり。
    running_changed = Signal(bool)
    # 種別違いのプロジェクトが選ばれた → 親にタブを切り替えてもらう (ver3 resolve9 §3-4)
    switch_tab_requested = Signal(str, str)      # (kind, project_path)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = load_settings()
        self._worker = None
        self._bridge = None
        # 音量解析ダイアログの橋渡し参照 (resolve7)
        self._volume_bridge = None
        # Timeline 編集画面の橋渡し参照 (ver3)
        self._timeline_bridge = None
        # 素材の再リンク画面の橋渡し参照 (ver3 resolve7 Phase 5)
        self._relink_bridge = None
        self._build_ui()
        # このタブ上で動画ファイルのドロップを受け付ける (resolve12)
        self.setAcceptDrops(True)

    # 画面構築
    def _build_ui(self):
        root = QVBoxLayout(self)

        # 入力動画選択 ("入力動画:" のラベルは廃止し、入力欄のプレースホルダで案内する)
        input_row = QHBoxLayout()
        self.input_edit = QLineEdit()
        # 動画ファイルを直接ドラッグ&ドロップできる旨を案内する (resolve12)
        self.input_edit.setPlaceholderText("動画ファイルをここにドラッグ&ドロップ、または「参照...」")
        input_row.addWidget(self.input_edit)
        self.browse_button = QPushButton("参照...")
        self.browse_button.clicked.connect(self._on_browse)
        input_row.addWidget(self.browse_button)
        root.addLayout(input_row)

        # 実行ボタン (文言ではなく再生アイコン。用途はツールチップで示す)
        # アプリの主要動作のためアクセント塗り (primaryButton) にする (resolve3 §5.2-2)。
        # 設定ボタンはタブ外へ移したため、この列には実行ボタンだけが残る (resolve4 M3)。
        # 幅を明示しないと列いっぱいに広がって間延びする (resolve4 §5.10-2)。
        button_row = QHBoxLayout()
        self.run_button = QPushButton()
        self.run_button.clicked.connect(self._on_run)
        theme.setup_primary_action_button(self.run_button, theme.RUN_GLYPH, "実行")
        button_row.addWidget(self.run_button)
        button_row.addStretch(1)
        root.addLayout(button_row)

        # 編集の続き (保存済み Timeline プロジェクトを開き直す / ver3 resolve7 §5.11)
        # 前半 (正規化・無音検出・音声認識) を飛ばし、保存した編集の続きから書き出す。
        # 行はアーカイブタブと共有のウィジェット (ver3 resolve9 §5.9)。
        self.resume_row = ProjectResumeRow(project_io.KIND_CLIP, self._settings)
        self.resume_row.resume_requested.connect(self._start_resume)
        self.resume_row.library_requested.connect(self._open_library)
        self.resume_row.wrong_kind_selected.connect(
            lambda path, kind: self.switch_tab_requested.emit(kind, path))
        root.addWidget(self.resume_row)

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

    # アイコンをテーマの色で描き直す (resolve3 §5.10-2)
    # ボタンアイコンは QPixmap へ焼き込むため、OS の明暗が切り替わったら作り直す必要がある。
    def refresh_theme(self):
        theme.refresh_primary_action_icon(self.run_button, theme.RUN_GLYPH)

    # スピナーを1コマ進めて status_label を更新する (QTimer 駆動)
    def _tick_spinner(self):
        frame = _SPINNER_FRAMES[self._spinner_index % len(_SPINNER_FRAMES)]
        self._spinner_index += 1
        self.status_label.setText(f"{frame} {self._spinner_message}")

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

    # ドロップされたファイルを受け取る
    # 動画は入力欄へ、保存済みプロジェクトは「編集の続き」へ入れる (ver3 resolve7 §5.11)
    def dropEvent(self, event):
        project = self._dropped_project_path(event)
        if project:
            self._select_project(project)
            event.acceptProposedAction()
            return
        path = self._dropped_video_path(event)
        if path:
            self.input_edit.setText(path)
            event.acceptProposedAction()
        else:
            event.ignore()

    # ドロップ内容が受理可能か判定する (実行中でないこと・拡張子)
    def _is_acceptable_drop(self, event):
        # 実行中は入力書き換えを避けるためドロップを受け付けない
        if self._worker is not None and self._worker.isRunning():
            return False
        return (self._dropped_video_path(event) is not None
                or self._dropped_project_path(event) is not None)

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

    # ===== 編集の続き (保存済みプロジェクトの再編集 / ver3 resolve7 §5.11) =====

    # 最近使ったプロジェクトの一覧を作り直す (setting.json timeline.project.recent)
    def _refresh_recent_projects(self):
        self.resume_row.set_settings(self._settings)

    # 一覧画面を開く (ver3 resolve9 §5.12)。開くときは自分のタブで再開する。
    def _open_library(self):
        dialog = ProjectLibraryDialog(project_io.KIND_CLIP, self._settings, parent=self)
        dialog.open_requested.connect(self._on_library_open)
        dialog.changed.connect(self._refresh_recent_projects)
        dialog.exec()
        self._refresh_recent_projects()

    def _on_library_open(self, path):
        self.resume_row.select(path)
        self._start_resume(path)

    # 自動保存が本体より新しければ、そちらから復元するか尋ねる (ver3 resolve7 §5.9)
    # 戻り値: 読み込みに使うパス (復元しないなら None = 本体をそのまま開く)
    def _resolve_autosave(self, project_path):
        cfg = timeline_config(self._settings)["project"]
        autosave = project_io.autosave_path(project_path, cfg["autosave_suffix"])
        try:
            if not os.path.exists(autosave):
                return None
            if os.path.getmtime(autosave) <= os.path.getmtime(project_path):
                return None    # 本体の方が新しい = 保存済み。自動保存は使わない
            stamp = datetime.fromtimestamp(
                os.path.getmtime(autosave)).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            return None
        answer = QMessageBox.question(
            self, "自動保存が見つかりました",
            f"保存されていない編集が自動保存に残っています（{stamp}）。\n"
            "こちらから編集を再開しますか?\n\n"
            "「いいえ」を選ぶと、最後に保存した内容を開きます"
            "（自動保存はそのまま残ります）。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        return autosave if answer == QMessageBox.Yes else None

    # 保存済みプロジェクトからの再編集をワーカースレッドで起動する
    def _start_resume(self, project_path):
        # 設定を最新化 (settings_window で変更された可能性に備える)
        self._settings = load_settings()
        register_fonts_in_dir(resolve_fonts_dir(self._settings))

        # 自動保存が残っていれば復元するか尋ねる (自動保存が無効なら何も起きない)
        restore_path = self._resolve_autosave(project_path)

        # Timeline 編集画面フック (再編集は Timeline モード専用の経路)
        self._timeline_bridge = TimelineReviewBridge(parent_window=self)
        # 見つからない素材の再リンク画面フック
        self._relink_bridge = MediaRelinkBridge(self._settings, parent_window=self)
        self._worker = ProjectResumeWorker(
            project_path, self._settings, self._timeline_bridge,
            relink_callback=self._relink_bridge, restore_path=restore_path,
            parent=self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.failed.connect(self._on_failed)

        self._spinner_message = "プロジェクトを読み込み中..."
        self._set_running(True)
        self._worker.start()

    # MIME から保存済み Timeline プロジェクトのパスを1件取り出す (非対応なら None)
    # 判定は setting.json の接尾辞 (timeline.project_suffix) に一致すること。
    def _dropped_project_path(self, event):
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        suffix = timeline_config(self._settings)["project_suffix"].lower()
        for url in mime.urls():
            local = url.toLocalFile()
            if local and local.lower().endswith(suffix) and os.path.exists(local):
                return local
        return None

    # D&D されたプロジェクトを「編集の続き」の選択状態にする
    # 落としただけで長い処理が始まらないよう、実行は「開く...」で明示的に行わせる。
    def _select_project(self, path):
        self.resume_row.select(path)
        self.status_label.setText(
            f"編集の続き: {os.path.basename(path)}（「開く...」で再開します）")

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

        # Timeline 編集画面フック (ver3)。Timeline モードのときだけ注入する。
        # 従来モードでは注入せず、既存の字幕編集画面がそのまま使われる (R2)。
        self._timeline_bridge = (
            TimelineReviewBridge(parent_window=self)
            if is_timeline_mode(self._settings) else None
        )

        self._worker = PipelineWorker(
            input_path, self._settings, self._bridge,
            volume_callback=self._volume_bridge,
            timeline_callback=self._timeline_bridge, parent=self,
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
        # 編集の続き (ver3 resolve7 §5.11 / 行は resolve9 で共有ウィジェットへ)
        self.resume_row.set_busy(running)
        if not running:
            # 編集画面で保存されていれば履歴が増えているため作り直す
            self._refresh_recent_projects()
        # 実行中のみスピナーを回す
        if running:
            self._spinner_timer.start()
        else:
            self._spinner_timer.stop()
        # タブ外の設定ボタンを無効化するため親へ通知する (resolve4 §5.7-4)
        self.running_changed.emit(running)

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


# GUI ランチャ本体 (タブホスト)
# request17 / flow17 R0: メイン画面を QTabWidget 化し、クリップ用/アーカイブ切り抜き用を
# タブで切り分ける。アプリ級の自動更新チェックは本ウィンドウが担う。
class MainWindow(QWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Stretheus")
        # 自動更新チェックの ON/OFF 判定に使用する設定
        self._settings = load_settings()
        # 自動更新ワーカー参照 (GC 防止)
        self._update_check_worker = None
        self._update_download_worker = None
        self._update_progress = None
        # ネイティブ背景効果は表示後 (winId() が有効になってから) に 1 度だけ要求する
        self._backdrop_requested = False
        # 設定画面の参照を保持する (ガベージコレクトによる即時クローズを防ぐ / resolve4 M3)
        self._settings_window = None
        # 実行中のタブ (resolve4 §5.7-4)。空でないあいだ設定ボタンを無効化する。
        self._running_tabs = set()
        self._build_ui()

        # ガラスモーフィズム (ver3 resolve3 §5.9)
        # 背景のグラデーションを描き、OS の明暗切り替えに追随する。
        theme.install_window_background(self)
        theme.watch_color_scheme(QApplication.instance(), self._on_theme_changed,
                                 self._settings)

    # OS のライト/ダーク設定が切り替わったときの貼り替え (R8 / resolve3 §5.10-2)
    # QSS・パレット・自前描画の色キャッシュを作り直して再描画するだけで、
    # 編集中の状態 (Timeline の内容・選択・Undo 履歴) は一切失われない。
    def _on_theme_changed(self):
        app = QApplication.instance()
        before = theme.mode(self._settings)
        theme.invalidate_cache()
        after = theme.mode(self._settings)
        _logger.info("OS の配色が変わったためテーマを貼り替えます: %s → %s",
                     "ライト" if before == "light" else "ダーク",
                     "ライト" if after == "light" else "ダーク")
        theme.apply(app, self._settings)
        # タイトルバーの明暗も追随させる
        self._backdrop_requested = False
        self._apply_native_backdrop()
        # QPixmap へ焼き込んだアイコンはテーマ追随しないため描き直す (resolve4 §5.10-3)
        self.clip_tab.refresh_theme()
        self.archive_tab.refresh_theme()
        self._refresh_settings_icon()
        theme.refresh_all_windows(app)

    # 表示後にネイティブのすりガラスを要求する (resolve3 §5.4)
    # winId() が有効になってからでないと DWM へ渡せないため showEvent で行う。
    def showEvent(self, event):
        super().showEvent(event)
        self._apply_native_backdrop()

    # ネイティブ背景効果を 1 度だけ要求する (失敗してもグラデーション背景で成立する)
    def _apply_native_backdrop(self):
        if self._backdrop_requested:
            return
        self._backdrop_requested = True
        theme.apply_native_backdrop(self, self._settings)

    # タブを構築する (現行クリップ機能 + アーカイブ切り抜き)
    def _build_ui(self):
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        # クリップ用タブ (現行機能を無改変移設)
        self.clip_tab = ClipTabWidget()
        self.tabs.addTab(self.clip_tab, "クリップ用")
        # アーカイブ切り抜き用タブ (R0: 準備中)
        self.archive_tab = ArchiveTabWidget()
        self.tabs.addTab(self.archive_tab, "アーカイブ切り抜き用")
        # 先頭 (クリップ用) タブ選択時のみペイン左上を四角にする (resolve4 M1)
        theme.bind_tab_pane_corner(self.tabs)

        # 設定ボタンはタブの外 (右上) へ置く (resolve4 M3)。
        # タブ内に置くとそのタブを選んでいる間しか設定を開けないため、
        # どのタブを選んでいても開けるようコーナーウィジェットにする。
        self.settings_button = QPushButton()
        self.settings_button.setIconSize(QSize(theme.BUTTON_ICON_PX, theme.BUTTON_ICON_PX))
        self.settings_button.setToolTip("設定")
        theme.mark_icon_button(self.settings_button)
        self.settings_button.clicked.connect(self._on_open_settings)
        self.tabs.setCornerWidget(self.settings_button, Qt.TopRightCorner)
        self._refresh_settings_icon()

        # どちらかのタブが実行中なら設定ボタンを無効化する (resolve4 §5.7-4 / 回答 Q1)
        self.clip_tab.running_changed.connect(
            lambda running: self._on_tab_running_changed("clip", running))
        self.archive_tab.running_changed.connect(
            lambda running: self._on_tab_running_changed("archive", running))

        # 種別違いのプロジェクトを選んだら相手のタブへ回す (ver3 resolve9 §3-4)
        self.clip_tab.switch_tab_requested.connect(self._switch_to_kind)
        self.archive_tab.switch_tab_requested.connect(self._switch_to_kind)

        root.addWidget(self.tabs)

    # 種別違いのプロジェクトを、その種別のタブの「編集の続き」へ移す (ver3 resolve9 §3-4)
    # 実行はしない (選択状態にするところまで)。長い処理は「開く...」で明示的に始めさせる。
    def _switch_to_kind(self, kind, project_path):
        tab = (self.archive_tab if kind == project_io.KIND_ARCHIVE else self.clip_tab)
        row = getattr(tab, "resume_row", None)
        if row is None:
            return
        self.tabs.setCurrentWidget(tab)
        row.select(project_path)

    # 設定画面を開く (別ウィンドウとして表示する)
    # resolve4 M3: ClipTabWidget から移設。どのタブを選んでいても開ける。
    def _on_open_settings(self):
        # 既に開いている場合は前面に出すだけ
        if self._settings_window is not None and self._settings_window.isVisible():
            self._settings_window.raise_()
            self._settings_window.activateWindow()
            return
        self._settings_window = SettingsWindow()
        self._settings_window.show()

    # タブの実行状態が変わったときに設定ボタンの可否を更新する (resolve4 §5.7-4)
    # 実行中のタブを集合で持つのは、一方が終わってももう一方が実行中なら
    # 有効化してはいけないため (bool 1 個だと取りこぼす)。
    def _on_tab_running_changed(self, tab_key, running):
        if running:
            self._running_tabs.add(tab_key)
        else:
            self._running_tabs.discard(tab_key)
        self.settings_button.setEnabled(not self._running_tabs)

    # 設定ボタンのアイコンを現在のテーマ色で描き直す (resolve3 §5.10-2)
    def _refresh_settings_icon(self):
        if theme.is_enabled():
            icon_color = theme.color("text.primary")
        else:
            # ui.theme = "system" では従来どおりパレットのボタン文字色を使う
            icon_color = self.settings_button.palette().color(QPalette.ButtonText)
        self.settings_button.setIcon(theme.glyph_icon(theme.SETTINGS_GLYPH, icon_color))

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
    # 凍結配布物では twitch-dl を別バイナリにせず、本体exeを multi-call で twitchdl CLI として動かす。
    # これによりアップデート/インストール時に本体と一緒に twitch-dl も展開される (追加バイナリ不要 /
    # flow17 R3)。twitch_source が凍結時に [sys.executable, "__twitchdl__", ...] を起動する。
    if len(sys.argv) > 1 and sys.argv[1] == "__twitchdl__":
        from twitchdl.cli import cli
        sys.argv = ["twitch-dl", *sys.argv[2:]]  # `python -m twitchdl` 相当に整える
        cli()  # click グループ (standalone_mode=True で内部 sys.exit する)
        return

    app = QApplication(sys.argv)
    # 実行中ウィンドウ/タスクバーのアイコンを設定する (exe 埋め込みアイコンとは別管理)。
    # QApplication へ設定すると全トップレベルウィンドウの既定アイコンになる。
    # 未配置(None)なら従来どおり Qt 既定アイコンで起動する (後方互換)。
    icon_path = _resolve_app_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))
    # ガラスモーフィズムのテーマを適用する (ver3 resolve3 §5.9)。
    # QApplication へ 1 回当てるだけで、以後に作られるダイアログにも自動で効く。
    # ui.theme = "system" のときは何も当てず従来の Qt 既定で起動する。
    theme.apply(app, load_settings())
    window = MainWindow()
    window.show()
    # 起動時の更新チェック (設定 ON かつ凍結ビルド時のみ実際に走る)
    window.start_update_check()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
