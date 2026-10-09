# 設定画面の「アカウント」タブ (ver5 resolve.md §5.6 R13 / ver6 resolve2 §2.3)
#
# ログイン状態・残高・サブスクの状態と履歴・ポイント履歴を 1 か所にまとめる。
# ここは**表示と操作だけ**を持ち、消費の判断はサーバーが行う (R9)。
#
# ver6 resolve2 で役割を分けた。**加入する導線はここには無い。**
#   メイン画面 → サブスクリプション画面 … 加入する (Twitch ログイン / 購入)
#   設定画面 → アカウント (ここ)        … 状態を確認する・履歴を見る・管理する
# 「サブスクリプションを管理」(解約・カード変更) は残す。メイン画面のボタンは
# 加入すると押せなくなるため、ここを外すと解約へたどり着けなくなる。
#
# ポイントの実体 (PointsService) は画面起動時に作られた共有のものを使う。
# 起動していない (CLI / テスト) 場合は None が来るため、その場合は
# 「未ログイン」として静かに閉じたままにする。
#
# 未ログインでもポイント制の対象 (端末に紐づく匿名の台帳) であるため、
# 残高と履歴は未ログインでも出す (ver7 resolve §4)。
# サブスクリプションの節だけは Twitch ログインが要る (匿名では加入できない)。
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

from ..i18n import tr
from ..services.billing import BillingService, TYPE_NONE, TYPE_STRIPE, TYPE_TWITCH
from ..services.stretheus_auth import parse_timestamp
from ..utils.logger import get_logger
from . import theme
from .billing_workers import BillingConfigWorker, BillingUrlWorker, SubscriptionWorker
from .points_indicator import BalanceWorker, LoginWorker, format_balance, format_reset

_logger = get_logger(__name__)

# 履歴の取得件数 (画面に出す範囲。ページ送りは設けない)
_HISTORY_LIMIT = 20

# サブスク履歴の表を何行ぶんの高さで出すか (これを超えたぶんはスクロールで見る)。
# 高さを抑えないと、加入直後で 1 件しか無くても表が画面いっぱいへ広がり、
# 下にあるポイント履歴が潰れる。
_SUBSCRIPTION_HISTORY_ROWS = 5

# 履歴の種別・ジョブ種別の表示名
_KIND_LABELS = {
    "Grant": "付与",
    "Reserve": "予約",
    "Commit": "消費",
    "Cancel": "解放",
}
_JOB_LABELS = {"Clip": "クリップ", "Archive": "アーカイブ"}

# サブスクリプションの加入種別の表示名 (ver6 resolve2 §4.4)
_SUBSCRIPTION_LABELS = {
    TYPE_NONE: "未加入",
    TYPE_STRIPE: "サブスクリプション加入中 (Stripe)",
    TYPE_TWITCH: "Twitch サブスクリプション",
}

# 加入状態 (API の status) の表示名
_STATUS_LABELS = {
    "active": "有効",
    "canceling": "解約予定 (期間末まで有効)",
    "past_due": "支払いが確認できていません",
    "none": "なし",
}

# サブスク履歴の出来事 (API の event) の表示名
_EVENT_LABELS = {
    "subscribed": "加入",
    "renewed": "更新",
    "canceled": "解約",
    "expired": "終了",
    "payment_failed": "支払い失敗",
}


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
            # 未ログインでも端末の履歴を出す。JWT が無ければ匿名セッションを作る。
            self._points.auth.ensure_session()
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
        self._billing_worker = None
        self._config_worker = None
        self._subscription_worker = None
        # 契約情報と加入種別は別々のタイミングで届く。両方そろってから節を描く。
        self._billing_config = None
        self._subscription_type = None
        # GET /api/billing/subscription の応答 (期間と履歴)。取れなければ None
        self._subscription_state = None
        # サブスクリプション (Stripe)。points が無い (CLI / テスト) なら導線は出さない。
        self._billing = BillingService(points, self._settings) if points is not None else None

        layout = QVBoxLayout(self)

        self.status_label = QLabel("")
        theme.mark_title(self.status_label)
        layout.addWidget(self.status_label)

        self.balance_label = QLabel("")
        layout.addWidget(self.balance_label)

        self.note_label = QLabel(
            tr("ログインしていないあいだは、この端末のポイントを消費します"
               "(残高が足りない出力には透かしが入ります)。"
               "ログインするとアカウントのポイントに切り替わります。"))
        self.note_label.setWordWrap(True)
        theme.mark_note(self.note_label)
        layout.addWidget(self.note_label)

        button_row = QHBoxLayout()
        self.login_button = QPushButton(tr("ログイン"))
        self.login_button.clicked.connect(self._on_login)
        button_row.addWidget(self.login_button)
        self.logout_button = QPushButton(tr("ログアウト"))
        self.logout_button.clicked.connect(self._on_logout)
        button_row.addWidget(self.logout_button)
        self.refresh_button = QPushButton(tr("更新"))
        self.refresh_button.clicked.connect(self.reload)
        button_row.addWidget(self.refresh_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.subscribe_button = QPushButton(tr("Twitch のサブスクページを開く"))
        self.subscribe_button.clicked.connect(self._on_subscribe)
        layout.addWidget(self.subscribe_button)

        # ---- サブスクリプション状態 (ver6 resolve2 §2.3 / ③) ---------------
        # 状態は 2 つの情報から作る。
        #   種別    … GET /api/points の subscriptionType (権威)
        #   期間/履歴 … GET /api/billing/subscription (新設。取れなければ出さない)
        # 登録の導線はここには置かない (メイン画面へ移した)。
        self.billing_title = QLabel(tr("サブスクリプション状態"))
        theme.mark_title(self.billing_title)
        layout.addWidget(self.billing_title)

        self.billing_state_label = QLabel("")
        self.billing_state_label.setWordWrap(True)
        layout.addWidget(self.billing_state_label)

        self.billing_note_label = QLabel("")
        self.billing_note_label.setWordWrap(True)
        theme.mark_note(self.billing_note_label)
        layout.addWidget(self.billing_note_label)

        billing_row = QHBoxLayout()
        self.portal_button = QPushButton(tr("サブスクリプションを管理"))
        self.portal_button.clicked.connect(self._on_portal)
        billing_row.addWidget(self.portal_button)
        billing_row.addStretch(1)
        layout.addLayout(billing_row)

        self.subscription_history_title = QLabel(tr("サブスクリプション履歴"))
        theme.mark_title(self.subscription_history_title)
        layout.addWidget(self.subscription_history_title)

        self.subscription_history_table = QTableWidget(0, 3)
        self.subscription_history_table.setHorizontalHeaderLabels(
            [tr("日時"), tr("種別"), tr("内容")])
        self.subscription_history_table.verticalHeader().setVisible(False)
        self.subscription_history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.subscription_history_table.setSelectionMode(QTableWidget.NoSelection)
        self.subscription_history_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents)
        self.subscription_history_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.subscription_history_table)

        self._show_billing(False)

        # ポイントの履歴。サブスクの履歴と見分けられるよう見出しを分ける。
        self.history_title = QLabel(tr("ポイント履歴"))
        theme.mark_title(self.history_title)
        layout.addWidget(self.history_title)

        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(
            [tr("日時"), tr("内容"), tr("増減"), tr("残高")])
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
        has_points = self._points is not None
        self.login_button.setVisible(not logged_in)
        self.logout_button.setVisible(logged_in)
        self.refresh_button.setVisible(has_points)
        self.history_table.setVisible(has_points)
        self.note_label.setVisible(not logged_in)
        self.subscribe_button.setVisible(logged_in and bool(self._subscribe_url()))

        if not logged_in:
            # 未ログインでも端末の残高と履歴は出す。サブスクの節だけ出さない
            # (匿名では加入できず、状態の照会先も無い)。
            self.status_label.setText(tr("ログインしていません (この端末のポイント)"))
            self._subscription_type = None
            self._subscription_state = None
            self._show_billing(False)

            if not has_points:
                self.balance_label.setText("")
                self.history_table.setRowCount(0)
                return

            self.balance_label.setText(tr("残高を取得しています…"))
            self._start_balance()
            self._start_history()
            return

        user = self._points.auth.user() or {}
        name = user.get("displayName") or user.get("twitchUserId") or ""
        self.status_label.setText(
            tr("ログイン中: {name}", name=name) if name else tr("ログイン中"))
        self.balance_label.setText(tr("残高を取得しています…"))
        self._start_balance()
        self._start_history()
        self._start_billing_config()
        self._start_subscription()

    # 文言を現在の言語へ貼り替える (ver8 resolve §5)。
    # 状態・残高・履歴は reload が組み立て直すが、通信を伴うため呼ばない。
    # 静的な見出しとボタンだけ入れ替え、動的な表示は次の「更新」に任せる。
    def retranslate(self):
        self.note_label.setText(
            tr("ログインしていないあいだは、この端末のポイントを消費します"
               "(残高が足りない出力には透かしが入ります)。"
               "ログインするとアカウントのポイントに切り替わります。"))
        if self.login_button.isEnabled():
            self.login_button.setText(tr("ログイン"))
        self.logout_button.setText(tr("ログアウト"))
        self.refresh_button.setText(tr("更新"))
        self.subscribe_button.setText(tr("Twitch のサブスクページを開く"))
        self.billing_title.setText(tr("サブスクリプション状態"))
        self.portal_button.setText(tr("サブスクリプションを管理"))
        self.subscription_history_title.setText(tr("サブスクリプション履歴"))
        self.subscription_history_table.setHorizontalHeaderLabels(
            [tr("日時"), tr("種別"), tr("内容")])
        self.history_title.setText(tr("ポイント履歴"))
        self.history_table.setHorizontalHeaderLabels(
            [tr("日時"), tr("内容"), tr("増減"), tr("残高")])

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
            self.balance_label.setText(
                tr("残高を取得できません (オフラインの可能性があります)"))
            # 加入しているかどうか分からない状態で「未加入」とは書かない。
            self._subscription_type = None
            self._show_billing(False)
            return

        lines = [format_balance(balance)]
        if not balance.get("unlimited"):
            costs = balance.get("costs") or {}
            clip = costs.get("clip")
            archive = costs.get("archive")
            if clip is not None and archive is not None:
                lines.append(tr("単価: クリップ {clip} pt / アーカイブ {archive} pt",
                                clip=int(clip), archive=int(archive)))
        reset = format_reset(balance)
        subscription = str(balance.get("subscriptionType") or "None")
        if subscription != "None":
            lines.append(tr("サブスク: {subscription}", subscription=subscription))
        elif reset:
            lines.append(tr("次回リセット: {reset}", reset=reset))
        self.balance_label.setText("\n".join(lines))

        # 残高の応答で初めて subscriptionType が分かるため、ここで節を作り直す。
        # 未ログイン (匿名) では節ごと出さないため、種別も持たない。
        if not self._is_logged_in():
            return

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
        kind = tr(_KIND_LABELS.get(str(item.get("kind") or ""),
                                   str(item.get("kind") or "")))
        job = tr(_JOB_LABELS.get(str(item.get("jobType") or ""), ""))
        text = tr("{kind} ({job})", kind=kind, job=job) if job else kind
        if item.get("unlimited"):
            return tr("{text} / サブスク", text=text)
        if item.get("watermarked"):
            return tr("{text} / 透かし入り", text=text)
        return text

    def _on_login(self):
        if self._points is None:
            QMessageBox.information(
                self, tr("ログインできません"),
                tr("メイン画面から起動した場合のみログインできます。"))
            return
        if self._login_worker is not None and self._login_worker.isRunning():
            return

        self.login_button.setEnabled(False)
        self.login_button.setText(tr("ブラウザで許可してください…"))
        self._login_worker = LoginWorker(self._points.auth, parent=self)
        self._login_worker.logged_in.connect(lambda _user: self.reload())
        self._login_worker.failed.connect(self._on_login_failed)
        self._login_worker.finished.connect(self._reset_login_button)
        self._login_worker.start()

    def _on_login_failed(self, message):
        QMessageBox.warning(self, tr("ログインできません"),
                            tr("ログインに失敗しました。\n{message}", message=message))

    def _reset_login_button(self):
        self.login_button.setEnabled(True)
        self.login_button.setText(tr("ログイン"))

    def _on_logout(self):
        answer = QMessageBox.question(
            self, tr("ログアウト"),
            tr("ログアウトすると、この端末のポイント (匿名) に切り替わります。"
               "ポイントの消費と透かしはそのまま続きます。\nログアウトしますか？"),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return

        try:
            self._points.auth.logout()
        except Exception as e:  # noqa: BLE001 (失敗しても画面は開いたままにする)
            _logger.exception("ログアウトに失敗")
            QMessageBox.warning(self, tr("ログアウトできません"), str(e))
        self.reload()

    def _on_subscribe(self):
        url = self._subscribe_url()
        if url:
            QDesktopServices.openUrl(QUrl(url))

    # ---- サブスクリプション状態と履歴 (ver6 resolve2 §4.4 / ③) -----------
    # 節ごとの表示 / 非表示。未ログイン・状態不明では出さない。
    def _show_billing(self, visible):
        for widget in (self.billing_title, self.billing_state_label,
                       self.billing_note_label, self.portal_button):
            widget.setVisible(bool(visible))
        # 履歴は API (GET /api/billing/subscription) が無いと 1 件も作れない。
        # 空の表を出すより、節ごと出さない方が「取れていない」と分かりやすい。
        has_history = bool(visible and self._subscription_history())
        self.subscription_history_title.setVisible(has_history)
        self.subscription_history_table.setVisible(has_history)

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

    # 加入状態と履歴の取得を始める。API 未実装でも None が届くだけで画面は壊れない。
    def _start_subscription(self):
        if self._billing is None:
            return
        if self._subscription_worker is not None and self._subscription_worker.isRunning():
            return
        self._subscription_worker = SubscriptionWorker(self._billing, parent=self)
        self._subscription_worker.loaded.connect(self._on_subscription)
        self._subscription_worker.start()

    def _on_subscription(self, state):
        self._subscription_state = state
        self._fill_subscription_history()
        self._refresh_billing()

    # API の応答から履歴の一覧を取り出す (取れていなければ空)
    def _subscription_history(self):
        state = self._subscription_state
        if not isinstance(state, dict):
            return []
        return [item for item in (state.get("history") or []) if isinstance(item, dict)]

    def _fill_subscription_history(self):
        items = self._subscription_history()
        self.subscription_history_table.setRowCount(len(items))
        for row, item in enumerate(items):
            at = parse_timestamp(item.get("at"))
            when = at.astimezone().strftime("%m/%d %H:%M") if at else ""
            event = _EVENT_LABELS.get(str(item.get("event") or ""),
                                      str(item.get("event") or ""))
            detail = str(item.get("detail") or "")
            cells = [when, self._subscription_kind(item.get("type")),
                     f"{event} ({detail})" if detail else event]
            for column, text in enumerate(cells):
                self.subscription_history_table.setItem(row, column,
                                                        QTableWidgetItem(text))
        self._fit_subscription_history()

    # 表の高さを中身に合わせて詰める。
    # QTableWidget は既定で縦へ伸びるため、1 件しか無くても画面を占有してしまう。
    def _fit_subscription_history(self):
        table = self.subscription_history_table
        rows = max(min(table.rowCount(), _SUBSCRIPTION_HISTORY_ROWS), 1)
        height = (table.horizontalHeader().height()
                  + table.verticalHeader().defaultSectionSize() * rows
                  + table.frameWidth() * 2)
        table.setMaximumHeight(height)

    # 種別の表示名。未知の値はそのまま出す (API が増やしても画面は壊れない)。
    @staticmethod
    def _subscription_kind(subscription_type):
        key = str(subscription_type or TYPE_NONE)
        if key == TYPE_TWITCH:
            return "Twitch"
        if key == TYPE_STRIPE:
            return "Stripe"
        return _SUBSCRIPTION_LABELS.get(key, key)

    # 加入種別が分かったら節を描く。種別が不明なあいだは出さない
    # (「未加入」と書いてしまうと、オフラインの加入者へ誤った表示になる)。
    #
    # 契約情報 (config) は「管理ページが使えるか」の判断にだけ使う。
    # config が取れなくても状態そのものは出す。ここは登録の導線ではないため、
    # 受付停止中でも加入者に状態を見せないと解約できなくなる。
    def _refresh_billing(self):
        subscription_type = self._subscription_type
        if subscription_type is None:
            self._show_billing(False)
            return

        self._show_billing(True)
        self.billing_state_label.setText("\n".join(self._state_lines(subscription_type)))

        config = self._billing_config
        manageable = config is not None and config.manageable
        self.portal_button.setVisible(subscription_type == TYPE_STRIPE and manageable)

        if subscription_type == TYPE_STRIPE:
            self.billing_note_label.setText(
                tr("解約・カードの変更・請求書の確認は"
                   "「サブスクリプションを管理」から行えます。"))
        elif subscription_type == TYPE_TWITCH:
            self.billing_note_label.setText(
                tr("Twitch のサブスクリプションが切れると、ポイント制に戻ります"
                   "(残高が足りない出力には透かしが入ります)。"))
        else:
            self.billing_note_label.setText(
                tr("登録はメイン画面の「サブスクリプション」から行えます。"))

    # 状態の表示行を組み立てる (種別 / 状態 / 次回更新日)
    def _state_lines(self, subscription_type):
        state = self._subscription_state if isinstance(self._subscription_state, dict) else {}

        kind = tr(_SUBSCRIPTION_LABELS.get(subscription_type, subscription_type))
        channel = str(state.get("twitchChannel") or "").strip()
        if subscription_type == TYPE_TWITCH and channel:
            kind = tr("{kind} ({channel})", kind=kind, channel=channel)
        lines = [tr("種別: {kind}", kind=kind)]

        status = str(state.get("status") or "")
        if status:
            lines.append(tr("状態: {status}",
                            status=tr(_STATUS_LABELS.get(status, status))))

        period_end = parse_timestamp(state.get("currentPeriodEnd"))
        if period_end is not None:
            label = tr("終了") if state.get("cancelAtPeriodEnd") else tr("次回更新")
            lines.append(tr("{label}: {date}", label=label,
                            date=period_end.astimezone().strftime("%Y/%m/%d")))
        return lines

    # 管理ページ (解約・カード変更・請求書) を開く。
    def _on_portal(self):
        if self._billing is None:
            return
        if self._billing_worker is not None and self._billing_worker.isRunning():
            return

        self.portal_button.setEnabled(False)
        self.billing_note_label.setText(tr("ブラウザで手続きしてください…"))

        self._billing_worker = BillingUrlWorker(self._billing, "portal", parent=self)
        self._billing_worker.ready.connect(self._on_portal_url)
        self._billing_worker.failed.connect(self._on_billing_failed)
        self._billing_worker.finished.connect(self._reset_billing_buttons)
        self._billing_worker.start()

    # 管理ページを開く。解約はブラウザ側で完結するため、反映は待たない
    # (解約しても期間の末日までは unlimited のままで、画面は変わらない)。
    def _on_portal_url(self, url):
        QDesktopServices.openUrl(QUrl(url))
        self.billing_note_label.setText(
            tr("ブラウザで手続きしてください。変更後は「更新」で反映されます。"))

    def _on_billing_failed(self, message):
        self.billing_note_label.setText("")
        QMessageBox.warning(
            self, tr("手続きを開始できません"),
            tr("サブスクリプションの手続きを開始できませんでした。\n{message}",
               message=message))
        self.reload()

    def _reset_billing_buttons(self):
        self.portal_button.setEnabled(True)
