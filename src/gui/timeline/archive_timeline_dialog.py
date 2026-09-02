# アーカイブ切り抜き用の Timeline 編集画面 (docs/request/ver3/resolve5.md §5.4)
#
# クリップ用の TimelineEditorDialog を継承し、アーカイブ固有の 2 つを足す。
#   ・採点グラフ (ScoreGraphWidget) をドックとして載せる。既定は最上部で、
#     左右下への移動・切り離しができ、配置は setting.json へ覚える (§3-7)。
#   ・クリップバー (選択・使用可否・テーマ) を Timeline の上へ固定で置く (§3-4)。
#
# Timeline には選ばれたクリップが 1 本に並んでおり、クリップ単位への切り分けは
# 書き出し時に archive.timeline_builder.split_by_clip() が行う (§3-8)。
import base64
import os

from PySide6.QtCore import QByteArray, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QWidget,
)

from ...archive import clip_writer
from ...modules import ffmpeg_runner
from ...archive import config as archive_config
from ...archive import timeline_builder as archive_timeline
from ...export import resolve_export
from ...timeline import project_io
from ...settings.settings_window import save_settings
from ...utils.logger import get_logger
from ..score_graph_widget import ScoreGraphWidget
from ..subtitle_editor_dialog import run_resolve_export
from .section_add_dialog import SectionAddDialog, format_hms
from .timeline_editor_dialog import TimelineEditorDialog

_logger = get_logger(__name__)

# 設定値 "top"/"left"/"right"/"bottom" → Qt のドック領域
_DOCK_AREAS = {
    "top": Qt.TopDockWidgetArea,
    "bottom": Qt.BottomDockWidgetArea,
    "left": Qt.LeftDockWidgetArea,
    "right": Qt.RightDockWidgetArea,
}

# 秒を "H:MM:SS" 表記へ (クリップバーの区間表示用)。
# 追加ダイアログと同じ表記にするため実装を 1 つに寄せる (ver3 resolve13 §5.3)。
_fmt = format_hms


class ArchiveTimelineDialog(TimelineEditorDialog):

    # timeline    : archive.timeline_builder.build_archive_timeline の結果
    # prepared    : clip_writer._prepare_clips の戻り値 (TOP 順)
    # curve       : 採点グラフ用の窓スコア列
    # source_path : 元 VOD (Resolve 出力に使う)
    # project_path/created_at/mode: 保存済みプロジェクトを開き直したときに
    #   保存先と初回作成時刻を引き継ぐ (ver3 resolve9 §5.4)
    # clip_workdir / clip_settings : セクション追加 (ver3 resolve13) に要る。
    #   clip_workdir  = write_clips の作業ディレクトリ (新素材の置き場。寿命が合う)
    #   clip_settings = OP/ED を無効化した設定コピー
    #   どちらかが無い経路では追加機能を出さない (従来の呼び出しがそのまま動く)。
    def __init__(self, timeline, prepared, curve, source_path, settings, work_dir,
                 parent=None, project_path=None, created_at=None, mode="pipeline",
                 clip_workdir=None, clip_settings=None):
        self._prepared = list(prepared or [])
        # 番号 → 準備済みデータ。画面が見せる一覧は source.archive.clips から作るため
        # (resolve13 §3-6)、ここは書き出しが要る normalized_path などの置き場に徹する。
        self._prepared_by_index = {p["index"]: p for p in self._prepared}
        self._curve = list(curve or [])
        self._source_path = source_path or ""
        self._current = self._prepared[0]["index"] if self._prepared else None
        self._syncing = False
        self._dock_host = None
        # セクション追加 (ver3 resolve13)
        self._clip_workdir = clip_workdir
        self._clip_settings = clip_settings
        self._section_cfg = archive_config.section_add_config(settings or {})
        self._section_worker = None
        self._section_progress = None
        self._vod_duration = 0.0
        super().__init__(timeline, settings, work_dir, parent=parent,
                         project_path=project_path, created_at=created_at, mode=mode)
        self.setWindowTitle("切り抜き編集 (採点結果)")
        # 再生ヘッドの移動でクリップバーの対象を切り替える (§3-4)
        self.controller.playhead_moved.connect(self._on_playhead_moved)
        self.controller.timeline_changed.connect(self._sync_clip_bar)
        self._sync_clip_bar()

    # ------------------------------------------------------------------
    # 画面構築 (基底のフック)
    # ------------------------------------------------------------------

    # 主要部を QMainWindow で包み、採点グラフをドックとして載せる (§3-7)
    def _wrap_content(self, content):
        # クリップバーは中央側へ固定する。ドックを閉じても使用可否・テーマを操作できる
        content.layout().insertWidget(0, self._build_clip_bar())

        host = QMainWindow()
        host.setCentralWidget(content)

        self.graph = ScoreGraphWidget()
        self.graph.set_data(self._curve, self._clips_meta())
        self.graph.clip_selected.connect(self.jump_to_clip)

        self.graph_dock = QDockWidget("採点グラフ")
        # saveState はドックを objectName で識別する (未設定だと復元できない)
        self.graph_dock.setObjectName("archiveScoreGraph")
        self.graph_dock.setWidget(self.graph)
        self.graph_dock.setAllowedAreas(
            Qt.TopDockWidgetArea | Qt.BottomDockWidgetArea
            | Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        ui_cfg = self.controller.cfg["ui"]
        host.addDockWidget(
            _DOCK_AREAS.get(str(ui_cfg["archive_graph_dock_area"]), Qt.TopDockWidgetArea),
            self.graph_dock)
        self.graph_dock.setMinimumHeight(int(ui_cfg["archive_graph_dock_height_px"]))

        self._dock_host = host
        self._restore_dock_state(ui_cfg)
        return host

    def _decide_button_text(self):
        return "完了（切り抜き＋字幕焼き込み）"

    # 保存まわりのフック (ver3 resolve9 §5.4)。
    # resolve7 §3-6 では保存 UI を出していなかったが、素材の復旧 (VOD からの
    # 切り直し + 音声サイドカー) とテーマ・採点値の保存を用意したため開放した。

    def _project_kind(self):
        return project_io.KIND_ARCHIVE

    # 既定の保存名は <VOD名>.archive.timeline.json (クリップ用と衝突させない)
    def _default_project_path(self):
        return project_io.default_project_path(
            self._settings,
            (self.controller.timeline.source or {}).get("input_path", ""),
            name_suffix=self.controller.cfg["project"]["archive_suffix"])

    def _title_base(self):
        return "切り抜き編集"

    # keep_media / keep_audio の対象は V1 が参照する全クリップ素材
    def _media_to_copy(self):
        timeline = self.controller.timeline
        media_list = []
        seen = set()
        for entry in (archive_timeline.archive_section(timeline).get("clips") or []):
            media_id = str(entry.get("media_id", "") or "")
            if not media_id or media_id in seen:
                continue
            seen.add(media_id)
            media = timeline.media_by_id(media_id)
            if media is not None:
                media_list.append(media)
        return media_list

    # クリップ選択・使用可否・テーマの行 (§3-4)
    def _build_clip_bar(self):
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(6, 4, 6, 4)

        layout.addWidget(QLabel("クリップ"))
        self.clip_combo = QComboBox()
        self.clip_combo.setMinimumWidth(
            int(self.controller.cfg["ui"]["archive_clip_selector_width_px"]))
        self._rebuild_clip_combo()
        self.clip_combo.activated.connect(self._on_clip_selected)
        layout.addWidget(self.clip_combo)

        # セクション追加 (ver3 resolve13)。素材の置き場と設定が渡らない経路
        # (従来画面からの流用・CLI) では出さない。
        self.add_section_button = None
        if self._section_add_available():
            self.add_section_button = QPushButton("セクション追加...")
            self.add_section_button.setAutoDefault(False)
            self.add_section_button.setToolTip(
                "元動画の区間を指定してセクションを追加します。\n"
                "元動画の時系列に合わせた位置へ挿入され、"
                "既存セクションと重なる場合は 1 つへ統合されます。")
            self.add_section_button.clicked.connect(self._on_add_section)
            layout.addWidget(self.add_section_button)

        self.use_check = QCheckBox("このクリップを使用する")
        self.use_check.setToolTip(
            "外すと Timeline 上で灰色になり、書き出しから外れます (戻せます)")
        self.use_check.setChecked(True)
        self.use_check.toggled.connect(self._on_use_toggled)
        layout.addWidget(self.use_check)

        layout.addWidget(QLabel("テーマ"))
        self.theme_edit = QLineEdit()
        self.theme_edit.setPlaceholderText("空欄=テーマ演出なし")
        self.theme_edit.setToolTip(
            "入力するとクリップ先頭にイントロカード、本編に左上タグが付きます")
        self.theme_edit.editingFinished.connect(self._on_theme_changed)
        layout.addWidget(self.theme_edit, 1)
        return bar

    # 画面が見せるセクション一覧 (ver3 resolve13 §3-6)
    # source.archive.clips から作るため、追加を Undo すれば自動的に消える。
    # 番号は振り直さないため昇順にならない。並びは Timeline 上の位置で決める。
    def _sections(self):
        timeline = self.controller.timeline
        rows = []
        for entry in (archive_timeline.archive_section(timeline).get("clips") or []):
            if not isinstance(entry, dict) or entry.get("index") is None:
                continue
            span = archive_timeline.clip_range(timeline, entry.get("index"))
            if span is None:
                continue            # Timeline 上に実体が無い = 見せない
            rows.append({"index": entry.get("index"),
                         "start": float(entry.get("vod_start", 0.0)),
                         "end": float(entry.get("vod_end", 0.0)),
                         "score": float(entry.get("score", 0.0)),
                         "timeline_start": span[0]})
        rows.sort(key=lambda r: r["timeline_start"])
        return rows

    # 採点グラフへ渡すマーカ情報
    def _clips_meta(self):
        return [{"index": row["index"], "start": row["start"],
                 "end": row["end"], "score": row["score"]}
                for row in self._sections()]

    # クリップバーの選択肢を今のセクション一覧で作り直す
    def _rebuild_clip_combo(self):
        rows = self._sections()
        self.clip_combo.blockSignals(True)
        try:
            self.clip_combo.clear()
            for row in rows:
                self.clip_combo.addItem(
                    f"clip{row['index']}  {_fmt(row['start'])}→{_fmt(row['end'])}"
                    f"  点{row['score']:.1f}", row["index"])
        finally:
            self.clip_combo.blockSignals(False)
        # 選択中のセクションが消えていたら先頭へ寄せる
        if rows and not any(r["index"] == self._current for r in rows):
            self._current = rows[0]["index"]

    # ------------------------------------------------------------------
    # クリップの選択・属性
    # ------------------------------------------------------------------

    # 指定クリップの先頭へ再生ヘッドを移動する (グラフのマーカ / コンボから)
    def jump_to_clip(self, clip_index):
        span = archive_timeline.clip_range(self.controller.timeline, clip_index)
        if span is None:
            return
        self._current = clip_index
        # 再生ヘッド移動で TimelinePanel が可視域へ追従する
        self.controller.set_playhead(span[0])
        self._sync_clip_bar()

    def _on_clip_selected(self, _row):
        index = self.clip_combo.currentData()
        if index is not None:
            self.jump_to_clip(index)

    # 再生ヘッドが別クリップへ入ったら対象を切り替える (クリップ外では保持)
    def _on_playhead_moved(self, sec):
        index = archive_timeline.clip_index_at(self.controller.timeline, sec)
        if index is None or index == self._current:
            return
        self._current = index
        self._sync_clip_bar()

    # 使用可否: そのクリップ由来の V1 クリップの enabled をまとめて切り替える (§3-4)
    def _on_use_toggled(self, checked):
        if self._syncing or self._current is None:
            return
        self.controller.execute(
            archive_timeline.SetArchiveClipEnabled(self._current, checked))
        self._update_history_buttons()

    # テーマは source.archive へ持たせ、コマンド経由で書き換える (ver3 resolve9 §3-3)。
    # こうすると Undo が効き、テーマだけ直して閉じた場合も未保存として扱える。
    def _on_theme_changed(self):
        if self._syncing or self._current is None:
            return
        if self.controller.execute(archive_timeline.SetArchiveClipTheme(
                self._current, self.theme_edit.text())):
            self._update_history_buttons()

    # クリップバーの表示を現在の選択へ合わせる
    def _sync_clip_bar(self):
        if self._current is None:
            return
        self._syncing = True
        try:
            # セクションが増減している場合があるため選択肢から作り直す
            self._rebuild_clip_combo()
            row = self.clip_combo.findData(self._current)
            if row >= 0:
                self.clip_combo.setCurrentIndex(row)
            self.use_check.setChecked(
                archive_timeline.is_clip_enabled(self.controller.timeline, self._current))
            self.theme_edit.setText(
                archive_timeline.clip_theme(self.controller.timeline, self._current))
            entry = self._entry(self._current)
            if entry is not None:
                self.graph.set_selected_range(entry.get("start"), entry.get("end"))
        finally:
            self._syncing = False

    def _entry(self, clip_index):
        for row in self._sections():
            if row["index"] == clip_index:
                return row
        return None


    # ------------------------------------------------------------------
    # セクションの追加 (ver3 resolve13)
    # ------------------------------------------------------------------

    # 追加機能を出せるか。新素材の置き場 (write_clips の作業ディレクトリ) と
    # OP/ED 無効化済みの設定が揃っている経路でのみ有効 (§5.6)。
    def _section_add_available(self):
        return bool(self._section_cfg["enabled"]
                    and self._clip_workdir
                    and self._clip_settings
                    and self._source_path
                    and os.path.exists(self._source_path))

    # 元動画の全長 (秒)。取れなければ 0.0 = 上限チェックをしない。
    # ffprobe を毎回叩かないよう 1 度だけ測る。
    def _vod_duration_sec(self):
        if self._vod_duration:
            return self._vod_duration
        try:
            self._vod_duration = float(ffmpeg_runner.probe_duration(
                self._source_path, self._settings.get("ffmpeg", {})) or 0.0)
        except Exception:  # noqa: BLE001 (測れなくても追加はできる)
            _logger.exception("元動画の全長を取得できませんでした")
            self._vod_duration = 0.0
        return self._vod_duration

    # 追加ダイアログの初期値。
    # 採点グラフのクリックも再生ヘッドの移動も選択セクション (_current) へ反映されるため、
    # 「今見ているセクションの元動画での開始時刻」を初期値にすれば、
    # グラフを見ながら数値を決める導線になる (resolve13 §3-1)。
    def _default_add_start(self):
        row = self._entry(self._current)
        return row["start"] if row else 0.0

    # 追加ダイアログに出す統合の予告 (破壊的に見える挙動を押す前に知らせる)
    def _preview_add(self, start, end):
        plan = archive_timeline.plan_section_add(
            self.controller.timeline, start, end,
            merge_on_overlap=self._section_cfg["merge_on_overlap"])
        if plan is None:
            return "この区間は既存セクションに含まれているため追加されません"
        if not plan["merge_indexes"]:
            return ""
        names = "・".join(f"clip{i}" for i in plan["merge_indexes"])
        span = plan["span"]
        return (f"{names} と統合され、1 つのセクション "
                f"{_fmt(span[0])}→{_fmt(span[1])} になります")

    # 「セクション追加...」
    def _on_add_section(self):
        if self._section_worker is not None:
            return                          # 準備中の多重起動を防ぐ
        dialog = SectionAddDialog(
            self._vod_duration_sec(), self._default_add_start(),
            self._section_cfg, preview_cb=self._preview_add, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        start, end = dialog.selected_range()

        plan = archive_timeline.plan_section_add(
            self.controller.timeline, start, end,
            merge_on_overlap=self._section_cfg["merge_on_overlap"])
        if plan is None:
            QMessageBox.information(
                self, "セクションの追加",
                "指定した区間は既にセクションに含まれているため、追加するものがありません。")
            return

        # 差分ごとに番号を振る (統合時は後で代表番号へ揃える / §3-4 ④)。
        # 挿入位置の算出を汚さないよう、この時点では必ず固有の番号にする。
        base_index = archive_timeline.next_section_index(self.controller.timeline)
        jobs = []
        for offset, (range_start, range_end) in enumerate(plan["ranges"]):
            jobs.append({
                "index": base_index + offset,
                "start": range_start,
                "end": range_end,
                # 採点はやり直さず、窓スコア列から推定する (§5.4 / 要望 G2)
                "score": archive_timeline.estimate_section_score(
                    self._curve, range_start, range_end),
            })

        self._start_section_worker(jobs, plan)

    # 準備 (切り出し→正規化→音量解析→編集点検出→文字起こし) を別スレッドで回す。
    # 画面はワーカースレッドを止めて動いているため、ここで同期実行すると固まる (§2.5)。
    def _start_section_worker(self, jobs, plan):
        total = sum(job["end"] - job["start"] for job in jobs)
        self._section_progress = QProgressDialog(
            "セクションを準備中…", "キャンセル", 0, 100, self)
        self._section_progress.setWindowTitle("セクションの追加")
        self._section_progress.setWindowModality(Qt.WindowModal)
        self._section_progress.setAutoClose(False)
        self._section_progress.setAutoReset(False)
        self._section_progress.setMinimumDuration(0)
        self._section_progress.setValue(0)
        self._section_progress.canceled.connect(self._on_section_cancel)
        _logger.info("セクション準備開始: %d 区間 / 合計 %.1fs", len(jobs), total)

        self._section_worker = _SectionPrepareWorker(
            self._source_path, self._settings, self._clip_settings, jobs,
            self._clip_workdir, parent=self)
        self._section_worker.progress.connect(self._on_section_progress)
        self._section_worker.finished_ok.connect(
            lambda entries, _plan=plan: self._on_section_ready(entries, _plan))
        self._section_worker.failed.connect(self._on_section_failed)
        self._section_worker.start()

    def _on_section_progress(self, ratio, label):
        if self._section_progress is not None:
            self._section_progress.setValue(int(max(0.0, min(ratio, 1.0)) * 100))
            self._section_progress.setLabelText(label)

    # キャンセルは工程の切れ目でのみ効く (音声認識は途中で止められない / §3-5)
    def _on_section_cancel(self):
        if self._section_worker is not None:
            self._section_worker.cancel()
        if self._section_progress is not None:
            self._section_progress.setLabelText(
                "現在の工程が終わり次第、中止します…")

    def _close_section_progress(self):
        if self._section_progress is not None:
            self._section_progress.close()
            self._section_progress = None
        self._section_worker = None

    # 準備できた素材を Timeline へ入れる。ここで初めて Timeline が変わるため、
    # 途中で失敗・キャンセルしても Timeline は無傷で履歴も汚れない (§5.7 c)。
    def _on_section_ready(self, entries, plan):
        self._close_section_progress()
        if not entries:
            self.preview.set_status("セクションの追加を中止しました")
            return
        # キャンセルで一部しか用意できていない場合は、用意できた区間だけを入れる
        prepared_ranges = {(round(e["start"], 3), round(e["end"], 3)) for e in entries}
        if len(entries) < len(plan["ranges"]):
            plan = dict(plan)
            plan["ranges"] = [r for r in plan["ranges"]
                              if (round(r[0], 3), round(r[1], 3)) in prepared_ranges]

        command = archive_timeline.AddArchiveSection(
            entries, self._clip_settings, plan,
            min_clip_sec=self.controller.cfg["min_clip_sec"])
        if not self.controller.execute(command):
            self.preview.set_status("セクションを追加できませんでした")
            return

        # 書き出しが使う準備済みデータを更新する (統合時は代表へ畳む / §3-6)
        self._prepared = archive_timeline.apply_prepared_after_add(
            self._prepared, plan, entries)
        self._prepared_by_index = {p["index"]: p for p in self._prepared}
        for entry in entries:
            self._prepared_by_index.setdefault(entry["index"], entry)

        self._update_history_buttons()
        self.graph.set_data(self._curve, self._clips_meta())
        target = plan.get("target_index")
        if target is None:
            target = entries[0]["index"]
        self.jump_to_clip(target)
        span = plan["span"]
        self.preview.set_status(
            f"セクションを追加しました（clip{target} / "
            f"{_fmt(span[0])}→{_fmt(span[1])}）")

    def _on_section_failed(self, message):
        self._close_section_progress()
        _logger.warning("セクションの準備に失敗しました: %s", message)
        QMessageBox.warning(
            self, "セクションの追加",
            f"セクションを準備できませんでした。\n\n{message}")

    # ------------------------------------------------------------------
    # ドック配置の保存・復元 (§7)
    # ------------------------------------------------------------------

    def _restore_dock_state(self, ui_cfg):
        state = str(ui_cfg.get("archive_dock_state") or "")
        if not state or self._dock_host is None:
            return
        try:
            raw = QByteArray(base64.b64decode(state.encode("ascii")))
        except (ValueError, UnicodeEncodeError):
            _logger.warning("保存されたドック配置を読めないため既定配置にします")
            return
        if not self._dock_host.restoreState(raw):
            _logger.info("ドック配置を復元できなかったため既定配置にします")

    def _save_dock_state(self):
        if self._dock_host is None:
            return
        try:
            state = base64.b64encode(
                bytes(self._dock_host.saveState())).decode("ascii")
            ui_cfg = self._settings.setdefault("timeline", {}).setdefault("ui", {})
            if ui_cfg.get("archive_dock_state") == state:
                return
            ui_cfg["archive_dock_state"] = state
            save_settings(self._settings)
        except Exception:  # noqa: BLE001 (配置の記憶に失敗しても画面は閉じる)
            _logger.exception("ドック配置の保存に失敗しました (処理は続行します)")

    # ------------------------------------------------------------------
    # 結果・終了
    # ------------------------------------------------------------------

    # 完了時の編集結果 (焼き込みへ渡す)
    # 戻り値: [{"index","theme","use"}]。編集済み Timeline は result_timeline() で取る。
    def result_data(self):
        results = []
        for row in self._sections():
            index = row["index"]
            results.append({
                "index": index,
                "theme": archive_timeline.clip_theme(self.controller.timeline, index),
                "use": archive_timeline.is_clip_enabled(self.controller.timeline, index),
            })
        return results

    # 書き出しが使う準備済みデータ (ver3 resolve13 §3-6)
    # セクションを追加・統合していれば差し替わっている。
    def result_prepared(self):
        return list(self._prepared)

    # Resolve 出力はアーカイブの現行仕様 (使用クリップ全件・VOD 基準) を維持する (§5.7)
    def _on_export_resolve(self):
        prepared_by_index = dict(self._prepared_by_index)
        entries = []
        for clip_index, sub_timeline in archive_timeline.split_by_clip(
                self.controller.timeline):
            prepared = prepared_by_index.get(clip_index)
            if prepared is None:
                continue
            entries.append(archive_timeline.to_export_entry(
                clip_index, sub_timeline, prepared,
                archive_timeline.clip_theme(self.controller.timeline, clip_index)))
        if not entries:
            self.preview.set_status("出力できるクリップがありません")
            return
        run_resolve_export(
            self,
            lambda confirm: resolve_export.export_archive_result(
                self._source_path, entries, self._settings, overwrite_confirm=confirm),
        )

    # 「完了」時は映像クリップの有無だけ見る (基底は Timeline 用の文言で警告する)
    def accept(self):
        if not self.controller.timeline.base_clips():
            self.preview.set_status("使用するクリップがありません")
            return
        # 決定後は書き出し側が本体を上書き保存するため自動保存は要らなくなる
        # (基底 accept と同じ後始末 / ver3 resolve9 §5.4)
        if self._save_enabled:
            self._discard_autosave()
        super(TimelineEditorDialog, self).accept()

    def done(self, code):
        self._save_dock_state()
        super().done(code)



# セクション追加の準備ワーカー (ver3 resolve13 §3-5)
#
# 切り出し→正規化→音量解析→編集点検出→文字起こし を別スレッドで回す。
# 編集画面はワーカースレッドを止めてメインスレッドで動いているため、
# ここを同期実行すると GUI が固まる (resolve13 §2.5)。
#
# 処理そのものは初回構築と同じ clip_writer.prepare_one_clip を通す。
# 書き写すと片方だけ直されて挙動が割れるため、必ず共有する (§5.4)。
#
# キャンセルは区間の切れ目でのみ効く。音声認識 (faster-whisper) は途中で
# 止める手段が無いため、実行中の中断はできない。
class _SectionPrepareWorker(QThread):

    progress = Signal(float, str)      # (0..1, 表示文言)
    finished_ok = Signal(object)       # 準備できた prepared の一覧
    failed = Signal(str)

    def __init__(self, source_path, settings, clip_settings, jobs, workdir, parent=None):
        super().__init__(parent)
        self._source_path = source_path
        self._settings = settings
        self._clip_settings = clip_settings
        self._jobs = list(jobs)
        self._workdir = workdir
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        ffmpeg_cfg = self._settings.get("ffmpeg", {})
        entries = []
        total = max(len(self._jobs), 1)
        try:
            for pos, job in enumerate(self._jobs):
                if self._cancelled:
                    _logger.info("セクションの準備を中止しました (%d/%d 区間で停止)",
                                 pos, total)
                    break

                def phase(label, _pos=pos, _job=job):
                    head = f"区間 {_pos + 1}/{total}" if total > 1 else "セクション"
                    self.progress.emit(
                        _pos / total,
                        f"{head} {format_hms(_job['start'])}→{format_hms(_job['end'])}"
                        f" を準備中…（{label}）")

                entries.append(clip_writer.prepare_one_clip(
                    self._source_path, self._settings, self._clip_settings, job,
                    ffmpeg_cfg, self._workdir, use_timeline=True, progress_cb=phase))
            self.progress.emit(1.0, "準備が完了しました")
            self.finished_ok.emit(entries)
        except Exception as e:  # noqa: BLE001 (GUI へ集約通知)
            _logger.exception("セクションの準備に失敗しました")
            self.failed.emit(str(e))
