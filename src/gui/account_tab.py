# 設定画面の「アカウント」タブ (ver5 resolve.md §5.6 R13)
#
# ログイン状態・残高・履歴・サブスク導線を 1 か所にまとめる。
# ここは**表示と操作だけ**を持ち、消費の判断はサーバーが行う (R9)。
#
# ポイントの実体 (PointsService) は画面起動時に作られた共有のものを使う。
# 起動していない (CLI / テスト) 場合は None が来るため、その場合は
# 「未ログイン」として静かに閉じたままにする。
from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..services.stretheus_auth import parse_timestamp
from ..utils.logger import get_logger
from . import theme
from .points_indicator import BalanceWorker, LoginWorker, format_balance, format_reset

_logger = get_logger(__name__)

# 履歴の取得件数 (画面に出す範囲。ページ送りは設けない)
_HISTORY_LIMIT = 20

# 履歴の種別・ジョブ種別の表示名
_KIND_LABELS = {
    "Grant": "付与",
    "Reserve": "予約",
    "Commit": "消費",
    "Cancel": "解放",
}
_JOB_LABELS = {"Clip": "クリップ", "Archive": "アーカイブ"}


# 履歴の取得をワーカースレッドで行う
class TransactionsWorker(QThread):

    loaded = Signal(object)     # [{...}] / 取れなければ []

    def __init__(self, points, limit=_HISTORY_LIMIT, parent=None):
        super().__init__(parent)
        self._points = points
        self._limit = limit

    def run(self):
        items = []
        try:
            response = self._points.auth.client.get(
                f"/api/points/transactions?limit={int(self._limit)}")
            items = list((response or {}).get("items") or [])
        except Exception:  # noqa: BLE001 (履歴が出せなくても画面は開いたままにする)
            _logger.exception("ポイント履歴の取得に失敗")
        self.loaded.emit(items)


class AccountTab(QWidget):

    def __init__(self, points, settings=None, parent=None):
        super().__init__(parent)
        self._points = points
        self._settings = settings or {}
        self._balance_worker = None
        self._history_worker = None
        self._login_worker = None

        layout = QVBoxLayout(self)

        self.status_label = QLabel("")
        theme.mark_title(self.status_label)
        layout.addWidget(self.status_label)

        self.balance_label = QLabel("")
        layout.addWidget(self.balance_label)

        self.note_label = QLabel(
            "ログインしていないあいだは、ポイントを消費せず透かしも入りません。")
        self.note_label.setWordWrap(True)
        theme.mark_note(self.note_label)
        layout.addWidget(self.note_label)

        button_row = QHBoxLayout()
        self.login_button = QPushButton("ログイン")
        self.login_button.clicked.connect(self._on_login)
        button_row.addWidget(self.login_button)
        self.logout_button = QPushButton("ログアウト")
        self.logout_button.clicked.connect(self._on_logout)
        button_row.addWidget(self.logout_button)
        self.refresh_button = QPushButton("更新")
        self.refresh_button.clicked.connect(self.reload)
        button_row.addWidget(self.refresh_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.subscribe_button = QPushButton("Twitch のサブスクページを開く")
        self.subscribe_button.clicked.connect(self._on_subscribe)
        layout.addWidget(self.subscribe_button)

        history_label = QLabel("履歴")
        theme.mark_title(history_label)
        layout.addWidget(history_label)

        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(["日時", "内容", "増減", "残高"])
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.setSelectionMode(QTableWidget.NoSelection)
        self.history_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.history_table, 1)

        self.reload()

    # 表示を作り直す (開いたとき・ログイン状態が変わったとき・「更新」押下時)
    def reload(self):
        logged_in = self._is_logged_in()
        self.login_button.setVisible(not logged_in)
        self.logout_button.setVisible(logged_in)
        self.refresh_button.setVisible(logged_in)
        self.history_table.setVisible(logged_in)
        self.note_label.setVisible(not logged_in)
        self.subscribe_button.setVisible(logged_in and bool(self._subscribe_url()))

        if not logged_in:
            self.status_label.setText("ログインしていません")
            self.balance_label.setText("")
            self.history_table.setRowCount(0)
            return

        user = self._points.auth.user() or {}
        name = user.get("displayName") or user.get("twitchUserId") or ""
        self.status_label.setText(f"ログイン中: {name}" if name else "ログイン中")
        self.balance_label.setText("残高を取得しています…")
        self._start_balance()
        self._start_history()

    def _is_logged_in(self):
        return bool(self._points is not None and self._points.auth.is_logged_in())

    # サブスク導線の URL。設定が空なら導線は出さない (配信者ごとに違うため既定は空)。
    def _subscribe_url(self):
        points_cfg = self._settings.get("points", {}) if isinstance(self._settings, dict) else {}
        return str(points_cfg.get("subscribe_url", "") or "").strip()

    def _start_balance(self):
        if self._balance_worker is not None and self._balance_worker.isRunning():
            return
        self._balance_worker = BalanceWorker(self._points, force=True, parent=self)
        self._balance_worker.loaded.connect(self._on_balance)
        self._balance_worker.start()

    def _on_balance(self, balance):
        balance = balance or self._points.last_balance()
        if not balance:
            self.balance_label.setText("残高を取得できません (オフラインの可能性があります)")
            return

        lines = [format_balance(balance)]
        if not balance.get("unlimited"):
            costs = balance.get("costs") or {}
            clip = costs.get("clip")
            archive = costs.get("archive")
            if clip is not None and archive is not None:
                lines.append(f"単価: クリップ {int(clip)} pt / アーカイブ {int(archive)} pt")
        reset = format_reset(balance)
        subscription = str(balance.get("subscriptionType") or "None")
        if subscription != "None":
            lines.append(f"サブスク: {subscription}")
        elif reset:
            lines.append(f"次回リセット: {reset}")
        self.balance_label.setText("\n".join(lines))

    def _start_history(self):
        if self._history_worker is not None and self._history_worker.isRunning():
            return
        self._history_worker = TransactionsWorker(self._points, parent=self)
        self._history_worker.loaded.connect(self._on_history)
        self._history_worker.start()

    def _on_history(self, items):
        self.history_table.setRowCount(len(items))
        for row, item in enumerate(items):
            created = parse_timestamp(item.get("createdAt"))
            when = created.astimezone().strftime("%m/%d %H:%M") if created else ""
            amount = int(item.get("amount") or 0)
            cells = [
                when,
                self._describe(item),
                f"{amount:+d}" if amount else "0",
                str(int(item.get("balanceAfter") or 0)),
            ]
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if column >= 2:
                    cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.history_table.setItem(row, column, cell)

    # 履歴 1 件の内容 (種別 + ジョブ種別 + 透かしの有無)
    @staticmethod
    def _describe(item):
        kind = _KIND_LABELS.get(str(item.get("kind") or ""), str(item.get("kind") or ""))
        job = _JOB_LABELS.get(str(item.get("jobType") or ""), "")
        text = f"{kind} ({job})" if job else kind
        if item.get("unlimited"):
            return f"{text} / サブスク"
        if item.get("watermarked"):
            return f"{text} / 透かし入り"
        return text

    def _on_login(self):
        if self._points is None:
            QMessageBox.information(
                self, "ログインできません",
                "メイン画面から起動した場合のみログインできます。")
            return
        if self._login_worker is not None and self._login_worker.isRunning():
            return

        self.login_button.setEnabled(False)
        self.login_button.setText("ブラウザで許可してください…")
        self._login_worker = LoginWorker(self._points.auth, parent=self)
        self._login_worker.logged_in.connect(lambda _user: self.reload())
        self._login_worker.failed.connect(self._on_login_failed)
        self._login_worker.finished.connect(self._reset_login_button)
        self._login_worker.start()

    def _on_login_failed(self, message):
        QMessageBox.warning(self, "ログインできません",
                            f"ログインに失敗しました。\n{message}")

    def _reset_login_button(self):
        self.login_button.setEnabled(True)
        self.login_button.setText("ログイン")

    def _on_logout(self):
        answer = QMessageBox.question(
            self, "ログアウト",
            "ログアウトすると、次の出力からポイントを消費しなくなります"
            "(そのぶん透かしも入りません)。\nログアウトしますか？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return

        try:
            self._points.auth.logout()
        except Exception as e:  # noqa: BLE001 (失敗しても画面は開いたままにする)
            _logger.exception("ログアウトに失敗")
            QMessageBox.warning(self, "ログアウトできません", str(e))
        self.reload()

    def _on_subscribe(self):
        url = self._subscribe_url()
        if url:
            QDesktopServices.openUrl(QUrl(url))
