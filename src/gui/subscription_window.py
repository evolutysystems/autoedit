# サブスクリプション画面 (ver6 resolve2 §2.2 / §4.1)
#
# メイン画面の「サブスクリプション」ボタンから開く独立したウィンドウ。
# ここは**加入するための画面**で、置くのは 2 つのボタンだけである。
#   ① Twitch でログイン … 既存の Stretheus ログイン (Twitch の認可コードフロー)。
#      ログインしたアカウントが Twitch のサブスクリプション特典を受けている場合は
#      購入ボタンを押せなくする (request2 ①)。
#   ② 購入             … 既存の Stripe Checkout をそのまま走らせる (request2 ②)。
#
# 状態の確認・履歴・解約は設定画面のアカウントタブが受け持つ (ver6 resolve2 §2.3)。
#
# モーダルにしない。ログインの認可待ちが最大 180 秒、支払いの反映待ちが最大 180 秒あり、
# モーダルで待つと「アプリが固まった」と見える (ver6 resolve §4.8)。
from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..services import billing as billing_module
from ..services.billing import BillingService
from ..services.stretheus_auth import login_in_progress
from ..utils.logger import get_logger
from . import theme
from .billing_workers import ActivationWorker, BillingConfigWorker, BillingUrlWorker
from .points_indicator import BalanceWorker, LoginWorker

_logger = get_logger(__name__)

# ウィンドウの寸法 (settings_window.WINDOW_WIDTH と同じ扱いでモジュール定数に置く)
WINDOW_WIDTH = 420
WINDOW_HEIGHT = 320

# 購入ボタンを押せない理由ごとの注記。コードは billing.py が決め、文言はここが持つ。
_BLOCK_MESSAGES = {
    billing_module.BLOCK_NOT_LOGGED_IN:
        "Twitch でログインすると購入できます。",
    billing_module.BLOCK_UNKNOWN:
        "加入状態を確認しています…",
    billing_module.BLOCK_UNAVAILABLE:
        "現在サブスクリプションのお申し込みを受け付けていません。"
        "しばらくしてからお試しください。",
    billing_module.BLOCK_TWITCH:
        "Twitch のサブスクリプションで特典が適用されています。購入は不要です。"
        "Twitch のサブスクリプションが切れると、この画面から購入できるようになります。",
    billing_module.BLOCK_STRIPE:
        "すでにサブスクリプションに加入しています。"
        "解約・カードの変更は設定画面の「アカウント」から行えます。",
}

# 購入できるときの案内
_PURCHASE_NOTE = "お支払いはブラウザ (Stripe) で行います。"

# 加入種別ごとの状態表示
_STATE_LABELS = {
    billing_module.TYPE_STRIPE: "加入中 (Stripe)",
    billing_module.TYPE_TWITCH: "Twitch サブスクリプションの特典で無制限",
    billing_module.TYPE_NONE: "未加入",
}


class SubscriptionWindow(QWidget):

    # 加入が確認できた (メイン画面のボタンへ反映させるため)
    subscription_changed = Signal()

    def __init__(self, points, settings=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("サブスクリプション")
        self._points = points
        self._settings = settings or {}
        self._balance_worker = None
        self._login_worker = None
        self._config_worker = None
        self._url_worker = None
        self._activation_worker = None
        # 契約情報と加入種別は別々のタイミングで届く。両方そろってから可否を決める。
        self._billing_config = None
        self._subscription_type = None
        # 手続き中 (ブラウザでの支払い待ち) はボタンを押せなくする
        self._in_progress = False
        # points が無い (CLI / テスト) 場合は導線を出さない
        self._billing = BillingService(points, self._settings) if points is not None else None

        self._build_ui()
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        theme.install_window_background(self)
        self.reload()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        title = QLabel("サブスクリプション")
        theme.mark_title(title)
        layout.addWidget(title)

        # 価格はサーバーから配られる (配布済みの exe へ焼き込まない / ver6 resolve §3.1)
        self.price_label = QLabel("")
        layout.addWidget(self.price_label)

        self.description_label = QLabel(
            "ポイントを消費せず、透かしの入らない出力を回数無制限で行えます。")
        self.description_label.setWordWrap(True)
        theme.mark_note(self.description_label)
        layout.addWidget(self.description_label)

        self.state_label = QLabel("")
        layout.addWidget(self.state_label)

        layout.addSpacing(theme.BUTTON_ICON_PX)

        self.twitch_button = QPushButton("Twitch でログイン")
        theme.mark_twitch_button(self.twitch_button)
        self.twitch_button.clicked.connect(self._on_twitch_login)
        layout.addWidget(self.twitch_button)

        self.purchase_button = QPushButton("購入")
        theme.mark_primary(self.purchase_button, solid=True)
        self.purchase_button.clicked.connect(self._on_purchase)
        layout.addWidget(self.purchase_button)

        self.note_label = QLabel("")
        self.note_label.setWordWrap(True)
        theme.mark_note(self.note_label)
        layout.addWidget(self.note_label)

        layout.addStretch(1)

    # ---- 状態の取り直し ---------------------------------------------------
    # 開いたとき・ログインしたとき・支払いが反映されたときに呼ぶ。
    def reload(self):
        # 取り直しの間は「不明」に戻す。前回の値で購入ボタンを開けたままにしない。
        self._billing_config = None
        self._subscription_type = None
        self._apply_state()

        if self._billing is None or not self._is_logged_in():
            return

        self._start_balance()
        self._start_config()

    def _is_logged_in(self):
        return bool(self._points is not None and self._points.auth.is_logged_in())

    def _start_balance(self):
        if self._balance_worker is not None and self._balance_worker.isRunning():
            return
        self._balance_worker = BalanceWorker(self._points, force=True, parent=self)
        self._balance_worker.loaded.connect(self._on_balance)
        self._balance_worker.start()

    def _on_balance(self, balance):
        balance = balance or self._points.last_balance()
        # 残高が取れないあいだは種別を確定させない (オフラインで購入を開かない)。
        if balance:
            self._subscription_type = str(
                balance.get("subscriptionType") or billing_module.TYPE_NONE)
        self._apply_state()

    def _start_config(self):
        if self._config_worker is not None and self._config_worker.isRunning():
            return
        self._config_worker = BillingConfigWorker(self._billing, parent=self)
        self._config_worker.loaded.connect(self._on_config)
        self._config_worker.start()

    def _on_config(self, config):
        self._billing_config = config
        self._apply_state()

    # ---- 表示 -------------------------------------------------------------
    # ボタンの有効 / 無効と文言を、今分かっている情報だけから決める。
    # 契約情報と加入種別の**両方**がそろうまで購入ボタンは開かない
    # (片方だけで描くと加入中の人に購入ボタンが一瞬押せて見える / ver6 resolve §4.2)。
    def _apply_state(self):
        logged_in = self._is_logged_in()
        config = self._billing_config

        # 価格。取れていなければ空にしておく (クライアントに価格を持たせない)。
        self.price_label.setText(config.price_label if config is not None else "")

        # Twitch のログインボタン。ログイン済みなら誰でログインしているかを出す。
        if logged_in:
            user = self._points.auth.user() or {}
            name = user.get("displayName") or user.get("twitchUserId") or ""
            self.twitch_button.setText(
                f"Twitch ログイン済み: {name}" if name else "Twitch ログイン済み")
            self.twitch_button.setEnabled(False)
        else:
            self.twitch_button.setText("Twitch でログイン")
            # 別のログインが進行中のあいだは押させない。認可コードの受け口 (localhost)
            # を同じポートで開こうとして失敗するため (ver6 resolve §6.3)。
            self.twitch_button.setEnabled(
                self._billing is not None and not login_in_progress()
                and not self._in_progress)

        self.state_label.setText(self._state_text(logged_in))

        # 購入ボタン。押せない理由は billing.py の純関数が決める (§4.6)。
        reason = billing_module.purchase_blocked_reason(
            config, self._subscription_type, logged_in)
        if self._in_progress:
            # 手続き中は理由の表示を上書きしない (進行中の案内が出ている)。
            self.purchase_button.setEnabled(False)
            return

        self.purchase_button.setEnabled(not reason)
        self.note_label.setText(_BLOCK_MESSAGES.get(reason, "") if reason
                                else _PURCHASE_NOTE)

    def _state_text(self, logged_in):
        if not logged_in:
            return "状態: 未ログイン"
        if self._subscription_type is None:
            return "状態: 確認中…"
        return "状態: " + _STATE_LABELS.get(self._subscription_type,
                                            self._subscription_type)

    # ---- ① Twitch ログイン ------------------------------------------------
    # 既存の Stretheus ログイン (Twitch の認可コードフロー) をそのまま使う。
    # 新しいログイン機構は作らない (ver6 resolve2 §1.1)。
    def _on_twitch_login(self):
        if self._billing is None:
            QMessageBox.information(
                self, "ログインできません",
                "メイン画面から起動した場合のみログインできます。")
            return
        if self._login_worker is not None and self._login_worker.isRunning():
            return

        self.twitch_button.setEnabled(False)
        self.twitch_button.setText("ブラウザで許可してください…")
        self.note_label.setText("ブラウザで Twitch の認可を行ってください。")
        self._login_worker = LoginWorker(self._points.auth, parent=self)
        self._login_worker.logged_in.connect(self._on_logged_in)
        self._login_worker.failed.connect(self._on_login_failed)
        self._login_worker.start()

    def _on_logged_in(self, user):
        _logger.info("サブスクリプション画面からログインしました: %s",
                     (user or {}).get("displayName", ""))
        # 加入状態は残高の応答で初めて分かる。取り直してから可否を決める。
        self.reload()
        # ログインで Twitch サブが判明する場合があるためメイン画面へも伝える。
        self.subscription_changed.emit()

    def _on_login_failed(self, message):
        self._apply_state()
        QMessageBox.warning(self, "ログインできません",
                            f"ログインに失敗しました。\n{message}")

    # ---- ② 購入 -----------------------------------------------------------
    def _on_purchase(self):
        if self._billing is None:
            return
        if self._url_worker is not None and self._url_worker.isRunning():
            return

        self._in_progress = True
        self.purchase_button.setEnabled(False)
        self.note_label.setText("ブラウザで手続きしてください…")

        self._url_worker = BillingUrlWorker(self._billing, "checkout", parent=self)
        self._url_worker.ready.connect(self._on_checkout_url)
        self._url_worker.failed.connect(self._on_checkout_failed)
        self._url_worker.start()

    def _on_checkout_url(self, url):
        QDesktopServices.openUrl(QUrl(url))
        self.note_label.setText(
            "ブラウザでお支払いを完了してください。反映を待っています…")
        self._start_activation()

    def _on_checkout_failed(self, message):
        self._in_progress = False
        QMessageBox.warning(
            self, "手続きを開始できません",
            f"サブスクリプションの手続きを開始できませんでした。\n{message}")
        # 409 (すでに加入) もここへ来る。状態を取り直して表示を作り直す。
        self.reload()

    # 支払いがサーバーへ反映されるのを待つ。画面は操作可能なままにする。
    def _start_activation(self):
        if self._activation_worker is not None and self._activation_worker.isRunning():
            return
        self._activation_worker = ActivationWorker(self._billing, parent=self)
        self._activation_worker.done.connect(self._on_activation)
        self._activation_worker.start()

    def _on_activation(self, activated):
        self._in_progress = False
        if activated:
            self.reload()
            self.subscription_changed.emit()
            return
        # 支払いを取りやめた場合もここへ来る。失敗とは書かない。
        self._apply_state()
        self.note_label.setText(
            "お支払いの反映を確認できませんでした。"
            "手続きが完了している場合は、しばらくしてからこの画面を開き直してください。")

    # 画面を閉じるときの後始末。
    #
    # 反映待ちは最大 180 秒動くため、中断を要求してから待ち合わせる。
    # 残高・契約情報・URL の取得は HTTP のタイムアウト (既定 15 秒) で必ず終わるため、
    # そのぶんだけ待つ。走ったままのスレッドを親ごと破棄すると Qt が異常終了する。
    #
    # ログイン (LoginWorker) は利用者がブラウザで許可するまで終われないため待たない。
    # 認可待ちのあいだに閉じても、ログイン自体は完了し次回の表示に反映される。
    def closeEvent(self, event):
        if self._activation_worker is not None and self._activation_worker.isRunning():
            self._activation_worker.requestInterruption()
        for worker in (self._activation_worker, self._url_worker,
                       self._config_worker, self._balance_worker):
            if worker is not None and worker.isRunning():
                worker.wait(5000)
        super().closeEvent(event)
