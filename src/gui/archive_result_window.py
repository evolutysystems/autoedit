# アーカイブ結果画面 (flow17 R2 / resolve17 §4.7)
# 全クリップを先に文字起こしした後、1つの画面で一括表示・編集する。
#   上   = 採点グラフ (ScoreGraphWidget)
#   下左 = クリップ選択リスト + 字幕編集 (R1 の SubtitleEditorWidget を再利用)
#   下右 = プレビュー (SubtitlePreviewWidget / resolve23。字幕適用済み区間の再生。
#          対象は prepared_path=無音カット後クリップで、字幕 items と時間軸が一致する)
# 「完了」で各クリップの編集済み字幕・テーマ・使用可否を返し、まとめて焼き込みへ進む。
import threading

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..export import resolve_export
from ..modules.subtitle_generator import _format_ass_time
from ..utils.logger import get_logger
from .score_graph_widget import ScoreGraphWidget
from .subtitle_editor_dialog import (
    RESOLVE_EXPORT_BUTTON_TEXT,
    SubtitleEditorWidget,
    run_resolve_export,
)
from .subtitle_preview_widget import SubtitlePreviewWidget

_logger = get_logger(__name__)


# 「H:MM:SS.cc → H:MM:SS.cc」形式の区間表示
def _fmt_range(start, end):
    return f"{_format_ass_time(start)} → {_format_ass_time(end)}"


# 結果画面本体
# prepared: [{"index","start","end","score","items","profile","eff_cfg"}]
#   items = 文字起こし済みタイムライン [{"start","end","text","use","role"}]
# curve: 窓スコア列 (グラフ用) / source_path: 元 VOD (プレビュー抽出用)
# settings: ffmpeg 設定等 / default_font/default_size/font_families: 字幕編集の既定
# theme_placeholder: テーマ欄プレースホルダ (resolve19 固定文言)
class ArchiveResultWindow(QDialog):

    def __init__(self, prepared, curve, source_path, settings,
                 default_font="", default_size=None, font_families=None,
                 theme_placeholder="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("採点結果 (切り抜き＋字幕焼き込み)")
        self.resize(1000, 720)

        self._prepared = list(prepared or [])
        self._curve = curve or []
        self._source_path = source_path
        self._settings = settings or {}
        self._default_font = default_font
        self._default_size = default_size
        self._font_families = font_families
        self._theme_placeholder = theme_placeholder

        # クリップごとの編集状態 {items, theme}。use はリストのチェックで持つ。
        self._states = [
            {"items": list(p.get("items", [])), "theme": ""}
            for p in self._prepared
        ]
        self._current = -1

        self._build_ui()
        if self._prepared:
            self.clip_list.setCurrentRow(0)

    def _build_ui(self):
        root = QVBoxLayout(self)

        # 上: 採点グラフ
        self.graph = ScoreGraphWidget()
        clips_meta = [
            {"index": p.get("index"), "start": p.get("start"),
             "end": p.get("end"), "score": p.get("score", 0.0)}
            for p in self._prepared
        ]
        self.graph.set_data(self._curve, clips_meta)
        self.graph.clip_selected.connect(self._on_graph_selected)

        # 下: 左右分割 (左=クリップリスト+字幕 / 右=プレビュー)
        bottom = QSplitter(Qt.Horizontal)

        # 左: クリップ選択リスト + 字幕編集
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("クリップ (チェック=使用) を選び、字幕とテーマを編集します。"))
        self.clip_list = QListWidget()
        self.clip_list.setMaximumHeight(120)
        for p in self._prepared:
            item = QListWidgetItem(
                f"clip{p.get('index')}  {_fmt_range(p.get('start', 0), p.get('end', 0))}  "
                f"点{p.get('score', 0):.1f}"
            )
            item.setFlags((item.flags() | Qt.ItemIsUserCheckable))
            item.setCheckState(Qt.Checked)  # 既定は全使用
            self.clip_list.addItem(item)
        self.clip_list.currentRowChanged.connect(self._on_row_changed)
        left_layout.addWidget(self.clip_list)

        # R1 の字幕編集ウィジェットを埋め込む (テーマ欄あり=アーカイブ経路)
        self.editor = SubtitleEditorWidget(
            [], parent=left,
            default_font=self._default_font, default_size=self._default_size,
            font_families=self._font_families, show_theme_field=True,
            theme_placeholder=self._theme_placeholder,
        )
        left_layout.addWidget(self.editor)

        # 右: プレビュー (字幕適用済み区間の再生 / resolve23)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.preview = SubtitlePreviewWidget(self._settings, parent=right)
        # 字幕編集テーブルと連動 (items 取得・行選択シーク)
        self.preview.bind_editor(self.editor)
        right_layout.addWidget(self.preview)
        right_layout.addStretch(1)

        bottom.addWidget(left)
        bottom.addWidget(right)
        bottom.setStretchFactor(0, 3)
        bottom.setStretchFactor(1, 2)

        # 上下スプリッタ
        outer = QSplitter(Qt.Vertical)
        outer.addWidget(self.graph)
        outer.addWidget(bottom)
        outer.setStretchFactor(0, 2)
        outer.setStretchFactor(1, 3)
        root.addWidget(outer)

        # DaVinci Resolve ファイル出力 / 完了 / キャンセル
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        # 出力ボタン (元 VOD が特定でき、設定で有効なときだけ追加する / resolve20 §5.4)
        self.export_button = None
        if self._can_export():
            self.export_button = QPushButton(RESOLVE_EXPORT_BUTTON_TEXT)
            self.export_button.setToolTip(
                "使用チェックしたクリップのカット編集点と字幕を DaVinci Resolve 用"
                "プロジェクトファイル(.fcpxml) として出力します。"
            )
            self.export_button.clicked.connect(self._on_export_resolve)
            button_row.addWidget(self.export_button)
        self.decide_button = QPushButton("完了（切り抜き＋字幕焼き込み）")
        self.decide_button.setDefault(True)
        self.decide_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.clicked.connect(self.reject)
        button_row.addWidget(self.decide_button)
        button_row.addWidget(self.cancel_button)
        root.addLayout(button_row)

    # グラフのマーカクリック → 対応クリップ行を選択
    def _on_graph_selected(self, clip_index):
        for row, p in enumerate(self._prepared):
            if p.get("index") == clip_index:
                self.clip_list.setCurrentRow(row)
                return

    # クリップ行が変わった → 現在の編集を保存し、新クリップを読み込む
    def _on_row_changed(self, row):
        self._save_current()
        self._current = row
        if 0 <= row < len(self._prepared):
            state = self._states[row]
            self.editor.set_items(state["items"])
            self.editor.set_theme(state["theme"])
            # プレビュー対象を無音カット後クリップへ切り替える (字幕と時間軸が一致 / resolve23)
            p = self._prepared[row]
            self.preview.set_source(
                p.get("prepared_path", ""), p.get("eff_cfg") or {}, p.get("profile"))

    # 現在クリップの編集結果 (字幕・テーマ) を states へ退避する
    def _save_current(self):
        if 0 <= self._current < len(self._states):
            self._states[self._current]["items"] = self.editor.result_items()
            self._states[self._current]["theme"] = self.editor.theme_value()

    # 完了時: 全クリップの編集結果を返す
    # 戻り値: [{"index","items","theme","use"}]  (use=False は焼き込みで除外)
    def result_data(self):
        self._save_current()
        results = []
        for row, p in enumerate(self._prepared):
            item = self.clip_list.item(row)
            use = item is not None and item.checkState() == Qt.Checked
            state = self._states[row]
            results.append({
                "index": p.get("index"),
                "items": state["items"],
                "theme": state["theme"],
                "use": use,
            })
        return results

    # Resolve 出力ボタンを出せるか (元 VOD の有無 + 設定の有効/無効 / resolve20 §5.4)
    def _can_export(self):
        return bool(self._source_path) and resolve_export.is_enabled(self._settings)

    # Resolve 出力用のクリップ情報を組み立てる (使用チェックのみ・TOP 順)
    # keep_segments はクリップ内の残す区間 (無音カット OFF/未実行なら None → 区間全長)。
    def _export_entries(self):
        self._save_current()
        entries = []
        for row, p in enumerate(self._prepared):
            list_item = self.clip_list.item(row)
            if list_item is not None and list_item.checkState() != Qt.Checked:
                continue
            state = self._states[row]
            entries.append({
                "index": p.get("index"),
                "start": p.get("start", 0.0),
                "end": p.get("end", 0.0),
                "keep_segments": p.get("keep_segments"),
                "items": state["items"],
                "theme": state["theme"],
                "eff_cfg": p.get("eff_cfg"),
            })
        return entries

    # 「DaVinci Resolve ファイル出力」押下: 現在の編集内容で FCPXML を書き出す
    def _on_export_resolve(self):
        entries = self._export_entries()
        run_resolve_export(
            self,
            lambda confirm: resolve_export.export_archive_result(
                self._source_path, entries, self._settings, overwrite_confirm=confirm),
        )

    def done(self, code):
        # ダイアログ終了時 (完了/キャンセル/×) にプレビューの再生停止と一時領域掃除を行う。
        # prepared_path は焼き込みで引き続き使うため削除しない (削除対象はプレビュー一時のみ)。
        self.preview.shutdown()
        # 保持していた編集点を破棄する (次回実行へ持ち越さない / resolve20 §5.3 寿命管理)
        for p in self._prepared:
            p.pop("keep_segments", None)
        super().done(code)


# ワーカースレッド → メインスレッドで結果画面を開く橋渡し
# SubtitleReviewBridge (main_window.py) と同型。ワーカーを Event でブロックし、
# メインスレッドで ArchiveResultWindow を開いて編集結果 or None(キャンセル) を返す。
class ArchiveResultBridge(QObject):

    result_requested = Signal(object)  # {"prepared","curve"} を渡す

    def __init__(self, parent_window=None, settings=None, source_path="",
                 default_font="", default_size=None, font_families=None,
                 theme_placeholder=""):
        super().__init__()
        self._parent_window = parent_window
        self._settings = settings or {}
        self._source_path = source_path
        self._default_font = default_font
        self._default_size = default_size
        self._font_families = font_families
        self._theme_placeholder = theme_placeholder
        self._event = threading.Event()
        self._result = None
        self.result_requested.connect(self._on_requested, Qt.QueuedConnection)

    # ワーカースレッドから呼ばれる。prepared/curve を渡し、編集結果 or None を返す。
    def __call__(self, prepared, curve):
        self._event.clear()
        self._result = None
        self.result_requested.emit({"prepared": prepared, "curve": curve})
        self._event.wait()
        return self._result

    # メインスレッドで結果画面を開く
    def _on_requested(self, payload):
        try:
            window = ArchiveResultWindow(
                payload["prepared"], payload["curve"], self._source_path, self._settings,
                default_font=self._default_font, default_size=self._default_size,
                font_families=self._font_families, theme_placeholder=self._theme_placeholder,
                parent=self._parent_window,
            )
            if window.exec() == QDialog.Accepted:
                self._result = window.result_data()
            else:
                self._result = None
        finally:
            self._event.set()
