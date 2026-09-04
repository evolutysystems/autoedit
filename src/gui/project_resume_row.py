# 「編集の続き」の 1 行 (コンボ + 開く + 一覧) を提供する共有ウィジェット
# (docs/request/ver3/resolve9.md §5.9)
#
# クリップ用タブとアーカイブ切り抜き用タブで同じ行を使う。履歴は種別ごとに分けて
# 持つため (recent / recent_archive)、各タブのコンボにはその種別のものだけが並ぶ。
# 削除・リネームは一覧画面 (ProjectLibraryDialog) 側に置く。破壊的な操作の入口を
# 2 か所に作らないため。
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)

from ..timeline import project_io
from ..timeline.builder import timeline_config
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 一覧末尾の「ファイルから選ぶ」項目 (データは空文字)
BROWSE_LABEL = "ファイルから選ぶ..."
EMPTY_LABEL = "（保存されたプロジェクトはありません）"
PROJECT_FILE_FILTER = "Timeline プロジェクト (*.json);;すべてのファイル (*)"


# 種別が違うプロジェクトを開こうとしていないか確かめる (ver3 resolve9 §3-4)。
# クリップ用とアーカイブ用は開く経路も書き出し方も違うため、取り違えると壊れる。
#
# クリップ用タブは「編集の続き」行を持たなくなった (ver3 resolve15 C5) が、
# D&D と一覧からプロジェクトを選ぶため同じ確認が要る。行とタブの双方から
# 使えるよう module 関数として置く (resolve15 §5.8)。
#
# 戻り値: (種別が一致したか, 実際の種別)。
#   一致        → (True, 種別)
#   種別違い    → (False, 実際の種別)  … 案内を出したうえで種別を返す
#   読めない    → (False, "")          … 案内を出す。呼び出し側は何もしない
def confirm_project_kind(parent, path, kind):
    try:
        timeline = project_io.load(path, validate_timeline=False)
        actual = project_io.project_kind(timeline)
    except Exception:  # noqa: BLE001 (壊れたファイルはここで弾いて案内する)
        QMessageBox.warning(
            parent, "開けません",
            f"プロジェクトファイルを読めませんでした。\n{path}")
        return False, ""
    if actual == kind:
        return True, actual
    other = ("アーカイブ切り抜き用" if actual == project_io.KIND_ARCHIVE
             else "クリップ用")
    QMessageBox.information(
        parent, "種別が違います",
        f"このプロジェクトは{other}です。\n"
        f"{other}のタブへ切り替えます。そちらで再開してください。")
    return False, actual


class ProjectResumeRow(QWidget):

    # 「開く...」で選ばれたプロジェクト (種別の確認済み)
    resume_requested = Signal(str)
    # 「一覧...」で一覧画面を開きたい
    library_requested = Signal()
    # 種別違いのプロジェクトが選ばれた (相手のタブへ回してもらう)
    wrong_kind_selected = Signal(str, str)      # (project_path, kind)

    # kind        : "clip" / "archive" (履歴キーと種別の確認に使う)
    # settings_ref: 呼び出し側の設定辞書 (履歴の読み書きに使う)
    def __init__(self, kind, settings_ref, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._settings = settings_ref
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("編集の続き:"))

        self.project_combo = QComboBox()
        self.project_combo.setToolTip(
            "保存した Timeline プロジェクトを開いて編集の続きから書き出します")
        layout.addWidget(self.project_combo, 1)

        self.resume_button = QPushButton("開く...")
        self.resume_button.clicked.connect(self._on_resume)
        layout.addWidget(self.resume_button)

        self.library_button = None
        if self._library_cfg()["enabled"]:
            self.library_button = QPushButton("一覧...")
            self.library_button.setToolTip(
                "保存したプロジェクトを一覧から開く・名前を変える・削除します")
            self.library_button.clicked.connect(self.library_requested.emit)
            layout.addWidget(self.library_button)

    # ------------------------------------------------------------------
    # 設定・履歴
    # ------------------------------------------------------------------

    def _timeline_cfg(self):
        return timeline_config(self._settings)

    def _library_cfg(self):
        return self._timeline_cfg()["project"]["library"]

    def _recent_key(self):
        return ("recent_archive" if self._kind == project_io.KIND_ARCHIVE
                else "recent")

    def _recent(self):
        project_cfg = (self._settings.get("timeline", {}) or {}).get("project", {}) or {}
        return [p for p in (project_cfg.get(self._recent_key()) or []) if p]

    # 呼び出し側が設定を読み直した場合に差し替える
    def set_settings(self, settings_ref):
        self._settings = settings_ref
        self.refresh()

    # 履歴からコンボを作り直す
    def refresh(self):
        current = self.project_combo.currentData()
        self.project_combo.clear()
        recent = self._recent()
        for path in recent:
            self.project_combo.addItem(os.path.basename(path), path)
            self.project_combo.setItemData(
                self.project_combo.count() - 1, path, Qt.ToolTipRole)
        if not recent:
            # 一覧が空でも行ごと消さない (「開く...」だけ押せる状態にする)
            self.project_combo.addItem(EMPTY_LABEL, "")
        self.project_combo.addItem(BROWSE_LABEL, "")
        if current:
            index = self.project_combo.findData(current)
            if index >= 0:
                self.project_combo.setCurrentIndex(index)

    # D&D や一覧から渡されたものを選択状態にする
    def select(self, path):
        if not path:
            return
        index = self.project_combo.findData(path)
        if index < 0:
            if self.project_combo.itemText(0) == EMPTY_LABEL:
                self.project_combo.removeItem(0)
            self.project_combo.insertItem(0, os.path.basename(path), path)
            self.project_combo.setItemData(0, path, Qt.ToolTipRole)
            index = 0
        self.project_combo.setCurrentIndex(index)

    # 実行中は行ごと無効化する
    def set_busy(self, busy):
        self.project_combo.setEnabled(not busy)
        self.resume_button.setEnabled(not busy)
        if self.library_button is not None:
            self.library_button.setEnabled(not busy)

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------

    def _on_resume(self):
        path = self.project_combo.currentData() or ""
        if not path:
            path = self._browse()
        if not path:
            return
        if not os.path.exists(path):
            QMessageBox.warning(
                self, "開けません",
                f"プロジェクトファイルが見つかりません。\n{path}")
            return
        if not self._check_kind(path):
            return
        self.resume_requested.emit(path)

    # プロジェクトファイルを選ぶ (初期フォルダ: project_dir → 出力先 → 直近のフォルダ)
    def _browse(self):
        timeline_cfg = self._settings.get("timeline", {}) or {}
        start_dir = (timeline_cfg.get("project_dir", "")
                     or (self._settings.get("general", {}) or {}).get(
                         "output_directory", ""))
        recent = self._recent()
        if not start_dir and recent:
            start_dir = os.path.dirname(recent[0])
        path, _ = QFileDialog.getOpenFileName(
            self, "Timeline プロジェクトを選択", start_dir, PROJECT_FILE_FILTER)
        return path

    # 種別が違うプロジェクトを開こうとしていないか確かめる (ver3 resolve9 §3-4)
    def _check_kind(self, path):
        ok, kind = confirm_project_kind(self, path, self._kind)
        if ok or not kind:
            return ok
        # タブの切り替えは親 (MainWindow) に任せる。選んだだけで長い処理が
        # 始まらないよう、実行はしない (選択状態にするところまで)。
        self.wrong_kind_selected.emit(path, kind)
        return False
