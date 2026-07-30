# アーカイブ切り抜き用タブ (flow17 R3: Twitch ログイン+twitch-dl 取得+コメント採点)
# 入力: ローカル mp4 (+任意 chat json) または Twitch VOD URL (ログインして自分のVODを自動取得)。
# → 採点開始 → 全クリップ文字起こし → 結果画面(採点グラフ+字幕編集+プレビュー) で一括編集
# → 完了で切り抜き+字幕焼き込み+結合。
# コメント採点(ｗ数・急増ボーナス)は chat 取得時に有効化される (resolve17 §4.4.1)。
import os

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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

from ..archive import clip_writer, comment_source, config, pipeline, twitch_source
from ..archive.twitch_auth import TwitchAuth
from ..exceptions import TwitchError
from ..settings.settings_window import (
    load_settings,
    register_fonts_in_dir,
    resolve_fonts_dir,
)
from ..utils.logger import get_logger
from .archive_result_window import ArchiveResultBridge

_logger = get_logger(__name__)

# 稼働証明スピナー (main_window と同種。循環 import を避けるため本ファイルで定義)
_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_SPINNER_INTERVAL_MS = 120

# 入力に使う動画フィルタ (main_window と整合)
_VIDEO_FILE_FILTER = "動画ファイル (*.mp4 *.mov *.avi *.mkv *.flv *.wmv);;すべてのファイル (*)"
_CHAT_FILE_FILTER = "チャットJSON (*.json);;すべてのファイル (*)"

# テーマ欄のプレースホルダ (resolve19 / request19 指定の固定文言。設定項目にはしない)
_THEME_PLACEHOLDER = "(任意)テーマを決めてください"

# 入力モード
_MODE_LOCAL = "local"
_MODE_TWITCH = "twitch"


# Twitch ログイン(OAuth)をワーカースレッドで実行する (ブラウザ認可待ちで UI を固めない)
class TwitchLoginWorker(QThread):
    finished_ok = Signal(object)   # get_self() の戻り (ユーザー情報 dict)
    failed = Signal(str)

    def __init__(self, auth, parent=None):
        super().__init__(parent)
        self._auth = auth

    def run(self):
        try:
            self._auth.login()
            self.finished_ok.emit(self._auth.get_self())
        except Exception as e:  # noqa: BLE001 (GUI へ集約通知)
            _logger.exception("Twitch ログインに失敗")
            self.failed.emit(str(e))


# 取得(Twitch)+採点(analyze)をワーカースレッドで実行する
# job: {"mode","input_path","url","chat_path"}。twitch モードは VOD/コメントを取得してから採点する。
class ArchiveAnalyzeWorker(QThread):
    progress = Signal(float, str)
    finished_ok = Signal(object)   # {"result": analyze結果, "input_path": ローカル実体パス}
    failed = Signal(str)

    def __init__(self, job, settings, auth=None, parent=None):
        super().__init__(parent)
        self._job = job
        self._settings = settings
        self._auth = auth

    def run(self):
        try:
            input_path, comments = self._prepare_source()
            result = pipeline.analyze(input_path, self._settings,
                                      progress_cb=self._emit, comments=comments)
            self.finished_ok.emit({"result": result, "input_path": input_path})
        except Exception as e:  # noqa: BLE001 (GUI へ集約通知)
            _logger.exception("アーカイブ採点に失敗")
            self.failed.emit(str(e))

    # モードに応じて (ローカル動画パス, 正規化コメント or None) を用意する
    def _prepare_source(self):
        if self._job.get("mode") == _MODE_TWITCH:
            return self._fetch_twitch()
        # ローカル: 任意で chat json を読み込みコメント採点を有効化
        input_path = self._job["input_path"]
        comments = None
        chat_path = self._job.get("chat_path")
        if chat_path:
            comments = comment_source.load_comments(chat_path)
        return input_path, comments

    # Twitch VOD/コメントを取得する (所有判定 → download → chat json)
    def _fetch_twitch(self):
        from ..modules import ffmpeg_runner
        dl = config.download_config(self._settings)
        auth_cfg = config.auth_config(self._settings)
        work_dir = config.resolve_work_dir(self._settings)
        video_id = twitch_source.extract_video_id(self._job["url"])
        # 同梱 ffmpeg のディレクトリ (twitch-dl の VOD 結合に PATH で渡す)
        ffmpeg_dir = os.path.dirname(
            ffmpeg_runner.get_ffmpeg_exe(self._settings.get("ffmpeg", {})))

        # 所有判定 (owner_only): 自分の VOD のみ許可 (resolve17 §8-1)
        if auth_cfg["owner_only"]:
            if not (self._auth and self._auth.is_logged_in()):
                raise TwitchError("Twitch にログインしてください (自分のVOD判定に必要です)。")
            if not self._auth.is_own_video(video_id):
                raise TwitchError("自分が所有する VOD のみ切り抜けます。他者の VOD は実行できません。")

        # VOD 取得。注: twitch-dl の --auth-token は Web クッキー系で Helix トークンとは別物のため、
        # ここでは付与しない (公開VODは不要)。sub-only 自VODはトークン貼付で補完 (resolve17 §4.3.0/§8-3b)。
        self.progress.emit(0.0, "VOD取得中…")
        input_path = twitch_source.download_vod(
            video_id, work_dir, dl["twitch_dl_path"], dl["vod_format"],
            progress_cb=lambda m: self.progress.emit(0.0, m), ffmpeg_dir=ffmpeg_dir)

        # コメント取得。失敗してもコメント無しで採点続行 (resolve17 §5)
        comments = None
        try:
            chat_path = twitch_source.download_chat(
                video_id, work_dir, dl["twitch_dl_path"],
                progress_cb=lambda m: self.progress.emit(0.0, m), ffmpeg_dir=ffmpeg_dir)
            comments = comment_source.load_comments(chat_path)
        except TwitchError as e:
            _logger.warning("コメント取得に失敗 (コメント無しで継続): %s", e)
            comments = None
        return input_path, comments

    def _emit(self, ratio, label):
        self.progress.emit(float(ratio), str(label))


# 準備(文字起こし)+一括レビュー+切り抜き+字幕焼き込みをワーカースレッドで実行する (R2)
class ArchiveClipWorker(QThread):
    progress = Signal(float, str)
    finished_ok = Signal(object)   # 出力パスのリスト
    failed = Signal(str)

    def __init__(self, input_path, settings, clips, result_callback=None,
                 curve=None, parent=None):
        super().__init__(parent)
        self._input_path = input_path
        self._settings = settings
        self._clips = clips
        self._result_callback = result_callback
        self._curve = curve or []

    def run(self):
        try:
            # prepare(全クリップ文字起こし) → 一括結果画面(result_callback) → burn → 結合
            outputs = clip_writer.write_clips(
                self._input_path, self._settings, self._clips,
                progress_cb=self._emit, result_callback=self._result_callback,
                curve=self._curve)
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
        self._login_worker = None
        self._review_bridge = None  # テロップ編集画面の橋渡し (GC 防止のため保持)
        self._pending_input = None  # 採点対象の入力パス (採点→切り抜きで引き継ぐ)
        self._auth = None           # TwitchAuth (ログイン状態を保持)
        # 無音カット可否チェック (resolve20 §5.8)。機能無効時は UI を作らないため None。
        self.silence_cut_check = None
        if self._enabled:
            self._build_ui()
            self._init_auth()
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
            "動画を採点し、上位の見どころを切り抜いて字幕を焼き込みます。\n"
            "入力はローカル動画、または Twitch VOD (ログインして自分のアーカイブを自動取得) が選べます。"
        ))

        # 入力モード選択 (ローカル / Twitch)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("入力ソース:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("ローカル動画", _MODE_LOCAL)
        self.mode_combo.addItem("Twitch VOD", _MODE_TWITCH)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self.mode_combo)
        mode_row.addStretch(1)
        root.addLayout(mode_row)

        # --- ローカル入力グループ ---
        self.local_group = QWidget()
        local_layout = QVBoxLayout(self.local_group)
        local_layout.setContentsMargins(0, 0, 0, 0)
        input_row = QHBoxLayout()
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("採点する動画ファイルを選択、または「参照...」")
        input_row.addWidget(self.input_edit)
        self.browse_button = QPushButton("参照...")
        self.browse_button.clicked.connect(self._on_browse)
        input_row.addWidget(self.browse_button)
        local_layout.addLayout(input_row)
        # 任意: ローカル chat json (コメント採点を Twitch 未連携でも試せる)
        chat_row = QHBoxLayout()
        self.chat_edit = QLineEdit()
        self.chat_edit.setPlaceholderText("(任意) コメントJSONを指定するとコメント採点が有効に")
        chat_row.addWidget(self.chat_edit)
        self.chat_browse_button = QPushButton("参照...")
        self.chat_browse_button.clicked.connect(self._on_browse_chat)
        chat_row.addWidget(self.chat_browse_button)
        local_layout.addLayout(chat_row)
        root.addWidget(self.local_group)

        # --- Twitch 入力グループ ---
        self.twitch_group = QWidget()
        twitch_layout = QVBoxLayout(self.twitch_group)
        twitch_layout.setContentsMargins(0, 0, 0, 0)
        login_row = QHBoxLayout()
        self.login_button = QPushButton("Twitch ログイン")
        self.login_button.clicked.connect(self._on_login)
        login_row.addWidget(self.login_button)
        self.login_status = QLabel("未ログイン")
        login_row.addWidget(self.login_status)
        login_row.addStretch(1)
        twitch_layout.addLayout(login_row)
        # 自分の VOD 一覧 (ログイン後に populate。選択で URL 自動入力)
        self.vod_combo = QComboBox()
        self.vod_combo.addItem("(ログイン後に自分のVODを選択)", "")
        self.vod_combo.currentIndexChanged.connect(self._on_vod_selected)
        twitch_layout.addWidget(self.vod_combo)
        # VOD URL 入力
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://www.twitch.tv/videos/xxxxxxxxx")
        twitch_layout.addWidget(self.url_edit)
        root.addWidget(self.twitch_group)

        # 実行オプション: 無音カットの可否 (resolve20 §5.8 / R7)
        # 初期値は setting.json (archive.clip_pipeline.silence_cut) に従う。
        option_row = QHBoxLayout()
        self.silence_cut_check = QCheckBox("無音カット")
        self.silence_cut_check.setToolTip(
            "各クリップから無音区間を除去します。"
            "外すと切り抜き区間をそのまま使用します。"
        )
        self.silence_cut_check.setChecked(self._silence_cut_default())
        option_row.addWidget(self.silence_cut_check)
        option_row.addStretch(1)
        root.addLayout(option_row)

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

        # 初期モード反映 (既定はローカル)
        self._on_mode_changed()

    # 無音カットチェックの初期値を設定から取得する (resolve20 §5.8)
    # 参照先は既存キー archive.clip_pipeline.silence_cut (既定 True)。新規キーは追加しない。
    def _silence_cut_default(self):
        clip_pipe = self._settings.get("archive", {}).get("clip_pipeline", {})
        return bool(clip_pipe.get("silence_cut", True))

    # チェック状態を「その実行の設定」へ反映する (resolve20 §5.8 / §10-10)
    # setting.json へは書き戻さない (初期値は毎回設定から復元する)。
    def _apply_silence_cut_option(self):
        if self.silence_cut_check is None:
            return
        enabled = bool(self.silence_cut_check.isChecked())
        clip_pipe = self._settings.setdefault("archive", {}).setdefault("clip_pipeline", {})
        clip_pipe["silence_cut"] = enabled
        _logger.info("無音カット: %s (アーカイブタブの選択)", "ON" if enabled else "OFF")

    # 設定から TwitchAuth を用意する (保存済みトークンがあればログイン状態を復元)
    def _init_auth(self):
        auth_cfg = config.auth_config(self._settings)
        self._auth = TwitchAuth(
            client_id=auth_cfg["client_id"],
            client_secret=auth_cfg["client_secret"],
            redirect_port=auth_cfg["redirect_port"],
            token_path=config.twitch_token_path(self._settings),
        )
        if self._auth.is_logged_in():
            # 保存済みトークンで自分情報を引ければログイン表示にする (失敗時は未ログイン)
            try:
                me = self._auth.get_self()
                self._set_logged_in(me)
            except Exception:  # noqa: BLE001 (トークン失効等は未ログイン扱い)
                self.login_status.setText("未ログイン")

    def _current_mode(self):
        return self.mode_combo.currentData() if hasattr(self, "mode_combo") else _MODE_LOCAL

    def _on_mode_changed(self, *args):
        is_twitch = self._current_mode() == _MODE_TWITCH
        self.local_group.setVisible(not is_twitch)
        self.twitch_group.setVisible(is_twitch)

    def _tick_spinner(self):
        frame = _SPINNER_FRAMES[self._spinner_index % len(_SPINNER_FRAMES)]
        self._spinner_index += 1
        self.status_label.setText(f"{frame} {self._spinner_message}")

    def _on_browse(self):
        start_dir = self._settings.get("general", {}).get("video_directory", "")
        path, _ = QFileDialog.getOpenFileName(self, "採点する動画を選択", start_dir, _VIDEO_FILE_FILTER)
        if path:
            self.input_edit.setText(path)

    def _on_browse_chat(self):
        start_dir = self._settings.get("general", {}).get("video_directory", "")
        path, _ = QFileDialog.getOpenFileName(self, "コメントJSONを選択", start_dir, _CHAT_FILE_FILTER)
        if path:
            self.chat_edit.setText(path)

    # ---- Twitch ログイン --------------------------------------------------
    def _on_login(self):
        self._settings = load_settings()  # 最新の設定(client_id 等)を反映
        auth_cfg = config.auth_config(self._settings)
        if not auth_cfg["client_id"]:
            QMessageBox.warning(
                self, "設定が必要",
                "Twitch の Client-ID が未設定です。\n"
                "dev.twitch.tv でアプリを登録し、setting.json の archive.auth.client_id に\n"
                "設定してください (Client-Secret は不要です)。\n"
                f"リダイレクトURL には http://localhost:{auth_cfg['redirect_port']} を登録します。")
            return
        # ログイン前に最新設定で auth を作り直す
        self._init_auth()
        self.login_status.setText("ブラウザで認可してください…")
        self.login_button.setEnabled(False)
        self._login_worker = TwitchLoginWorker(self._auth, parent=self)
        self._login_worker.finished_ok.connect(self._on_login_ok)
        self._login_worker.failed.connect(self._on_login_failed)
        self._login_worker.start()

    def _on_login_ok(self, me):
        self.login_button.setEnabled(True)
        self._set_logged_in(me)
        self._populate_own_vods()

    def _on_login_failed(self, message):
        self.login_button.setEnabled(True)
        self.login_status.setText("ログイン失敗")
        QMessageBox.critical(self, "ログイン失敗", f"Twitch ログインに失敗しました。\n{message}")

    def _set_logged_in(self, me):
        name = (me or {}).get("display_name") or (me or {}).get("login") or "?"
        self.login_status.setText(f"ログイン中: {name}")

    def _populate_own_vods(self):
        try:
            videos = self._auth.list_own_videos(first=20)
        except Exception as e:  # noqa: BLE001 (一覧取得失敗は致命でない)
            _logger.warning("自VOD一覧の取得に失敗: %s", e)
            return
        self.vod_combo.blockSignals(True)
        self.vod_combo.clear()
        self.vod_combo.addItem("(自分のVODを選択)", "")
        for v in videos:
            label = f"{v.get('created_at', '')[:10]}  {v.get('title', '')}".strip()
            self.vod_combo.addItem(label or v.get("url", ""), v.get("url", ""))
        self.vod_combo.blockSignals(False)

    def _on_vod_selected(self, *args):
        url = self.vod_combo.currentData()
        if url:
            self.url_edit.setText(url)

    # ---- 採点開始 --------------------------------------------------------
    def _on_analyze(self):
        self._settings = load_settings()  # 最新化
        # 無音カット可否 (チェックボックス) を当該実行の設定へ反映する (resolve20 §5.8)
        self._apply_silence_cut_option()
        mode = self._current_mode()

        if mode == _MODE_TWITCH:
            url = self.url_edit.text().strip()
            if not url:
                QMessageBox.warning(self, "入力エラー", "Twitch VOD の URL を入力してください。")
                return
            job = {"mode": _MODE_TWITCH, "url": url}
            self._spinner_message = "VOD取得中…"
        else:
            input_path = self.input_edit.text().strip()
            if not input_path or not os.path.exists(input_path):
                QMessageBox.warning(self, "入力エラー", "存在する動画ファイルを指定してください。")
                return
            chat_path = self.chat_edit.text().strip() or None
            if chat_path and not os.path.exists(chat_path):
                QMessageBox.warning(self, "入力エラー", "指定したコメントJSONが見つかりません。")
                return
            job = {"mode": _MODE_LOCAL, "input_path": input_path, "chat_path": chat_path}
            self._pending_input = input_path
            self._spinner_message = "採点準備中…"

        self._analyze_worker = ArchiveAnalyzeWorker(job, self._settings, auth=self._auth, parent=self)
        self._analyze_worker.progress.connect(self._on_progress)
        self._analyze_worker.finished_ok.connect(self._on_analyze_done)
        self._analyze_worker.failed.connect(self._on_failed)

        self._set_running(True)
        self._analyze_worker.start()

    # 採点完了 → 全クリップ準備(文字起こし) → 1つの結果画面で一括編集 → 完了で切り抜き (R2)
    def _on_analyze_done(self, payload):
        self._set_running(False)
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        # twitch モードで取得したローカル実体パスを以降の切り抜きに使う
        self._pending_input = payload.get("input_path") if isinstance(payload, dict) else self._pending_input
        clips = result.get("clips", []) if isinstance(result, dict) else []
        curve = result.get("curve", []) if isinstance(result, dict) else []
        if not clips:
            QMessageBox.information(self, "採点結果", "切り抜き候補が見つかりませんでした。")
            self.status_label.setText("候補なし")
            return

        # 追加フォント(settings/fonts)を Qt へ登録し編集画面の一覧へ反映する (resolve16 §4.2)
        register_fonts_in_dir(resolve_fonts_dir(self._settings))
        subtitle_cfg = self._settings.get("subtitle", {})

        # 一括結果画面 (採点グラフ+字幕編集+プレビュー) をメインスレッドで開く橋渡し (R2)。
        self._review_bridge = ArchiveResultBridge(
            parent_window=self,
            settings=self._settings,
            source_path=self._pending_input,
            default_font=subtitle_cfg.get("font_family", ""),
            default_size=subtitle_cfg.get("font_size", None),
            font_families=list(QFontDatabase.families()),
            theme_placeholder=_THEME_PLACEHOLDER,
        )

        # 準備+一括編集+切り抜き+焼き込み+結合を開始
        self._clip_worker = ArchiveClipWorker(
            self._pending_input, self._settings, clips,
            result_callback=self._review_bridge, curve=curve, parent=self)
        self._clip_worker.progress.connect(self._on_progress)
        self._clip_worker.finished_ok.connect(self._on_clip_done)
        self._clip_worker.failed.connect(self._on_failed)
        self._spinner_message = "文字起こし中…"
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
        self.chat_browse_button.setEnabled(not running)
        self.chat_edit.setEnabled(not running)
        self.mode_combo.setEnabled(not running)
        self.url_edit.setEnabled(not running)
        self.vod_combo.setEnabled(not running)
        self.login_button.setEnabled(not running)
        # 実行中は無音カットの可否を変更できないようにする (resolve20 §5.8)
        if self.silence_cut_check is not None:
            self.silence_cut_check.setEnabled(not running)
        if running:
            self._spinner_timer.start()
        else:
            self._spinner_timer.stop()
