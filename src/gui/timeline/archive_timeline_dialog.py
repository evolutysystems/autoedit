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

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QWidget,
)

from ...archive import timeline_builder as archive_timeline
from ...export import resolve_export
from ...timeline import project_io
from ...settings.settings_window import save_settings
from ...utils.logger import get_logger
from ..score_graph_widget import ScoreGraphWidget
from ..subtitle_editor_dialog import run_resolve_export
from .timeline_editor_dialog import TimelineEditorDialog

_logger = get_logger(__name__)

# 設定値 "top"/"left"/"right"/"bottom" → Qt のドック領域
_DOCK_AREAS = {
    "top": Qt.TopDockWidgetArea,
    "bottom": Qt.BottomDockWidgetArea,
    "left": Qt.LeftDockWidgetArea,
    "right": Qt.RightDockWidgetArea,
}

# 秒を "H:MM:SS" 表記へ (クリップバーの区間表示用)
def _fmt(seconds):
    total = int(max(float(seconds or 0.0), 0.0))
    return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


class ArchiveTimelineDialog(TimelineEditorDialog):

    # timeline    : archive.timeline_builder.build_archive_timeline の結果
    # prepared    : clip_writer._prepare_clips の戻り値 (TOP 順)
    # curve       : 採点グラフ用の窓スコア列
    # source_path : 元 VOD (Resolve 出力に使う)
    # project_path/created_at/mode: 保存済みプロジェクトを開き直したときに
    #   保存先と初回作成時刻を引き継ぐ (ver3 resolve9 §5.4)
    def __init__(self, timeline, prepared, curve, source_path, settings, work_dir,
                 parent=None, project_path=None, created_at=None, mode="pipeline"):
        self._prepared = list(prepared or [])
        self._curve = list(curve or [])
        self._source_path = source_path or ""
        self._current = self._prepared[0]["index"] if self._prepared else None
        self._syncing = False
        self._dock_host = None
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
        for entry in self._prepared:
            self.clip_combo.addItem(
                f"clip{entry['index']}  {_fmt(entry.get('start'))}→{_fmt(entry.get('end'))}"
                f"  点{float(entry.get('score', 0.0)):.1f}", entry["index"])
        self.clip_combo.activated.connect(self._on_clip_selected)
        layout.addWidget(self.clip_combo)

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

    # 採点グラフへ渡すマーカ情報
    def _clips_meta(self):
        return [{"index": p["index"], "start": p.get("start", 0.0),
                 "end": p.get("end", 0.0), "score": p.get("score", 0.0)}
                for p in self._prepared]

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
        for entry in self._prepared:
            if entry["index"] == clip_index:
                return entry
        return None

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
        for entry in self._prepared:
            index = entry["index"]
            results.append({
                "index": index,
                "theme": archive_timeline.clip_theme(self.controller.timeline, index),
                "use": archive_timeline.is_clip_enabled(self.controller.timeline, index),
            })
        return results

    # Resolve 出力はアーカイブの現行仕様 (使用クリップ全件・VOD 基準) を維持する (§5.7)
    def _on_export_resolve(self):
        prepared_by_index = {p["index"]: p for p in self._prepared}
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
