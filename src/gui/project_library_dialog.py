# 保存済み Timeline プロジェクトの一覧画面
# (docs/request/ver3/resolve9.md §5.12 / §5.14 / §5.10)
#
# 各行に Timeline の V1 から取ったコマ (帯) を出し、開く・リネーム・削除ができる。
# 一覧はタブごとに分ける (クリップ用タブにはクリップ用だけ / 回答 Q3)。
#
# 応答性のため 2 段構えにする (§3-11):
#   ① 走査 (os.scandir + 履歴) の結果で行をすぐ出す。種別はファイル名で一次判定。
#   ② 背景スレッドで JSON を読み、種別を確定して情報とサムネイルを埋める。
#      種別が食い違った行は取り除く。
import os
from datetime import datetime

from PySide6.QtCore import QSize, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..settings.settings_window import save_settings
from ..timeline import project_io
from ..timeline.builder import timeline_config
from ..utils.logger import get_logger
from . import project_thumbnail, theme

_logger = get_logger(__name__)

# 行のデータを持たせるロール
_PATH_ROLE = Qt.UserRole + 1
_SUMMARY_ROLE = Qt.UserRole + 2

_SORT_CHOICES = [
    ("更新が新しい順", "updated_desc"),
    ("名前順", "name_asc"),
    ("サイズが大きい順", "size_desc"),
]


# 秒を "H:MM:SS" 表記へ
def _fmt_duration(seconds):
    total = int(max(float(seconds or 0.0), 0.0))
    if total >= 3600:
        return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


# バイト数を読みやすい単位へ
def _fmt_size(size):
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


# 更新日時を "MM-DD HH:MM" 表記へ
def _fmt_mtime(path):
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%m-%d %H:%M")
    except OSError:
        return "-"


# 背景で概要とサムネイルを読み込むワーカー (§5.12-b)
class _SummaryWorker(QThread):

    # (パス, 概要 dict, サムネイルのパス一覧)
    loaded = Signal(str, object, object)
    progressed = Signal(int, int)

    def __init__(self, paths, settings, parent=None):
        super().__init__(parent)
        self._paths = list(paths)
        self._settings = settings

    def run(self):
        total = len(self._paths)
        for index, path in enumerate(self._paths, 1):
            if self.isInterruptionRequested():
                return
            try:
                summary = project_io.read_summary(path, self._settings)
            except Exception:  # noqa: BLE001 (1 件の失敗で一覧を止めない)
                _logger.warning("プロジェクトの概要取得に失敗しました: %s", path)
                continue
            if self.isInterruptionRequested():
                return
            thumbs = []
            if not summary.get("broken"):
                thumbs = project_thumbnail.ensure(path, self._settings, summary=summary)
            self.loaded.emit(path, summary, thumbs)
            self.progressed.emit(index, total)


class ProjectLibraryDialog(QDialog):

    # 一覧で選ばれたプロジェクトを開きたい
    open_requested = Signal(str)
    # 履歴・ファイルが変わった (削除・リネームの後)
    changed = Signal()

    # kind        : "clip" / "archive" (母集合とタイトルを決める / 回答 Q3)
    # settings_ref: 呼び出し側の設定辞書 (履歴の読み書きに使う)
    def __init__(self, kind, settings_ref, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._settings = settings_ref
        self._cfg = timeline_config(settings_ref)
        self._library_cfg = self._cfg["project"]["library"]
        self._worker = None
        self._truncated = 0
        self._summaries = {}

        label = ("アーカイブ用" if kind == project_io.KIND_ARCHIVE else "クリップ用")
        self.setWindowTitle(f"Timeline プロジェクト一覧（{label}）")
        self.resize(int(self._library_cfg["window_width"]),
                    int(self._library_cfg["window_height"]))
        theme.install_window_background(self)
        self._build_ui()
        self.reload()

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("名前・パスで絞り込み")
        self.search_edit.textChanged.connect(self._apply_filter)
        top.addWidget(self.search_edit, 1)

        top.addWidget(QLabel("並び"))
        self.sort_combo = QComboBox()
        for text, value in _SORT_CHOICES:
            self.sort_combo.addItem(text, value)
        index = self.sort_combo.findData(self._library_cfg["sort"])
        self.sort_combo.setCurrentIndex(max(index, 0))
        self.sort_combo.activated.connect(lambda _row: self._sort_items())
        top.addWidget(self.sort_combo)

        self.reload_button = QPushButton("再読込")
        self.reload_button.setToolTip("F5")
        self.reload_button.clicked.connect(self.reload)
        top.addWidget(self.reload_button)
        root.addLayout(top)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list_widget.setIconSize(self._icon_size())
        self.list_widget.setWordWrap(True)
        self.list_widget.itemDoubleClicked.connect(lambda _item: self._on_open())
        self.list_widget.itemSelectionChanged.connect(self._update_buttons)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._on_context_menu)
        root.addWidget(self.list_widget, 1)

        bottom = QHBoxLayout()
        self.count_label = QLabel("")
        theme.mark_note(self.count_label)
        bottom.addWidget(self.count_label, 1)

        self.open_button = QPushButton("開く")
        self.open_button.setToolTip("Enter")
        self.open_button.clicked.connect(self._on_open)
        theme.mark_primary(self.open_button)
        bottom.addWidget(self.open_button)

        self.rename_button = None
        if self._cfg["project"]["rename_enabled"]:
            self.rename_button = QPushButton("リネーム")
            self.rename_button.setToolTip("F2")
            self.rename_button.clicked.connect(self._on_rename)
            bottom.addWidget(self.rename_button)

        self.delete_button = None
        if self._cfg["project"]["delete_button"]:
            self.delete_button = QPushButton("削除")
            self.delete_button.setToolTip("Delete")
            self.delete_button.clicked.connect(self._on_delete)
            theme.mark_danger(self.delete_button)
            bottom.addWidget(self.delete_button)

        self.close_button = QPushButton("閉じる")
        self.close_button.clicked.connect(self.reject)
        bottom.addWidget(self.close_button)
        root.addLayout(bottom)
        self._update_buttons()

    def _icon_size(self):
        width = int(self._library_cfg["thumbnail_width_px"])
        count = max(int(self._library_cfg["thumbnail_count"]), 1)
        # 高さは 16:9 を目安に決める (実際の絵はアスペクト比を保って収まる)
        return QSize(width * count + 2 * (count - 1), int(width * 9 / 16))

    # ------------------------------------------------------------------
    # 読み込み
    # ------------------------------------------------------------------

    def reload(self):
        self._stop_worker()
        self.list_widget.clear()
        self._summaries.clear()
        scanned = project_io.scan_projects(
            self._settings, kind=self._kind, timeline_cfg=self._cfg)
        self._truncated = scanned["truncated"]
        for path in scanned["paths"]:
            self._add_item(path)
        self._sort_items()
        self._update_count()
        self._update_buttons()
        if scanned["paths"]:
            self._worker = _SummaryWorker(scanned["paths"], self._settings, parent=self)
            self._worker.loaded.connect(self._on_summary)
            self._worker.progressed.connect(self._on_progress)
            self._worker.start()

    def _add_item(self, path):
        item = QListWidgetItem()
        item.setData(_PATH_ROLE, path)
        item.setText(self._item_text(path, None))
        item.setToolTip(path)
        self.list_widget.addItem(item)
        return item

    # 1 行の文言 (3 行構成)
    def _item_text(self, path, summary):
        name = project_io.display_name(path, self._cfg)
        marks = []
        if summary is not None:
            if summary.get("has_media_dir"):
                marks.append("素材の複製あり")
            if summary.get("has_autosave"):
                marks.append("自動保存あり")
            if summary.get("broken"):
                marks.append("⚠ 読み込めません")
        head = name if not marks else f"{name}    {' / '.join(marks)}"

        if summary is None:
            middle = "読み込み中…"
        elif summary.get("broken"):
            middle = f"更新 {_fmt_mtime(path)} / {_fmt_size(summary.get('size'))}"
        else:
            middle = (f"V1 {summary.get('clip_count', 0)} クリップ / "
                      f"{_fmt_duration(summary.get('duration_sec'))} / "
                      f"更新 {_fmt_mtime(path)} / "
                      f"{_fmt_size(summary.get('size'))}")
        return f"{head}\n{middle}\n{path}"

    def _find_item(self, path):
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item.data(_PATH_ROLE) == path:
                return item
        return None

    # 背景スレッドからの結果を行へ流し込む
    def _on_summary(self, path, summary, thumbs):
        item = self._find_item(path)
        if item is None:
            return
        # 種別が一次判定と食い違ったら取り除く (規約外の名前で保存されたもの)
        if not summary.get("broken") and summary.get("kind") != self._kind:
            self.list_widget.takeItem(self.list_widget.row(item))
            self._update_count()
            return
        self._summaries[path] = summary
        item.setData(_SUMMARY_ROLE, summary)
        item.setText(self._item_text(path, summary))
        pixmap = project_thumbnail.build_pixmap(thumbs, self._settings)
        if pixmap is not None:
            item.setIcon(QIcon(pixmap))
        self._apply_filter()

    def _on_progress(self, done, total):
        if done >= total:
            self._update_count()
            self._sort_items()
        else:
            self.count_label.setText(f"読み込み中… {done}/{total}")

    def _stop_worker(self):
        if self._worker is None:
            return
        self._worker.requestInterruption()
        self._worker.wait(3000)
        self._worker = None

    # ------------------------------------------------------------------
    # 並び替え・絞り込み
    # ------------------------------------------------------------------

    def _sort_items(self):
        mode = self.sort_combo.currentData()
        rows = []
        while self.list_widget.count():
            rows.append(self.list_widget.takeItem(0))

        def key(item):
            path = item.data(_PATH_ROLE)
            summary = self._summaries.get(path) or {}
            if mode == "name_asc":
                return project_io.display_name(path, self._cfg).lower()
            if mode == "size_desc":
                return -float(summary.get("size", 0) or 0)
            try:
                return -os.path.getmtime(path)
            except OSError:
                return 0.0

        for item in sorted(rows, key=key):
            self.list_widget.addItem(item)
        self._apply_filter()

    def _apply_filter(self):
        text = self.search_edit.text().strip().lower()
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            path = str(item.data(_PATH_ROLE) or "")
            item.setHidden(bool(text) and text not in path.lower())

    def _update_count(self):
        visible = self.list_widget.count()
        text = f"{visible} 件"
        if self._truncated:
            text += f"（他 {self._truncated} 件は更新日時が古いため表示していません）"
        self.count_label.setText(text)

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------

    def _selected_paths(self):
        return [str(item.data(_PATH_ROLE))
                for item in self.list_widget.selectedItems()]

    def _update_buttons(self):
        selected = self._selected_paths()
        self.open_button.setEnabled(len(selected) == 1)
        if self.rename_button is not None:
            self.rename_button.setEnabled(len(selected) == 1)
        if self.delete_button is not None:
            self.delete_button.setEnabled(bool(selected))

    def _on_context_menu(self, position):
        item = self.list_widget.itemAt(position)
        if item is None:
            return
        menu = QMenu(self)
        menu.addAction("開く", self._on_open)
        if self.rename_button is not None:
            menu.addAction("リネーム", self._on_rename)
        if self.delete_button is not None:
            menu.addAction("削除", self._on_delete)
        menu.addSeparator()
        menu.addAction("フォルダを開く", self._on_open_folder)
        menu.exec(self.list_widget.viewport().mapToGlobal(position))

    def _on_open(self):
        selected = self._selected_paths()
        if len(selected) != 1:
            return
        path = selected[0]
        if not os.path.exists(path):
            QMessageBox.warning(self, "開けません",
                                f"プロジェクトファイルが見つかりません。\n{path}")
            return
        self.accept()
        self.open_requested.emit(path)

    def _on_open_folder(self):
        selected = self._selected_paths()
        if not selected:
            return
        folder = os.path.dirname(selected[0])
        if os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    # 表示名を変えて改名する (本体・自動保存・素材フォルダ・JSON 内のパス・履歴)
    def _on_rename(self):
        selected = self._selected_paths()
        if len(selected) != 1:
            return
        old_path = selected[0]
        current = project_io.display_name(old_path, self._cfg)
        # 接尾辞 (.timeline.json) はアプリが付けるため入力させない (回答 Q13)
        new_name, ok = QInputDialog.getText(
            self, "名前を変更", "新しい名前:", QLineEdit.Normal, current)
        if not ok or not new_name.strip() or new_name.strip() == current:
            return
        try:
            new_path = project_io.rename_project(
                old_path, new_name.strip(), self._settings, timeline_cfg=self._cfg)
        except Exception as e:  # noqa: BLE001 (理由を見せて一覧は残す)
            QMessageBox.warning(self, "名前を変更できません", str(e))
            return
        project_thumbnail.rename(old_path, new_path, self._settings)
        if project_io.forget_recent(self._settings, old_path, new_path=new_path):
            save_settings(self._settings)
        self.changed.emit()
        self.reload()

    # 選択したプロジェクトを削除する (既定はゴミ箱経由 / §5.10)
    def _on_delete(self):
        selected = self._selected_paths()
        if not selected:
            return
        project_cfg = self._cfg["project"]
        use_trash = bool(project_cfg["delete_to_trash"])
        delete_media = bool(project_cfg["delete_media_dir"])

        targets = []            # (プロジェクト, 消える実体の一覧)
        missing = []
        for path in selected:
            related = project_io.related_paths(
                path, self._cfg, include_media_dir=delete_media)
            entries = [p for p in (related["media_dir"], related["autosave"],
                                   related["blur_cache"], related["project"]) if p]
            if related["project"]:
                targets.append((path, entries))
            else:
                missing.append(path)

        if targets and not self._confirm_delete(targets, use_trash):
            return

        failed = []
        for path, _entries in targets:
            result = project_io.delete_project(
                path, timeline_cfg=self._cfg, delete_media_dir=delete_media,
                use_trash=use_trash)
            failed.extend(result["failed"])
            if result["deleted"]:
                project_thumbnail.discard(path, self._settings)
            project_io.forget_recent(self._settings, path)
        for path in missing:
            project_io.forget_recent(self._settings, path)
        save_settings(self._settings)

        if failed and use_trash:
            self._offer_permanent_delete(failed)
        elif failed:
            QMessageBox.warning(
                self, "削除できませんでした",
                "次のファイルを削除できませんでした。\n\n"
                + "\n".join(f"・{f['path']}（{f['reason']}）" for f in failed[:10]))
        self.changed.emit()
        self.reload()

    def _confirm_delete(self, targets, use_trash):
        lines = []
        total = 0
        for _path, entries in targets:
            for entry in entries:
                size = _entry_size(entry)
                total += size
                lines.append(f"・{entry}（{_fmt_size(size)}）")
        head = ("次のファイルをゴミ箱へ移動します。" if use_trash
                else "次のファイルを削除します。元に戻せません。")
        tail = ("\n\nゴミ箱から元に戻せます。" if use_trash else "")
        shown = lines[:12]
        if len(lines) > len(shown):
            shown.append(f"…ほか {len(lines) - len(shown)} 件")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question if use_trash else QMessageBox.Warning)
        box.setWindowTitle("プロジェクトの削除")
        box.setText(head)
        box.setInformativeText("\n".join(shown) + f"\n\n合計 {_fmt_size(total)}" + tail)
        delete = box.addButton("ゴミ箱へ移動" if use_trash else "削除する",
                               QMessageBox.DestructiveRole)
        cancel = box.addButton("キャンセル", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is delete

    # ゴミ箱へ送れなかったものを完全削除するか聞き直す (黙って完全削除はしない)
    def _offer_permanent_delete(self, failed):
        listed = "\n".join(f"・{f['path']}（{f['reason']}）" for f in failed[:10])
        answer = QMessageBox.question(
            self, "ゴミ箱へ送れませんでした",
            f"次のファイルをゴミ箱へ送れませんでした。\n\n{listed}\n\n"
            "完全に削除しますか?（元に戻せません）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        import shutil
        for entry in failed:
            path = entry["path"]
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                elif os.path.exists(path):
                    os.remove(path)
            except OSError:
                _logger.exception("完全削除に失敗しました: %s", path)

    # ------------------------------------------------------------------
    # キー操作・終了
    # ------------------------------------------------------------------

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._on_open()
            return
        if key == Qt.Key_F2 and self.rename_button is not None:
            self._on_rename()
            return
        if key == Qt.Key_Delete and self.delete_button is not None:
            self._on_delete()
            return
        if key == Qt.Key_F5:
            self.reload()
            return
        super().keyPressEvent(event)

    def done(self, code):
        self._stop_worker()
        super().done(code)


# ファイル・フォルダの大きさ (フォルダは中身の合計)
def _entry_size(path):
    try:
        if os.path.isdir(path):
            total = 0
            for root, _dirs, files in os.walk(path):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass
            return total
        return os.path.getsize(path)
    except OSError:
        return 0
