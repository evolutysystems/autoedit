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

from ..services.billing import BillingService, TYPE_STRIPE, TYPE_TWITCH
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


# Stripe の決済ページ / カスタマーポータルの URL 取得をワーカースレッドで行う。
# 利用者が押した操作なので、失敗は理由つきで通知する (ver6 resolve §4.2)。
class BillingUrlWorker(QThread):

    ready = Signal(str)         # 取得できた URL
    failed = Signal(str)        # 失敗した理由

    def __init__(self, billing, action, parent=None):
        super().__init__(parent)
        self._billing = billing
        self._action = action   # "checkout" / "portal"

    def run(self):
        try:
            if self._action == "checkout":
                self.ready.emit(self._billing.start_checkout())
            else:
                self.ready.emit(self._billing.open_portal())
        except Exception as e:  # noqa: BLE001 (GUI へ集約通知するため広く捕捉)
            _logger.exception("サブスクリプションの URL 取得に失敗")
            self.failed.emit(str(e))


# サブスクリプションの契約情報の取得をワーカースレッドで行う。
# config() は HTTP を伴うため、GUI スレッドから直接呼ぶと
# API へ到達できないときにタイムアウト (既定 15 秒) ぶん画面が固まる。
class BillingConfigWorker(QThread):

    loaded = Signal(object)     # BillingConfig (取れなければ enabled=False のもの)

    def __init__(self, billing, parent=None):
        super().__init__(parent)
        self._billing = billing

    def run(self):
        config = None
        try:
            config = self._billing.config()
        except Exception:  # noqa: BLE001 (契約情報が取れなくても画面は開いたままにする)
            _logger.exception("サブスクリプションの契約情報の取得に失敗")
        self.loaded.emit(config)


# 支払いがサーバーへ反映される (unlimited が立つ) のを待つ (ver6 resolve §4.4)。
#
# 支払いを取りやめてブラウザを閉じた場合もタイムアウトで False になる。
# 異常ではないため、呼び出し側は「確認できなかった」と伝えるだけにする。
class ActivationWorker(QThread):

    done = Signal(bool)

    def __init__(self, billing, parent=None):
        super().__init__(parent)
        self._billing = billing

    def run(self):
        activated = False
        try:
            activated = self._billing.wait_for_activation(
                should_continue=lambda: not self.isInterruptionRequested())
        except Exception:  # noqa: BLE001 (待ち合わせの失敗で画面を壊さない)
            _logger.exception("サブスクリプションの反映待ちに失敗")
        self.done.emit(activated)


class AccountTab(QWidget):

    def __init__(self, points, settings=None, parent=None):
        super().__init__(parent)
        self._points = points
        self._settings = settings or {}
        self._balance_worker = None
        self._history_worker = None
        self._login_worker = None
        self._billing_worker = None
        self._config_worker = None
        self._activation_worker = None
        # 契約情報と加入種別は別々のタイミングで届く。両方そろってから節を描く。
        self._billing_config = None
        self._subscription_type = None
        # サブスクリプション (Stripe)。points が無い (CLI / テスト) なら導線は出さない。
        self._billing = BillingService(points, self._settings) if points is not None else None

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

        # ---- サブスクリプション (Stripe) ---------------------------------
        # API 側が未実装・未設定のあいだは節ごと出さない (ver6 resolve §4.3)。
        # 価格はサーバーから配られる。配布済みの exe に価格を焼き込まないため。
        self.billing_title = QLabel("サブスクリプション")
        theme.mark_title(self.billing_title)
        layout.addWidget(self.billing_title)

        self.billing_price_label = QLabel("")
        layout.addWidget(self.billing_price_label)

        self.billing_note_label = QLabel("")
        self.billing_note_label.setWordWrap(True)
        theme.mark_note(self.billing_note_label)
        layout.addWidget(self.billing_note_label)

        billing_row = QHBoxLayout()
        self.checkout_button = QPushButton("サブスクリプションに登録")
        self.checkout_button.clicked.connect(self._on_checkout)
        billing_row.addWidget(self.checkout_button)
        self.portal_button = QPushButton("サブスクリプションを管理")
        self.portal_button.clicked.connect(self._on_portal)
        billing_row.addWidget(self.portal_button)
        billing_row.addStretch(1)
        layout.addLayout(billing_row)

        self._show_billing(False)

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
            # 登録には JWT が要るため、未ログインでは節ごと出さない。
            self._subscription_type = None
            self._show_billing(False)
            return

        user = self._points.auth.user() or {}
        name = user.get("displayName") or user.get("twitchUserId") or ""
        self.status_label.setText(f"ログイン中: {name}" if name else "ログイン中")
        self.balance_label.setText("残高を取得しています…")
        self._start_balance()
        self._start_history()
        self._start_billing_config()

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
            # 状態が分からないまま登録ボタンを出すと二重課金を招く。
            self._subscription_type = None
            self._show_billing(False)
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

        # 残高の応答で初めて subscriptionType が分かるため、ここで節を作り直す。
        self._subscription_type = subscription
        self._refresh_billing()

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

    # ---- サブスクリプション (Stripe) -------------------------------------
    # 節ごとの表示 / 非表示。API 未実装・オフライン・未ログインでは出さない。
    def _show_billing(self, visible):
        for widget in (self.billing_title, self.billing_price_label,
                       self.billing_note_label, self.checkout_button,
                       self.portal_button):
            widget.setVisible(bool(visible))

    # 契約情報の取得を始める。HTTP を伴うため GUI スレッドでは呼ばない。
    def _start_billing_config(self):
        if self._billing is None:
            return
        if self._config_worker is not None and self._config_worker.isRunning():
            return
        self._config_worker = BillingConfigWorker(self._billing, parent=self)
        self._config_worker.loaded.connect(self._on_billing_config)
        self._config_worker.start()

    def _on_billing_config(self, config):
        self._billing_config = config
        self._refresh_billing()

    # 契約情報と加入種別がそろったら節を描く。
    # どちらかでも欠けていれば出さない。片方だけで描くと、
    # 加入中の人に「登録」を出してしまう瞬間ができる。
    def _refresh_billing(self):
        config = self._billing_config
        subscription_type = self._subscription_type
        if config is None or subscription_type is None or not config.is_available():
            self._show_billing(False)
            return

        self._show_billing(True)
        self.billing_price_label.setText(config.price_label)

        subscribed_here = subscription_type == TYPE_STRIPE
        self.checkout_button.setVisible(not subscribed_here)
        self.portal_button.setVisible(subscribed_here and config.manageable)

        if subscribed_here:
            self.billing_note_label.setText(
                "サブスクリプションに加入中です。"
                "解約・カードの変更・請求書の確認は「サブスクリプションを管理」から行えます。")
            return

        if subscription_type == TYPE_TWITCH:
            # Twitch サブは切れる。切れたあとも続けたい人のために登録は出すが、
            # 黙って出すと「今も無制限なのに課金させられた」という話になる (§4.6)。
            self.billing_note_label.setText(
                "現在は Twitch サブスクリプションの特典で無制限にご利用いただけます。"
                "登録しておくと、Twitch サブスクが切れたあとも継続してご利用いただけます"
                "(両方に加入した場合、料金は二重に発生します)。")
            return

        self.billing_note_label.setText(
            "ポイントを消費せず、透かしの入らない出力を回数無制限で行えます。"
            "お支払いはブラウザ (Stripe) で行います。")

    def _on_checkout(self):
        self._start_billing_url("checkout")

    def _on_portal(self):
        self._start_billing_url("portal")

    # 決済ページ / 管理ページの URL 取得を始める。
    def _start_billing_url(self, action):
        if self._billing is None:
            return
        if self._billing_worker is not None and self._billing_worker.isRunning():
            return

        self.checkout_button.setEnabled(False)
        self.portal_button.setEnabled(False)
        self.billing_note_label.setText("ブラウザで手続きしてください…")

        self._billing_worker = BillingUrlWorker(self._billing, action, parent=self)
        if action == "checkout":
            self._billing_worker.ready.connect(self._on_checkout_url)
        else:
            self._billing_worker.ready.connect(self._on_portal_url)
        self._billing_worker.failed.connect(self._on_billing_failed)
        self._billing_worker.finished.connect(self._reset_billing_buttons)
        self._billing_worker.start()

    # 決済ページを開き、反映を待ち始める。
    def _on_checkout_url(self, url):
        QDesktopServices.openUrl(QUrl(url))
        self.billing_note_label.setText(
            "ブラウザでお支払いを完了してください。反映を待っています…")
        self._start_activation()

    # 管理ページを開く。解約はブラウザ側で完結するため、反映は待たない
    # (解約しても期間の末日までは unlimited のままで、画面は変わらない)。
    def _on_portal_url(self, url):
        QDesktopServices.openUrl(QUrl(url))
        self.billing_note_label.setText(
            "ブラウザで手続きしてください。変更後は「更新」で反映されます。")

    def _on_billing_failed(self, message):
        self.billing_note_label.setText("")
        QMessageBox.warning(
            self, "手続きを開始できません",
            f"サブスクリプションの手続きを開始できませんでした。\n{message}")
        self.reload()

    def _reset_billing_buttons(self):
        self.checkout_button.setEnabled(True)
        self.portal_button.setEnabled(True)

    # 支払いがサーバーへ反映されるのを待つ。画面は操作可能なままにする (§4.7)。
    def _start_activation(self):
        if self._activation_worker is not None and self._activation_worker.isRunning():
            return
        self._activation_worker = ActivationWorker(self._billing, parent=self)
        self._activation_worker.done.connect(self._on_activation)
        self._activation_worker.start()

    def _on_activation(self, activated):
        if activated:
            self.reload()
            return
        # 支払いを取りやめた場合もここへ来る。失敗とは書かない。
        self.billing_note_label.setText(
            "お支払いの反映を確認できませんでした。"
            "手続きが完了している場合は、しばらくしてから「更新」を押してください。")

    # 反映待ちは最大 180 秒動く。タブが閉じられたら止める。
    def closeEvent(self, event):
        if self._activation_worker is not None and self._activation_worker.isRunning():
            self._activation_worker.requestInterruption()
            self._activation_worker.wait(5000)
        super().closeEvent(event)
