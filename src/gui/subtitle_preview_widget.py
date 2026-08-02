# 字幕適用プレビューウィジェット (resolve23)
# クリップ用 字幕一覧 (SubtitleEditorDialog) とアーカイブ用 結果画面 (ArchiveResultWindow)
# の双方へ埋め込む共通部品。選択行の時刻から preview.segment_sec 秒だけ、本番と同一の
# ASS (build_subtitle_file) を焼き込んだ低解像度 mp4 を生成し QtMultimedia で再生する。
# QtMultimedia 不可環境・再生失敗時は字幕焼き込み済み静止画へフォールバックする。
# preview.enabled=false のときは従来同等の先頭フレーム静止画 (字幕なし) のみ表示する。
import os
import shutil
import subprocess
import tempfile

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

# QtMultimedia はビルド/環境により使用不可のことがあるため防御的に import する (§7)
try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    _MULTIMEDIA_AVAILABLE = True
except Exception:  # noqa: BLE001 (import 失敗は静止画モードへフォールバック)
    _MULTIMEDIA_AVAILABLE = False

# 確定時と同一の字幕整形 (使用チェック+wrap_lines) を再利用する (resolve23 §5.2-1)
from ..archive.clip_writer import _finalize_timeline
from ..modules import ffmpeg_runner
from ..modules.subtitle_generator import build_font_profile, build_subtitle_file
from ..settings.settings_window import resolve_fonts_dir
from ..utils.logger import get_logger
from ..utils.proc import no_window_creationflags

_logger = get_logger(__name__)

# プレビュー映像の画面表示幅 (高さはアスペクト維持。生成解像度は preview.width)
_DISPLAY_WIDTH = 480
# 生成区間の最短秒数 (動画末尾で区間が確保できない場合の下限)
_MIN_SEGMENT_SEC = 0.5


# プレビュー欄を出せるか (注入の有無 + 対象動画の存在 + 設定の有効/無効 / §5.3)
def is_preview_enabled(preview_context):
    if not preview_context:
        return False
    video_path = preview_context.get("video_path") or ""
    if not video_path or not os.path.exists(video_path):
        return False
    settings = preview_context.get("settings") or {}
    return bool(settings.get("preview", {}).get("enabled", True))


# プレビュー生成ワーカー (ffmpeg 実行を GUI スレッド外で行う / §4-4)
# 生成完了で finished_job(job_id, kind, path) を emit する (失敗は path="")。
# cancel() は実行中の ffmpeg を kill し、結果を破棄扱いにする。
class _PreviewWorker(QThread):

    finished_job = Signal(int, str, str)  # (job_id, "video"|"frame", 出力パス or "")

    def __init__(self, job_id, kind, cmd, out_path, parent=None):
        super().__init__(parent)
        self._job_id = job_id
        self._kind = kind
        self._cmd = cmd
        self._out_path = out_path
        self._proc = None
        self._cancelled = False

    def run(self):
        ok = False
        try:
            self._proc = subprocess.Popen(
                self._cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                # GUI(windowed)実行時にコンソール窓を出さない (Windows のみ有効)
                creationflags=no_window_creationflags(),
            )
            _out, err = self._proc.communicate()
            ok = self._proc.returncode == 0 and os.path.exists(self._out_path)
            if not ok and not self._cancelled:
                text = err.decode("utf-8", errors="replace") if err else ""
                _logger.warning("プレビュー生成に失敗: %s", text[-500:])
        except OSError as e:
            _logger.warning("プレビュー生成でエラー: %s", e)
        emit_path = self._out_path if (ok and not self._cancelled) else ""
        self.finished_job.emit(self._job_id, self._kind, emit_path)

    # 実行中の ffmpeg を止め、結果を破棄扱いにする (画面クローズ・ジョブ差し替え時)
    def cancel(self):
        self._cancelled = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


# プレビューウィジェット本体 (resolve23 §5.2)
# set_source() で対象動画と実効字幕設定を受け取り、埋め込み側の編集テーブル
# (SubtitleEditorWidget) と bind_editor() で連動する。
class SubtitlePreviewWidget(QWidget):

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings or {}
        cfg = self._settings.get("preview", {})
        # プレビュー設定 (setting.json preview 節 / §6)
        self._enabled = bool(cfg.get("enabled", True))
        self._segment_sec = float(cfg.get("segment_sec", 20))
        self._width = int(cfg.get("width", 640))
        self._preset = str(cfg.get("preset", "ultrafast") or "ultrafast")
        self._crf = int(cfg.get("crf", 28))
        self._audio_enabled = bool(cfg.get("audio_enabled", True))
        self._ffmpeg_cfg = self._settings.get("ffmpeg", {})
        self._fonts_dir = resolve_fonts_dir(self._settings)

        # プレビュー対象 (set_source で更新)
        self._video_path = ""
        self._eff_cfg = {}
        self._profile = None
        self._duration = None  # 対象動画の尺 (キャッシュ)

        # 編集内容の取得元 (result_items 相当) と連動先エディタ
        self._items_provider = None
        self._bound_editor = None

        # 生成ジョブ管理 (最新 job_id 以外の完了通知は破棄する / §4-4)
        self._job_seq = 0
        self._workers = []

        # 生成済み区間 [seg_start, seg_start+seg_len] (シーク可否の判定用)
        self._seg_start = 0.0
        self._seg_len = 0.0
        self._has_video = False
        self._pending_anchor = 0.0
        self._current_media = ""  # 再生中の一時 mp4 (差し替え時に削除)

        # QtMultimedia を使うか (設定 ON かつ import 成功)
        self._use_player = self._enabled and _MULTIMEDIA_AVAILABLE
        if self._enabled and not _MULTIMEDIA_AVAILABLE:
            _logger.info("QtMultimedia が利用できないため静止画プレビューで動作します")

        self._tmp_dir = tempfile.mkdtemp(prefix="subtitle_preview_")
        self._build_ui()

    # ------------------------------------------------------------------
    # UI 構築
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # 見出し (テーマ演出はプレビュー対象外である旨を明示する / §4-8)
        title = "プレビュー (字幕適用 / テーマ演出は含まれません)" if self._enabled else "プレビュー"
        root.addWidget(QLabel(title))

        # 映像エリア: 動画 (QVideoWidget) と静止画 (QLabel) を切り替える
        self._stack = QStackedWidget(self)
        self._static_label = QLabel("プレビューを読み込み中…")
        self._static_label.setAlignment(Qt.AlignCenter)
        self._static_label.setMinimumSize(_DISPLAY_WIDTH, int(_DISPLAY_WIDTH * 9 / 16))
        self._static_label.setStyleSheet("background:#111; color:#aaa;")
        self._stack.addWidget(self._static_label)

        self._player = None
        self._audio = None
        self._video_widget = None
        if self._use_player:
            self._video_widget = QVideoWidget(self)
            self._video_widget.setMinimumSize(_DISPLAY_WIDTH, int(_DISPLAY_WIDTH * 9 / 16))
            self._stack.addWidget(self._video_widget)
            self._audio = QAudioOutput(self)
            self._audio.setMuted(not self._audio_enabled)
            self._player = QMediaPlayer(self)
            self._player.setAudioOutput(self._audio)
            self._player.setVideoOutput(self._video_widget)
            self._player.positionChanged.connect(self._on_position_changed)
            self._player.durationChanged.connect(self._on_duration_changed)
            self._player.errorOccurred.connect(self._on_player_error)
        root.addWidget(self._stack, 1)

        # 再生コントロール (再生/一時停止・シーク・ミュート)。無効時は出さない。
        if self._use_player:
            control_row = QHBoxLayout()
            self.play_button = QPushButton("▶")
            self.play_button.setFixedWidth(36)
            self.play_button.clicked.connect(self._toggle_play)
            control_row.addWidget(self.play_button)
            self.seek_slider = QSlider(Qt.Horizontal)
            self.seek_slider.setRange(0, 0)
            self.seek_slider.sliderMoved.connect(self._on_slider_moved)
            control_row.addWidget(self.seek_slider, 1)
            self.mute_button = QPushButton("🔇" if not self._audio_enabled else "🔊")
            self.mute_button.setFixedWidth(36)
            self.mute_button.clicked.connect(self._toggle_mute)
            control_row.addWidget(self.mute_button)
            root.addLayout(control_row)

        # プレビュー更新 (編集内容を反映して再生成) と状態表示
        if self._enabled:
            update_row = QHBoxLayout()
            self.update_button = QPushButton("プレビュー更新")
            self.update_button.setToolTip(
                "現在の字幕編集内容を反映し、選択行の時刻からプレビューを生成し直します。"
            )
            self.update_button.clicked.connect(self._on_update_clicked)
            update_row.addWidget(self.update_button)
            self.status_label = QLabel("")
            self.status_label.setStyleSheet("color:#888;")
            update_row.addWidget(self.status_label, 1)
            root.addLayout(update_row)
        else:
            self.update_button = None
            self.status_label = None

    # ------------------------------------------------------------------
    # 公開 I/F (§5.2)
    # ------------------------------------------------------------------

    # プレビュー対象動画と実効字幕設定を設定する (アーカイブのクリップ切替でも呼ぶ)。
    # 生成済み区間は破棄し、静止画 (有効時は字幕適用済み) を自動表示する。
    def set_source(self, video_path, eff_cfg, profile):
        self._stop_player()
        self._video_path = video_path or ""
        self._eff_cfg = eff_cfg or {}
        self._profile = profile
        self._duration = None
        self._has_video = False
        self._seg_start = 0.0
        self._seg_len = 0.0
        self._pending_anchor = 0.0
        if not self._video_path or not os.path.exists(self._video_path):
            self._show_static_message("プレビューを表示できません")
            return
        # 有効時は字幕適用済みフレーム、無効時は従来同等の素のフレームを自動表示する
        self._request_frame(0.0, with_subtitles=self._enabled)

    # 現在の編集内容 (result_items 相当) を返す関数を登録する
    def set_items_provider(self, provider):
        self._items_provider = provider

    # 編集テーブル (SubtitleEditorWidget) と連動する:
    #   ・items_provider = editor.result_items
    #   ・行選択の変更 → 生成済み区間内はシーク / 区間外は案内表示 (§10 Q2)
    def bind_editor(self, editor):
        self._bound_editor = editor
        self.set_items_provider(editor.result_items)
        editor.table.currentCellChanged.connect(self._on_editor_cell_changed)

    # anchor_sec を起点に区間プレビューを生成する (完了後に自動再生)
    def request_preview(self, anchor_sec):
        if not self._enabled or not self._video_path:
            return
        self._pending_anchor = max(0.0, float(anchor_sec))
        if self._use_player:
            self._request_video(self._pending_anchor)
        else:
            # 再生不可環境: 字幕適用済み静止画をその時刻で更新する (§3 フォールバック)
            self._request_frame(self._pending_anchor, with_subtitles=True)

    # 指定秒へシークする。生成済み区間外は自動生成せず案内のみ出す (§10 Q2)
    def seek_to(self, sec):
        if not self._enabled:
            return
        sec = max(0.0, float(sec))
        self._pending_anchor = sec
        if (self._use_player and self._has_video
                and self._seg_start <= sec <= self._seg_start + self._seg_len):
            self._player.setPosition(int((sec - self._seg_start) * 1000))
            return
        if self._has_video:
            self._set_status("選択行は生成済み区間外です。「プレビュー更新」で生成できます。")

    # 再生停止・ソース解放・一時ファイル削除 (画面クローズ時に必ず呼ぶ / §5.2)
    def shutdown(self):
        for worker in self._workers:
            worker.cancel()
        self._stop_player()
        # Windows のファイルロック解放後に一時領域を削除する (失敗は握りつぶす / §7)
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # 生成ジョブ (§5.2 内部処理)
    # ------------------------------------------------------------------

    # 対象動画の尺を取得する (キャッシュ付き。失敗時は None)
    def _probe_duration(self):
        if self._duration is None:
            try:
                self._duration = ffmpeg_runner.probe_duration(
                    self._video_path, self._ffmpeg_cfg)
            except Exception:  # noqa: BLE001 (尺不明でも生成は継続する)
                self._duration = 0.0
        return self._duration or None

    # ASS の PlayRes に使うキャンバス寸法 (出力プロファイル優先 / §5.2-3)
    def _canvas(self):
        if self._profile:
            return int(self._profile["width"]), int(self._profile["height"])
        ffmpeg_cfg = self._ffmpeg_cfg
        return (int(ffmpeg_cfg.get("output_width", 1920)),
                int(ffmpeg_cfg.get("output_height", 1080)))

    # 編集内容を確定時と同一整形し、区間 [seg_start, seg_start+seg_len] へシフトする (§5.2-2)
    def _segment_items(self, seg_start, seg_len):
        items = self._items_provider() if callable(self._items_provider) else []
        finalized = _finalize_timeline(items or [], self._eff_cfg)
        shifted = []
        for e in finalized:
            start = max(float(e["start"]) - seg_start, 0.0)
            end = min(float(e["end"]) - seg_start, seg_len)
            if end <= start:
                continue
            entry = dict(e)
            entry["start"] = start
            entry["end"] = end
            shifted.append(entry)
        return shifted

    # 区間用の ASS を書き出す (本番と同一の build_subtitle_file / §3)
    def _write_segment_ass(self, seg_start, seg_len):
        canvas_w, canvas_h = self._canvas()
        ass_path = os.path.join(self._tmp_dir, f"preview_{self._job_seq}.ass")
        build_subtitle_file(
            self._segment_items(seg_start, seg_len),
            build_font_profile(self._eff_cfg),
            ass_path,
            video_width=canvas_w, video_height=canvas_h,
        )
        return ass_path

    # フィルタチェーンを組み立てる (縦動画はキャンバス正規化 → ass → プレビュー縮小 / §5.2-4)
    # ASS はフルキャンバスで生成済みのため、ass 適用後に縮小すれば描画比率が本番と一致する。
    def _build_filter(self, ass_path):
        chain = ""
        if self._profile and self._profile.get("is_portrait"):
            canvas_w, canvas_h = self._canvas()
            chain = (
                f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,"
                f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            )
        if ass_path:
            # burn_subtitle と同一の2段階エスケープ ('\'→'/', ':'→'\:')
            safe = ass_path.replace("\\", "/").replace(":", "\\:")
            opt = f"ass='{safe}'"
            if self._fonts_dir and os.path.isdir(self._fonts_dir):
                safe_dir = self._fonts_dir.replace("\\", "/").replace(":", "\\:")
                opt += f":fontsdir='{safe_dir}'"
            chain += f"{opt},"
        return f"{chain}scale={self._width}:-2"

    # 区間動画の生成を開始する (完了で _on_worker_done → 再生)
    def _request_video(self, anchor_sec):
        duration = self._probe_duration()
        seg_start = anchor_sec
        if duration is not None:
            seg_start = min(seg_start, max(0.0, duration - _MIN_SEGMENT_SEC))
        seg_len = self._segment_sec
        if duration is not None:
            seg_len = max(min(seg_len, duration - seg_start), _MIN_SEGMENT_SEC)
        self._job_seq += 1
        ass_path = self._write_segment_ass(seg_start, seg_len)
        out_path = os.path.join(self._tmp_dir, f"preview_{self._job_seq}.mp4")
        ffmpeg = ffmpeg_runner.get_ffmpeg_exe(self._ffmpeg_cfg)
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{seg_start:.3f}", "-t", f"{seg_len:.3f}",
            "-i", self._video_path,
            "-vf", self._build_filter(ass_path),
            "-c:v", "libx264", "-preset", self._preset, "-crf", str(self._crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ac", "2",
            out_path,
        ]
        # 生成完了時にシーク判定へ使う区間を控えておく (ジョブと同時に確定)
        self._seg_start = seg_start
        self._seg_len = seg_len
        self._set_status("プレビュー生成中…")
        self._spawn_worker("video", cmd, out_path)

    # 字幕適用済み (または素の) 静止画1フレームの生成を開始する
    def _request_frame(self, anchor_sec, with_subtitles):
        self._job_seq += 1
        ass_path = self._write_segment_ass(anchor_sec, self._segment_sec) if with_subtitles else ""
        out_path = os.path.join(self._tmp_dir, f"preview_{self._job_seq}.png")
        ffmpeg = ffmpeg_runner.get_ffmpeg_exe(self._ffmpeg_cfg)
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, anchor_sec):.3f}",
            "-i", self._video_path,
            "-frames:v", "1",
            "-vf", self._build_filter(ass_path),
            out_path,
        ]
        self._set_status("プレビュー生成中…")
        self._spawn_worker("frame", cmd, out_path)

    # ワーカーを起動する (完了済みワーカーの参照はここで回収する)
    def _spawn_worker(self, kind, cmd, out_path):
        self._workers = [w for w in self._workers if not w.isFinished()]
        worker = _PreviewWorker(self._job_seq, kind, cmd, out_path, parent=self)
        worker.finished_job.connect(self._on_worker_done)
        self._workers.append(worker)
        worker.start()

    # 生成完了 (メインスレッド)。最新ジョブ以外の結果は破棄する。
    def _on_worker_done(self, job_id, kind, path):
        if job_id != self._job_seq:
            # 差し替え済みの古い結果 → 生成物だけ片づける
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            return
        if not path:
            self._show_static_message("プレビュー生成に失敗しました")
            self._set_status("プレビュー生成に失敗しました")
            return
        if kind == "frame":
            pixmap = QPixmap(path)
            if pixmap.isNull():
                self._show_static_message("プレビューを表示できません")
            else:
                self._static_label.setPixmap(
                    pixmap.scaledToWidth(_DISPLAY_WIDTH, Qt.SmoothTransformation))
                self._stack.setCurrentWidget(self._static_label)
            self._set_status("")
            return
        # kind == "video": 旧メディアを解放してから差し替えて再生する
        old_media = self._current_media
        self._stop_player()
        if old_media:
            try:
                os.remove(old_media)
            except OSError:
                pass  # ロック中は shutdown の一括削除に委ねる
        self._current_media = path
        self._has_video = True
        self._player.setSource(QUrl.fromLocalFile(path))
        self._stack.setCurrentWidget(self._video_widget)
        self._player.play()
        self.play_button.setText("⏸")
        self._set_status(
            f"{self._seg_start:.1f}s から {self._seg_len:.1f}s 区間を表示中")

    # ------------------------------------------------------------------
    # 再生コントロール・連動
    # ------------------------------------------------------------------

    # 再生を止めてソースを解放する (Windows のファイルロック対策 / §5.2)
    def _stop_player(self):
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())
        self._has_video = False

    def _toggle_play(self):
        if self._player is None or not self._has_video:
            return
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
            self.play_button.setText("▶")
        else:
            self._player.play()
            self.play_button.setText("⏸")

    def _toggle_mute(self):
        if self._audio is None:
            return
        muted = not self._audio.isMuted()
        self._audio.setMuted(muted)
        self.mute_button.setText("🔇" if muted else "🔊")

    def _on_slider_moved(self, value):
        if self._player is not None and self._has_video:
            self._player.setPosition(value)

    def _on_position_changed(self, position):
        # ユーザー操作中はスライダーを奪わない
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(position)

    def _on_duration_changed(self, duration):
        self.seek_slider.setRange(0, max(0, duration))

    # 再生エラー → 字幕適用済み静止画へフォールバックする (§7)
    def _on_player_error(self, error, error_string):
        if error == QMediaPlayer.NoError:
            return
        _logger.warning("プレビュー再生エラー → 静止画へフォールバック: %s", error_string)
        self._stop_player()
        self._request_frame(self._seg_start, with_subtitles=True)

    # 編集テーブルの行選択変更 → 選択行の開始時刻へ連動する
    def _on_editor_cell_changed(self, row, _col, _prev_row, _prev_col):
        if self._bound_editor is None:
            return
        start = self._bound_editor.row_start(row)
        if start is not None:
            self.seek_to(start)

    # 「プレビュー更新」: 現在の選択行 (無ければ直近アンカー) から再生成する
    def _on_update_clicked(self):
        anchor = self._pending_anchor
        if self._bound_editor is not None:
            start = self._bound_editor.row_start(self._bound_editor.table.currentRow())
            if start is not None:
                anchor = start
        self.request_preview(anchor)

    # 静止画エリアへメッセージを表示する
    def _show_static_message(self, text):
        self._static_label.setPixmap(QPixmap())
        self._static_label.setText(text)
        self._stack.setCurrentWidget(self._static_label)

    def _set_status(self, text):
        if self.status_label is not None:
            self.status_label.setText(text)
