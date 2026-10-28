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
import shutil

from PySide6.QtCore import QByteArray, Qt, QThread, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
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

from ...archive import clip_writer, project_resume, twitch_source, vod_range, vod_rebase
from ...exceptions import DiskSpaceError, PipelineCancelled
from ...i18n import tr
from ...modules import ffmpeg_runner
from ...archive import config as archive_config
from ...archive import timeline_builder as archive_timeline
from ...export import resolve_export
from ...services import config as services_config
from ...timeline import project_io
from ...utils.logger import get_logger
from ..points_indicator import WATERMARK_MESSAGE_RESOLVE, watermark_confirm
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

# 準備データと素材の控えを vod_start で突き合わせるときの許容 (秒)。
# どちらも同じ値を書き写したものなので、丸めの差だけを見逃せばよい。
_INTRO_MATCH_EPS = 0.01


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
        # ドック配置を復元したか (画面が出たとき 1 回だけ / 2026-10-23 の報告)
        self._dock_restored = False
        # セクション追加 (ver3 resolve13)
        self._clip_workdir = clip_workdir
        self._clip_settings = clip_settings
        self._section_cfg = archive_config.section_add_config(settings or {})
        self._section_worker = None
        self._section_progress = None
        self._vod_duration = 0.0
        # 区間の取り直しに使う取得元 (ver9 resolve5 §4.3)。
        # 画面構築 (_build_clip_bar) から参照するため super().__init__ より先に決める。
        # この時点では controller が無いので、引数の timeline から引く。
        self._vod_source = archive_timeline.resolve_vod_source(timeline, settings or {})
        # 全長を Twitch へ尋ねたか (取れなくても二度は尋ねない)
        self._vod_length_asked = False
        super().__init__(timeline, settings, work_dir, parent=parent,
                         project_path=project_path, created_at=created_at, mode=mode)
        self.setWindowTitle(tr("切り抜き編集 (採点結果)"))
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

        self.graph_dock = QDockWidget(tr("採点グラフ"))
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
        # 配置の復元は**画面が出てから**行う (showEvent / 2026-10-23 の報告)。
        # ここは host がまだこの画面へ入っていない (親なし = それ自体が
        # トップレベル) ため、記憶が「切り離し」だと浮いた窓がこの時点で作られ、
        # 直後の親付け替えで画面へ出ないまま残る。Qt 側の可視フラグだけが
        # True になるので show() もトグルも効かず、二度と出せなくなる。
        self._bind_graph_toggle()
        return host

    def _decide_button_text(self):
        return tr("完了（切り抜き＋字幕焼き込み）")

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
        return tr("切り抜き編集")

    # ------------------------------------------------------------------
    # 縦動画プロジェクト (ver5 resolve10)
    # ------------------------------------------------------------------
    #
    # アーカイブ用 Timeline が参照している中間ファイルは実行の終わりに消えるため、
    # そのまま保存すると開けない縦プロジェクトになる。作成時に素材を元 VOD へ
    # 張り替えることで、いつでも開き直せるようにする。張り替えには元 VOD が要る
    # ので、VOD が無いときはメニューを出さない。

    def _can_make_vertical(self):
        if not super()._can_make_vertical():
            return False
        vod = project_resume.recorded_vod_path(self.controller.timeline)
        return bool(vod) and os.path.exists(vod)

    def _vertical_rebase(self, clips):
        return vod_rebase.plan(self.controller.timeline, self._settings, clips)

    # keep_media / keep_audio の対象は V1 が参照する全クリップ素材
    def _media_to_copy(self):
        # 素材 1 本ごとの控えを回る (ver5 resolve6 §5.3)。
        # セクションの控え (clips) は統合で 1 件へ畳まれるため、そこから集めると
        # 畳まれた側の素材のサイドカーが保存されず、開き直したときに
        # 正規化のやり直し・復元失敗になっていた (ver5 resolve6 §2.2 (1))。
        timeline = self.controller.timeline
        media_list = []
        seen = set()
        rows = archive_timeline.archive_media(timeline)
        if not rows:
            rows = archive_timeline.archive_section(timeline).get("clips") or []
        for entry in rows:
            media_id = str(entry.get("media_id", "") or "")
            if not media_id or media_id in seen:
                continue
            seen.add(media_id)
            media = timeline.media_by_id(media_id)
            if media is not None:
                media_list.append(media)
        return media_list

    # 保存時にイントロカードの素をプロジェクトの隣へ残す
    # (docs/error/fromUser/resolve.md §9-7)
    #
    # イントロカードはクリップ開始の buffer_sec 秒**手前**の映像から作るため、
    # クリップ素材 (vod_start 始まり) には入っておらず、元 VOD だけが素だった。
    # §6-6 で元 VOD を手放せるようにした結果、開き直して書き出すと
    # イントロカードだけが作れなくなっていた。数秒ぶんをここで控えておく。
    # 失敗しても保存そのものは続ける (元 VOD があるうちは元 VOD から作れる)。
    def _keep_extra_beside_project(self, project_path):
        if not bool(self._project_cfg["archive_keep_intro_source"]):
            return
        card = archive_config.intro_card_config(self._settings)
        if not card["enabled"] or card["buffer_sec"] <= 0:
            return              # イントロカードを使わない設定
        vod = project_resume.recorded_vod_path(self.controller.timeline)
        # 元 VOD が無くても、区間を取り直して足したセクションには準備中の控えがある
        # (ver9 resolve5 §4.5)。在るものだけを素材フォルダへ移す。
        has_vod = bool(vod and os.path.exists(vod))
        folder = self._media_dir(project_path)
        ffmpeg_cfg = self._settings.get("ffmpeg", {})
        timeline = self.controller.timeline
        saved = 0
        try:
            for media in self._media_to_copy():
                if media is None:
                    continue
                entry = archive_timeline.archive_media_entry(timeline, media.id)
                if entry is None:
                    continue    # 復元情報が無い素材 (D&D で足した画像など)
                if clip_writer.existing_intro_source(folder, media.id):
                    saved += 1
                    continue    # 前回の保存で作ってある (作り直さない)
                dest = clip_writer.intro_source_path(folder, media.id)
                QApplication.setOverrideCursor(Qt.WaitCursor)
                try:
                    self.preview.set_status(tr("イントロ用の映像を保存しています…"))
                    if has_vod:
                        if clip_writer.export_intro_source(
                                vod, float(entry.get("vod_start", 0.0)),
                                card["buffer_sec"], dest, ffmpeg_cfg):
                            saved += 1
                    elif self._copy_prepared_intro_source(entry, dest):
                        saved += 1
                finally:
                    QApplication.restoreOverrideCursor()
        except OSError:
            _logger.exception("イントロカードの素の保存に失敗しました (保存は続行します)")
            return
        if saved:
            _logger.info("イントロカードの素を %d 件そろえました: %s", saved, folder)
        elif not has_vod:
            # 元 VOD も控えも無い素材 (元 VOD があった時代に保存したセクション)。
            # 既に在る控えはそのまま残るため、書き出しは従来どおり続けられる。
            _logger.info("元 VOD が無いためイントロカードの素は作れません "
                         "(既存の控えはそのまま使います)")

    # 準備中に控えたイントロの素を素材フォルダへ写す (ver9 resolve5 §4.5)
    #
    # 区間を取り直して足したセクションは、保存の時点では取り直した素材が消えている。
    # 準備の最中に控えてあるので、それをプロジェクトの隣へ移しておく。
    # 突き合わせは vod_start で行う (素材 1 本と 1 対 1 で対応する値のため)。
    def _copy_prepared_intro_source(self, media_entry, dest):
        target = float(media_entry.get("vod_start", 0.0))
        # 統合で足した差分の素材は self._prepared へは入らず (代表へ畳まれる)
        # _prepared_by_index にだけ残るため、そちらを走査する。
        for prepared in self._prepared_by_index.values():
            if abs(float(prepared.get("start", 0.0)) - target) > _INTRO_MATCH_EPS:
                continue
            kept = str(prepared.get("intro_source", "") or "")
            if not kept or not os.path.exists(kept):
                continue
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            shutil.copy2(kept, dest)
            _logger.info("イントロカードの素を控えから戻しました: %s", dest)
            return True
        return False

    # クリップ選択・使用可否・テーマの行 (§3-4)
    def _build_clip_bar(self):
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(6, 4, 6, 4)

        layout.addWidget(QLabel(tr("クリップ")))
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
            self.add_section_button = QPushButton(tr("セクション追加..."))
            self.add_section_button.setAutoDefault(False)
            self.add_section_button.setToolTip(self._section_add_tooltip())
            self.add_section_button.clicked.connect(self._on_add_section)
            layout.addWidget(self.add_section_button)
        elif self._section_add_blocked_by_missing_source():
            # 元 VOD を手放し、取り直しもできない場合 (ver9 resolve5 §4.6)。
            # 黙ってボタンを消すと「前は在ったのに」と理由が分からないため、
            # 押せない状態で出し、なぜ使えないかと戻し方を伝える。
            self.add_section_button = QPushButton(tr("セクション追加..."))
            self.add_section_button.setAutoDefault(False)
            self.add_section_button.setEnabled(False)
            self.add_section_button.setToolTip(self._section_add_blocked_tooltip())
            layout.addWidget(self.add_section_button)

        self.use_check = QCheckBox(tr("このクリップを使用する"))
        self.use_check.setToolTip(
            tr("外すと Timeline 上で灰色になり、書き出しから外れます (戻せます)"))
        self.use_check.setChecked(True)
        self.use_check.toggled.connect(self._on_use_toggled)
        layout.addWidget(self.use_check)

        layout.addWidget(QLabel(tr("テーマ")))
        self.theme_edit = QLineEdit()
        self.theme_edit.setPlaceholderText(tr("空欄=テーマ演出なし"))
        self.theme_edit.setToolTip(
            tr("入力するとクリップ先頭にイントロカード、本編に左上タグが付きます"))
        self.theme_edit.editingFinished.connect(self._on_theme_changed)
        layout.addWidget(self.theme_edit, 1)

        # 採点グラフ (ドック) の表示切替 (ver8 resolve2 §3-2)。
        # ドックは × で閉じられるため、1 クリックで出し直せる口を
        # 常に見えるクリップバーへ置く。押下状態は実際の表示と同期する。
        self.graph_toggle = QPushButton(tr("採点グラフ"))
        self.graph_toggle.setCheckable(True)
        self.graph_toggle.setChecked(True)
        self.graph_toggle.setAutoDefault(False)
        layout.addWidget(self.graph_toggle)
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
                    tr("clip{index}  {start}→{end}  点{score:.1f}",
                       index=row["index"], start=_fmt(row["start"]),
                       end=_fmt(row["end"]), score=row["score"]),
                    row["index"])
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

    # 元 VOD が手元にあるか
    def _has_local_vod(self):
        return bool(self._source_path and os.path.exists(self._source_path))

    # 区間だけの取り直しが使えるか (ver9 resolve5 §4.6)
    #
    # 所有判定 (archive.auth.owner_only) はここでは見ない。取り直しは
    # 「採点のときに所有判定を通した同じ VOD の一部をもう一度取る」操作であり、
    # 入口で一度確かめてあるためである。sub-only などで取れない場合は
    # 取得を試みた時点で理由が出る (resolve5 §5.2)。
    def _refetch_available(self):
        return bool(self._section_cfg["refetch"]["enabled"]
                    and self._vod_source.get("video_id"))

    # 追加機能を出せるか。新素材の置き場 (write_clips の作業ディレクトリ) と
    # OP/ED 無効化済みの設定が揃っている経路でのみ有効 (§5.6)。
    # 元 VOD が手元に無くても、区間だけ取り直せるなら使える (ver9 resolve5)。
    def _section_add_available(self):
        if not (self._section_cfg["enabled"]
                and self._clip_workdir and self._clip_settings):
            return False
        return bool(self._has_local_vod() or self._refetch_available())

    # 追加機能が「素材を用意できないことだけ」を理由に使えない状態か。
    # 機能そのものが無効・経路が違う場合と区別し、説明を出す相手を絞る。
    def _section_add_blocked_by_missing_source(self):
        return bool(self._section_cfg["enabled"]
                    and self._clip_workdir
                    and self._clip_settings
                    and not self._has_local_vod()
                    and not self._refetch_available())

    # 「セクション追加...」のツールチップ (素材の出どころで文言を変える)
    def _section_add_tooltip(self):
        base = tr("元動画の区間を指定してセクションを追加します。\n"
                  "元動画の時系列に合わせた位置へ挿入され、"
                  "既存セクションと重なる場合は 1 つへ統合されます。")
        if self._has_local_vod():
            return base
        return base + tr("\n\n元動画は手元にありませんが、"
                         "追加する区間だけを Twitch から取得します"
                         "（元動画全体を取り直すわけではありません）。")

    # 追加できないときのツールチップ (なぜ使えないかと、戻し方を伝える)
    def _section_add_blocked_tooltip(self):
        if not self._section_cfg["refetch"]["enabled"]:
            return tr("元動画が無いため、新しい区間を追加できません。\n"
                      "この編集に使う素材はプロジェクトに保存されているため編集は続けられます。\n"
                      "区間を足したい場合は、設定の「元動画を残す」を有効にしたうえで"
                      "取得し直してください。")
        # 取り直しは有効だが取得元が分からない (ローカル動画からの採点など)
        return tr("元動画が無く、取得元も分からないため新しい区間を追加できません。\n"
                  "この編集に使う素材はプロジェクトに保存されているため編集は続けられます。\n"
                  "区間を足したい場合は、元動画を用意して取得し直してください。")

    # 同梱 ffmpeg のディレクトリ (twitch-dl は VOD の扱いに ffmpeg を PATH から探す)
    def _ffmpeg_dir(self):
        try:
            return os.path.dirname(
                ffmpeg_runner.get_ffmpeg_exe(self._settings.get("ffmpeg", {})))
        except Exception:  # noqa: BLE001 (見つからなくても取得は試せる)
            return None

    # 元動画の全長 (秒)。取れなければ 0.0 = 上限チェックをしない。
    #
    # ① 元 VOD があれば測る → ② 採点時の記録 → ③ Twitch へ尋ねる
    # (ver9 resolve5 §4.6)。ffprobe も API も 1 度だけ呼ぶ。
    def _vod_duration_sec(self):
        if self._vod_duration:
            return self._vod_duration
        if self._has_local_vod():
            try:
                self._vod_duration = float(ffmpeg_runner.probe_duration(
                    self._source_path, self._settings.get("ffmpeg", {})) or 0.0)
            except Exception:  # noqa: BLE001 (測れなくても追加はできる)
                _logger.exception("元動画の全長を取得できませんでした")
                self._vod_duration = 0.0
        if not self._vod_duration:
            # 採点時に記録した全長。元 VOD を手放したあとはこれが頼りになる
            self._vod_duration = float(
                self._vod_source.get("vod_duration_sec", 0.0) or 0.0)
        if (not self._vod_duration and not self._vod_length_asked
                and self._refetch_available()):
            self._vod_length_asked = True
            self._vod_duration = self._ask_vod_length()
        return self._vod_duration

    # Twitch へ全長を尋ねる (記録も元 VOD も無いプロジェクト向け / §4.6 ③)。
    # 数秒かかるため待ちカーソルを出す。取れなければ 0.0 (上限チェックなし)。
    def _ask_vod_length(self):
        dl = archive_config.download_config(self._settings)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            return float(twitch_source.video_length_sec(
                self._vod_source["video_id"], dl["twitch_dl_path"],
                self._ffmpeg_dir()) or 0.0)
        finally:
            QApplication.restoreOverrideCursor()

    # 追加ダイアログの初期値。
    # 採点グラフのクリックも再生ヘッドの移動も選択セクション (_current) へ反映されるため、
    # 「今見ているセクションの元動画での開始時刻」を初期値にすれば、
    # グラフを見ながら数値を決める導線になる (resolve13 §3-1)。
    def _default_add_start(self):
        row = self._entry(self._current)
        return row["start"] if row else 0.0

    # 追加の計画を立てる (設定を必ず通すため 1 か所にまとめる / ver5 resolve6 §5.6)
    def _plan_add(self, start, end):
        return archive_timeline.plan_section_add(
            self.controller.timeline, start, end,
            merge_on_overlap=self._section_cfg["merge_on_overlap"],
            occupied_by=self._section_cfg["occupied_by"],
            gap_merge_sec=self._section_cfg["used_gap_merge_sec"],
            max_ranges=self._section_cfg["max_ranges"],
            min_range_sec=self._section_cfg["min_length_sec"])

    # 追加ダイアログに出す案内 (ver5 resolve6 §5.8)
    # 破壊的に見える統合の予告に加え、「なぜ追加できないのか」「何を用意するのか」を出す。
    def _preview_add(self, start, end):
        plan = self._plan_add(start, end)
        if plan is None:
            return (tr("この区間はすべて Timeline で使われています。"
                       "使われていない時間を指定してください。"), False)
        if plan.get("too_many"):
            return (tr("用意する区間が {count} 個に分かれます（上限 {limit} 個）。"
                       "もっと狭い範囲を指定してください。",
                       count=plan["too_many"],
                       limit=self._section_cfg["max_ranges"]), False)

        lines = []
        covered = float(plan.get("covered_sec") or 0.0)
        ranges = plan.get("ranges") or []
        if covered > 0.5 or len(ranges) > 1:
            total = sum(e - s for s, e in ranges)
            head = (tr("うち {covered} は既に使われているため、", covered=_fmt(covered))
                    if covered > 0.5 else "")
            lines.append(tr("{head}{count} 個の区間（合計 {total}）を用意します",
                            head=head, count=len(ranges), total=_fmt(total)))
        if plan["merge_indexes"]:
            names = "・".join(f"clip{i}" for i in plan["merge_indexes"])
            span = plan["span"]
            lines.append(tr("{names} と統合され、1 つのセクション {start}→{end} になります",
                            names=names, start=_fmt(span[0]), end=_fmt(span[1])))
        return "\n".join(lines), True

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

        plan = self._plan_add(start, end)
        if plan is None:
            QMessageBox.information(
                self, tr("セクションの追加"),
                tr("指定した区間は、すべて Timeline で使われています。\n"
                "まだ使っていない時間を指定してください。"))
            return
        if plan.get("too_many"):
            QMessageBox.information(
                self, tr("セクションの追加"),
                tr("用意する区間が {count} 個に分かれます（上限 {limit} 個）。\n"
                   "指定する範囲を狭くしてください。",
                   count=plan["too_many"],
                   limit=self._section_cfg["max_ranges"]))
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
            tr("セクションを準備中…"), tr("キャンセル"), 0, 100, self)
        self._section_progress.setWindowTitle(tr("セクションの追加"))
        self._section_progress.setWindowModality(Qt.WindowModal)
        self._section_progress.setAutoClose(False)
        self._section_progress.setAutoReset(False)
        self._section_progress.setMinimumDuration(0)
        self._section_progress.setValue(0)
        self._section_progress.canceled.connect(self._on_section_cancel)
        _logger.info("セクション準備開始: %d 区間 / 合計 %.1fs", len(jobs), total)

        self._section_worker = _SectionPrepareWorker(
            self._source_path, self._settings, self._clip_settings, jobs,
            self._clip_workdir, parent=self,
            # 元 VOD が手元に無いときだけ取り直す (ver9 resolve5 §4.7)
            vod_source=(None if self._has_local_vod() else self._vod_source),
            # 採点時に VOD 全体で測った音量。追加セクションだけ基準が変わるのを防ぐ (§4.4)
            vod_analysis=archive_timeline.vod_analysis_record(self.controller.timeline),
            ffmpeg_dir=self._ffmpeg_dir(),
            vod_duration_sec=self._vod_duration)
        self._section_worker.progress.connect(self._on_section_progress)
        self._section_worker.finished_ok.connect(
            lambda entries, _plan=plan: self._on_section_ready(entries, _plan))
        self._section_worker.failed.connect(self._on_section_failed)
        self._section_worker.start()

    # 進捗の反映 (docs/error/fromUser/resolve.md §9-3)
    #
    # **setValue() を最後に呼ぶ**こと。モーダルの QProgressDialog の setValue() は
    # 内部で processEvents() を回すため、その最中に完了通知が届いて
    # _on_section_ready → _close_section_progress が走り、
    # self._section_progress が None になる。先に setValue() を呼ぶと、
    # 戻ってきた直後の行で None を触って落ちる (2026-10-22 の報告)。
    # 値の更新を最後に置けば、再入から戻った先に触るものが無くなる。
    def _on_section_progress(self, ratio, label):
        dialog = self._section_progress
        if dialog is None:
            return
        dialog.setLabelText(label)
        dialog.setValue(int(max(0.0, min(ratio, 1.0)) * 100))

    # キャンセルは工程の切れ目でのみ効く (音声認識は途中で止められない / §3-5)
    def _on_section_cancel(self):
        if self._section_worker is not None:
            self._section_worker.cancel()
        if self._section_progress is not None:
            self._section_progress.setLabelText(
                tr("現在の工程が終わり次第、中止します…"))

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
            self.preview.set_status(tr("セクションの追加を中止しました"))
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
            self.preview.set_status(tr("セクションを追加できませんでした"))
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
            tr("セクションを追加しました（clip{index} / {start}→{end}）",
               index=target, start=_fmt(span[0]), end=_fmt(span[1])))

    def _on_section_failed(self, message):
        self._close_section_progress()
        _logger.warning("セクションの準備に失敗しました: %s", message)
        QMessageBox.warning(
            self, tr("セクションの追加"),
            tr("セクションを準備できませんでした。\n\n{message}", message=message))

    # ------------------------------------------------------------------
    # ドック配置の保存・復元 (§7)
    # ------------------------------------------------------------------

    # 画面が出てからドック配置を戻す (2026-10-23 の報告)。
    #
    # この時点なら host はこの画面の中に入っているため、記憶が「切り離し」でも
    # 浮いた窓が正しく画面へ出る。復元は 1 回だけで、2 度目以降の表示
    # (最小化からの復帰など) では利用者が動かした配置をそのまま残す。
    def showEvent(self, event):
        super().showEvent(event)
        self._restore_dock_once()

    # 画面が出た最初の 1 回だけ配置を戻す
    def _restore_dock_once(self):
        if self._dock_restored:
            return
        self._dock_restored = True
        ui_cfg = self.controller.cfg["ui"]
        self._restore_dock_state(ui_cfg)
        # 記憶した配置に「閉じた」状態が入っていても採点グラフは出し直す
        # (ver8 resolve2 §3-1)。閉じたまま覚えると、出し直す口が
        # 画面のどこにも無くなってしまうため。
        if bool(ui_cfg["archive_graph_dock_always_show"]):
            self._ensure_graph_visible()

    # 採点グラフを「実際に見える」状態にする (2026-10-23 の報告)。
    #
    # 閉じていれば開き、切り離したまま画面へ出ていない / 画面外にある場合は
    # ドック領域へ戻す。可視フラグだけ True で画面に無い状態は利用者からは
    # 「押しても出ない」としか見えず、復帰する手段が無くなるため。
    def _ensure_graph_visible(self):
        self.graph_dock.show()
        if not self.graph_dock.isFloating():
            return
        if (bool(self.controller.cfg["ui"]["archive_graph_dock_redock_when_hidden"])
                and not self._dock_on_screen()):
            _logger.info("採点グラフが画面に出ていないためドック位置へ戻します")
            self.graph_dock.setFloating(False)
            self.graph_dock.show()
            return
        self.graph_dock.raise_()

    # 切り離した採点グラフが実際に画面へ出ているか (_pull_into_screen と同じ考え方)。
    # 窓そのものが作られていない / どのモニターにも見える幅が無いときは False。
    def _dock_on_screen(self):
        handle = self.graph_dock.windowHandle()
        if handle is None or not handle.isVisible():
            return False
        margin = max(int(self.controller.cfg["ui"]["window_min_visible_px"]), 1)
        frame = self.graph_dock.frameGeometry()
        for screen in QGuiApplication.screens():
            shown = screen.availableGeometry().intersected(frame)
            if shown.width() >= margin and shown.height() >= margin:
                return True
        return False

    # クリップバーのトグルとドックの表示状態をつなぐ (ver8 resolve2 §3-2)
    #
    # ドック側の toggleViewAction を介すため、× で閉じた・ショートカットで
    # 消した・配置を復元した、のどの経路でもボタンの押下状態が実表示と一致する。
    # setChecked / setVisible は値が変わるときだけ通知するので循環しない。
    def _bind_graph_toggle(self):
        self.graph_toggle.setChecked(self.graph_dock.isVisible())
        self.graph_dock.toggleViewAction().toggled.connect(
            self.graph_toggle.setChecked)
        self.graph_toggle.toggled.connect(self.graph_dock.setVisible)
        # 「実際に見える場所へ出す」は**利用者が押したときだけ**通す。
        # toggled は配置替え (切り離し) の途中にも流れるため、そこへ繋ぐと
        # 意図して切り離した直後にドックへ戻してしまう。clicked は
        # 押下操作だけで、setChecked() の追従では流れない (2026-10-23 の報告)。
        self.graph_toggle.clicked.connect(self._on_graph_clicked)
        key = str(self.controller.cfg["shortcuts"].get("toggle_score_graph", ""))
        tip = tr("採点グラフの表示を切り替えます")
        self.graph_toggle.setToolTip("%s (%s)" % (tip, key) if key else tip)

    # トグルを押して ON にしたとき。浮いたまま画面に無い状態でも
    # 1 クリックで戻せるようにする (2026-10-23 の報告)。
    def _on_graph_clicked(self, checked):
        if checked:
            self._ensure_graph_visible()

    # 採点グラフの表示切替キー (ver8 resolve2 §3-3)。
    # 基底のフック経由で登録するため、文字入力中は無効化される。
    def _register_extra_shortcuts(self, bind):
        bind("toggle_score_graph", self._toggle_graph_dock)

    # ドックの表示 / 非表示を 1 打鍵で入れ替える (ボタン側は結線で追従する)
    def _toggle_graph_dock(self):
        if self.graph_dock.isVisible():
            self.graph_dock.setVisible(False)
        else:
            self._ensure_graph_visible()

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

    # 閉じるときに覚える派生画面ぶんの状態 = 採点グラフのドック配置。
    # 画面の大きさ・位置と同じ 1 回の保存に相乗りする (ver8 resolve3 §3-3)。
    def _collect_window_state(self, store):
        if self._dock_host is None:
            return False
        try:
            state = base64.b64encode(
                bytes(self._dock_host.saveState())).decode("ascii")
        except Exception:  # noqa: BLE001 (配置の記憶に失敗しても画面は閉じる)
            _logger.exception("ドック配置の保存に失敗しました (処理は続行します)")
            return False
        # 画面へ出ていない切り離し位置は覚えない (2026-10-23 の報告)。
        # そのまま覚えると「出ない状態」を起動のたびに再現してしまう。
        # 空で覚え直し、次回は既定の配置 (上部) で開く。
        if (self.graph_dock.isFloating() and self.graph_dock.isVisible()
                and not self._dock_on_screen()):
            _logger.info("画面に出ていない採点グラフの位置は覚えません "
                         "(次回は既定の配置で開きます)")
            state = ""
        if store.get("archive_dock_state") == state:
            return False
        store["archive_dock_state"] = state
        return True

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
            self.preview.set_status(tr("出力できるクリップがありません"))
            return
        run_resolve_export(
            self,
            lambda confirm: resolve_export.export_archive_result(
                self._source_path, entries, self._settings, overwrite_confirm=confirm,
                points=services_config.current_points(),
                watermark_confirm_callback=watermark_confirm(
                    self, WATERMARK_MESSAGE_RESOLVE)),
        )

    # 「完了」時は映像クリップの有無だけ見る (基底は Timeline 用の文言で警告する)
    def accept(self):
        if not self.controller.timeline.base_clips():
            self.preview.set_status(tr("使用するクリップがありません"))
            return
        # 決定後は書き出し側が本体を上書き保存するため自動保存は要らなくなる
        # (基底 accept と同じ後始末 / ver3 resolve9 §5.4)
        if self._save_enabled:
            self._discard_autosave()
        super(TimelineEditorDialog, self).accept()



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

    # vod_source       : 取得元の控え。渡すと**元 VOD の代わりに区間を取り直す**
    #                    (ver9 resolve5 §4.7)。None なら従来どおり source_path を使う。
    # vod_analysis     : VOD 全体で測った音量 (§4.4)。既存セクションと基準を揃える。
    # vod_duration_sec : 元動画の全長 (分かっていれば取得範囲の終端を丸めるのに使う)
    def __init__(self, source_path, settings, clip_settings, jobs, workdir, parent=None,
                 vod_source=None, vod_analysis=None, ffmpeg_dir=None,
                 vod_duration_sec=0.0):
        super().__init__(parent)
        self._source_path = source_path
        self._settings = settings
        self._clip_settings = clip_settings
        self._jobs = list(jobs)
        self._workdir = workdir
        self._cancelled = False
        self._vod_source = vod_source or None
        self._vod_analysis = vod_analysis
        self._ffmpeg_dir = ffmpeg_dir
        self._vod_duration_sec = float(vod_duration_sec or 0.0)

    def cancel(self):
        self._cancelled = True

    # 区間の素材と、その素材の VOD 絶対原点を用意する (ver9 resolve5 §4.7)
    #
    # 元 VOD が手元にあればそのまま使う (原点 0.0 = 従来と完全に同じ)。
    # 無ければ要る区間だけを取り直し、素材の原点を添えて返す。原点は
    # プレイリストの EXTINF 累積から決まるため、切り出し位置はずれない。
    def _source_for(self, job, progress_cb):
        if not self._vod_source:
            return self._source_path, 0.0
        progress_cb(tr("元動画の区間を取得"))
        fetched = vod_range.fetch_range(
            self._vod_source["video_id"], job["start"], job["end"],
            self._workdir, self._settings,
            quality=self._vod_source.get("quality", "source"),
            ffmpeg_dir=self._ffmpeg_dir,
            progress_cb=lambda message: progress_cb(str(message)),
            cancel_cb=lambda: self._cancelled,
            tag=str(job["index"]),
            vod_duration_sec=self._vod_duration_sec)
        return fetched["path"], float(fetched["origin_sec"])

    # イントロカードの素を、区間の素材があるうちに控えておく (ver9 resolve5 §4.5)
    #
    # イントロカードはクリップ開始の buffer_sec 秒**手前**から作るため、
    # クリップ素材 (vod_start 始まり) には入っていない。取り直した区間の素材は
    # 画面を閉じると消えるので、ここで控えないと後から作れなくなる。
    # 元 VOD が手元にある場合は保存時に作れるため何もしない (従来どおり)。
    def _keep_intro_source(self, entry, source, origin, ffmpeg_cfg):
        if not self._vod_source or not entry:
            return
        card = archive_config.intro_card_config(self._settings)
        if not card["enabled"] or card["buffer_sec"] <= 0:
            return              # イントロカードを使わない設定
        dest = os.path.join(self._workdir, f"intro_{entry['index']}.mp4")
        try:
            kept = clip_writer.export_intro_source(
                source, float(entry.get("start", 0.0)), card["buffer_sec"],
                dest, ffmpeg_cfg, source_origin_sec=origin)
        except DiskSpaceError:
            raise               # 容量不足は継続しない (resolve.md §6-9)
        except Exception:  # noqa: BLE001 (控えが作れなくても追加そのものは続ける)
            _logger.warning("イントロカードの素を控えられませんでした (追加は続けます)")
            return
        if kept:
            entry["intro_source"] = kept

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
                    head = (tr("区間 {pos}/{total}", pos=_pos + 1, total=total)
                            if total > 1 else tr("セクション"))
                    self.progress.emit(
                        _pos / total,
                        tr("{head} {start}→{end} を準備中…（{phase}）",
                           head=head, start=format_hms(_job["start"]),
                           end=format_hms(_job["end"]), phase=label))

                source, origin = self._source_for(job, phase)
                entry = clip_writer.prepare_one_clip(
                    source, self._settings, self._clip_settings, job,
                    ffmpeg_cfg, self._workdir, use_timeline=True, progress_cb=phase,
                    vod_analysis=self._vod_analysis, source_origin_sec=origin)
                self._keep_intro_source(entry, source, origin, ffmpeg_cfg)
                entries.append(entry)
            self.progress.emit(1.0, tr("準備が完了しました"))
            self.finished_ok.emit(entries)
        except PipelineCancelled:
            # 取得中の中止は失敗ではない。用意できた区間だけを渡して終える
            # (呼び側が 0 件なら「中止しました」と出す / ver9 resolve5 §4.7)
            _logger.info("セクションの準備を中止しました (%d 区間で停止)", len(entries))
            self.finished_ok.emit(entries)
        except Exception as e:  # noqa: BLE001 (GUI へ集約通知)
            _logger.exception("セクションの準備に失敗しました")
            self.failed.emit(str(e))
