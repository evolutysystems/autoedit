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

from ..i18n import tr
from ..services import billing as billing_module
from ..services.billing import BillingService
from ..services.stretheus_auth import (
    PROVIDER_GOOGLE,
    PROVIDER_TWITCH,
    login_in_progress,
)
from ..utils.logger import get_logger
from . import theme
from .billing_workers import (
    BillingConfigWorker,
    BillingUrlWorker,
    GoogleAvailableWorker,
)
from .points_indicator import BalanceWorker, LoginWorker

_logger = get_logger(__name__)

# ウィンドウの寸法 (settings_window.WINDOW_WIDTH と同じ扱いでモジュール定数に置く)
WINDOW_WIDTH = 420
WINDOW_HEIGHT = 320

# 画面の段 (ver9 resolve18 §0 / §2)。
# purchase_blocked_reason() が返すコードから決める。**真理値表そのものは変えない**
# (ver6 resolve2 §6 / §4.6)。変えるのは「コードをどう描くか」だけである。
_S_LOGIN = "login"              # S0 未ログイン → Twitch / Google の 2 ボタン
_S_CHECKING = "checking"        # S1 確認中
_S_TWITCH_SUB = "twitch_sub"    # S2 Twitch サブ → 閉じてメイン画面へ戻る
_S_SUBSCRIBED = "subscribed"    # S3 Stripe 加入中
_S_UNAVAILABLE = "unavailable"  # S4 受付停止
_S_PURCHASE = "purchase"        # S5 購入可能

_STAGES = {
    billing_module.BLOCK_NOT_LOGGED_IN: _S_LOGIN,
    billing_module.BLOCK_UNKNOWN: _S_CHECKING,
    billing_module.BLOCK_TWITCH: _S_TWITCH_SUB,
    billing_module.BLOCK_STRIPE: _S_SUBSCRIBED,
    billing_module.BLOCK_UNAVAILABLE: _S_UNAVAILABLE,
    "": _S_PURCHASE,
}

# 段ごとの注記。コードは billing.py が決め、文言はここが持つ。
_STAGE_MESSAGES = {
    _S_LOGIN: "ログインしてからお申し込みいただけます。",
    _S_CHECKING: "加入状態を確認しています…",
    _S_UNAVAILABLE:
        "現在サブスクリプションのお申し込みを受け付けていません。"
        "しばらくしてからお試しください。",
    _S_SUBSCRIBED:
        "すでにサブスクリプションに加入しています。"
        "解約・カードの変更は設定画面の「アカウント」から行えます。",
    _S_PURCHASE: "お支払いはブラウザ (Stripe) で行います。",
}

# 未ログインのときに添える誘導 (ver9 resolve18 §2)。
# Twitch サブの特典を持つ人が Google を選ぶと、別の利用者になって特典を受けられない。
_LOGIN_HINT = ("Twitch のサブスクリプションをご利用中の方は、"
               "Twitch でログインすると特典がそのまま適用されます。")

# 加入種別ごとの状態表示
_STATE_LABELS = {
    billing_module.TYPE_STRIPE: "加入中 (Stripe)",
    billing_module.TYPE_TWITCH: "Twitch サブスクリプションの特典で無制限",
    billing_module.TYPE_NONE: "未加入",
}


class SubscriptionWindow(QWidget):

    # 加入が確認できた (メイン画面のボタンへ反映させるため)
    subscription_changed = Signal()

    # ブラウザでの支払いを開始した (ver9 resolve17 §4.1)。
    # 反映待ちはメイン画面が引き取る。この画面が閉じられても待ち続けるため。
    checkout_started = Signal()

    # この画面が閉じられた (ver9 resolve17 §1.2 / §4.1)。
    # destroyed は WA_DeleteOnClose を設定していないため閉じても飛ばない。
    # 「閉じた時点で取り直す」にはこちらを使う。
    closed = Signal()

    def __init__(self, points, settings=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("サブスクリプション"))
        self._points = points
        self._settings = settings or {}
        self._balance_worker = None
        self._login_worker = None
        self._config_worker = None
        self._url_worker = None
        self._google_worker = None
        # 契約情報と加入種別は別々のタイミングで届く。両方そろってから可否を決める。
        self._billing_config = None
        self._subscription_type = None
        # Google ログインを提供できる構成か (ver9 resolve18 §3)。
        # 取得前は False として扱い、押せるのに失敗するボタンを出さない。
        self._google_available = False
        # 手続き中 (ブラウザでの支払い待ち) はボタンを押せなくする
        self._in_progress = False
        # S2 で閉じる処理の再入防止 (ver9 resolve18 §4.4)。
        # _apply_state は残高と契約情報が別々に届くたび呼ばれるため、
        # S2 の判定は 2 回以上起こりうる。
        self._closing_for_subscription = False
        # points が無い (CLI / テスト) 場合は導線を出さない
        self._billing = BillingService(points, self._settings) if points is not None else None

        self._build_ui()
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        theme.install_window_background(self)
        self.reload()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.title_label = QLabel(tr("サブスクリプション"))
        theme.mark_title(self.title_label)
        layout.addWidget(self.title_label)

        # 価格はサーバーから配られる (配布済みの exe へ焼き込まない / ver6 resolve §3.1)
        self.price_label = QLabel("")
        layout.addWidget(self.price_label)

        self.description_label = QLabel(
            tr("ポイントを消費せず、透かしの入らない出力を回数無制限で行えます。"))
        self.description_label.setWordWrap(True)
        theme.mark_note(self.description_label)
        layout.addWidget(self.description_label)

        self.state_label = QLabel("")
        layout.addWidget(self.state_label)

        layout.addSpacing(theme.BUTTON_ICON_PX)

        self.twitch_button = QPushButton(tr("Twitch でログイン"))
        theme.mark_twitch_button(self.twitch_button)
        self.twitch_button.clicked.connect(self._on_twitch_login)
        layout.addWidget(self.twitch_button)

        # Google ログイン (ver9 resolve18 §2)。Twitch の下に置く。
        # Twitch サブの特典を持つ人が先に Google を選ばないようにするため。
        self.google_button = QPushButton(tr("Google でログイン"))
        self.google_button.clicked.connect(self._on_google_login)
        layout.addWidget(self.google_button)

        self.purchase_button = QPushButton(tr("購入"))
        theme.mark_primary(self.purchase_button, solid=True)
        self.purchase_button.clicked.connect(self._on_purchase)
        layout.addWidget(self.purchase_button)

        # 反映を自分で確かめる手段 (ver9 resolve17 §4.4)。
        # 待たされること自体より、確かめる手段が無いことが不安を生む。
        self.refresh_button = QPushButton(tr("更新"))
        self.refresh_button.clicked.connect(self.reload)
        layout.addWidget(self.refresh_button)

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

        if self._billing is None:
            return

        if not self._is_logged_in():
            # 未ログインのうちに Google を出せるかを確かめておく (§3)。
            # ログイン後は使わないため、この段でだけ取得する。
            self._start_google_check()
            return

        self._start_balance()
        self._start_config()

    def _start_google_check(self):
        if self._google_worker is not None and self._google_worker.isRunning():
            return
        self._google_worker = GoogleAvailableWorker(self._billing, parent=self)
        self._google_worker.loaded.connect(self._on_google_available)
        self._google_worker.start()

    def _on_google_available(self, available):
        self._google_available = bool(available)
        self._apply_state()

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
    # 段を決めてから描く (ver9 resolve18 §4.3)。
    #
    # 段の決定は purchase_blocked_reason() の戻り値だけに依存する。
    # 契約情報と加入種別の**両方**がそろうまで購入ボタンは開かない
    # (片方だけで描くと加入中の人に購入ボタンが一瞬押せて見える / ver6 resolve §4.2)。
    def _apply_state(self):
        logged_in = self._is_logged_in()
        config = self._billing_config

        reason = billing_module.purchase_blocked_reason(
            config, self._subscription_type, logged_in)
        stage = _STAGES.get(reason, _S_CHECKING)

        # 手続き中 (ブラウザでの支払い待ち) は段を動かさない。
        # 進行中の案内を出したまま、押せない購入ボタンを見せ続ける。
        if self._in_progress:
            stage = _S_PURCHASE

        # Twitch サブで特典が適用されている場合、この画面に用は無い (要望 ②)。
        if stage == _S_TWITCH_SUB:
            self._close_for_twitch_subscription()
            return

        self._show_stage(stage, logged_in, config)

    # 段ごとに出すものを切り替える (ver9 resolve18 §4.3 の表)。
    def _show_stage(self, stage, logged_in, config):
        # 価格。取れていなければ空にしておく (クライアントに価格を持たせない)。
        self.price_label.setText(config.price_label if config is not None else "")

        self.price_label.setVisible(True)
        self.description_label.setVisible(True)
        self.state_label.setVisible(stage != _S_LOGIN)
        self.refresh_button.setVisible(stage != _S_LOGIN)

        # ログインボタンは未ログインの段だけに出す。
        show_login = stage == _S_LOGIN
        self.twitch_button.setVisible(show_login)
        # Google は API が提供できる構成のときだけ (§3)。
        self.google_button.setVisible(show_login and self._google_available)

        if show_login:
            self.twitch_button.setText(tr("Twitch でログイン"))
            self.google_button.setText(tr("Google でログイン"))
            # 別のログインが進行中のあいだは押させない。認可コードの受け口 (localhost)
            # を同じポートで開こうとして失敗するため (ver6 resolve §6.3)。
            can_login = (self._billing is not None and not login_in_progress()
                         and not self._in_progress)
            self.twitch_button.setEnabled(can_login)
            self.google_button.setEnabled(can_login)

        # 購入ボタンは購入可能な段だけ。
        self.purchase_button.setVisible(stage == _S_PURCHASE)
        self.purchase_button.setEnabled(stage == _S_PURCHASE and not self._in_progress)

        self.state_label.setText(self._state_text(logged_in))

        # 手続き中は注記を上書きしない (進行中の案内が出ている)。
        if self._in_progress:
            return

        note = tr(_STAGE_MESSAGES.get(stage, ""))
        if stage == _S_LOGIN:
            note = tr("{note}\n{hint}", note=note, hint=tr(_LOGIN_HINT))
        self.note_label.setText(note)

    # 文言を現在の言語へ貼り替える (ver8 resolve §5)。
    # 状態・注記・ボタンの可否は _apply_state が一手に組み立てるため、
    # 静的なラベルだけ入れ替えてそれを呼び直す (通信は起きない)。
    def retranslate(self):
        self.setWindowTitle(tr("サブスクリプション"))
        self.title_label.setText(tr("サブスクリプション"))
        self.description_label.setText(
            tr("ポイントを消費せず、透かしの入らない出力を回数無制限で行えます。"))
        self.purchase_button.setText(tr("購入"))
        self.refresh_button.setText(tr("更新"))
        self.twitch_button.setText(tr("Twitch でログイン"))
        self.google_button.setText(tr("Google でログイン"))
        self._apply_state()

    def _state_text(self, logged_in):
        if not logged_in:
            return tr("状態: 未ログイン")

        if self._subscription_type is None:
            state = tr("状態: 確認中…")
        else:
            state = tr("状態: {state}", state=tr(_STATE_LABELS.get(
                self._subscription_type, self._subscription_type)))

        # 誰でログインしているかを添える (ver9 resolve18 §4.3)。
        # ログイン済みの段ではログインボタンを隠すため、ここに出さないと
        # どのアカウントで入っているのかが画面から分からなくなる。
        account = self._account_text()
        return tr("{state}\n{account}", state=state, account=account) if account else state

    # ログイン済みのアカウント表示。未ログインなら空文字。
    def _account_text(self):
        if self._points is None or not self._is_logged_in():
            return ""

        user = self._points.auth.user() or {}
        name = user.get("displayName") or user.get("twitchUserId") or ""
        google = self._points.auth.provider() == PROVIDER_GOOGLE

        if name:
            return (tr("Google ログイン済み: {name}", name=name) if google
                    else tr("Twitch ログイン済み: {name}", name=name))
        return tr("Google ログイン済み") if google else tr("Twitch ログイン済み")

    # ---- ① ログイン (Twitch / Google) --------------------------------------
    # 既存の Stretheus ログイン (認可コードフロー) をそのまま使い、
    # プロバイダだけを渡し分ける。新しいログイン機構は作らない
    # (ver6 resolve2 §1.1 / ver9 resolve18 §4.1)。
    def _on_twitch_login(self):
        self._start_login(PROVIDER_TWITCH, self.twitch_button,
                          tr("ブラウザで Twitch の認可を行ってください。"))

    def _on_google_login(self):
        self._start_login(PROVIDER_GOOGLE, self.google_button,
                          tr("ブラウザで Google の認可を行ってください。"))

    def _start_login(self, provider, button, note):
        if self._billing is None:
            QMessageBox.information(
                self, tr("ログインできません"),
                tr("メイン画面から起動した場合のみログインできます。"))
            return
        if self._login_worker is not None and self._login_worker.isRunning():
            return

        # 受け口のポートを取り合わないよう、押した側だけでなく両方を止める
        # (ver6 resolve §6.3)。
        self.twitch_button.setEnabled(False)
        self.google_button.setEnabled(False)
        button.setText(tr("ブラウザで許可してください…"))
        self.note_label.setText(note)

        self._login_worker = LoginWorker(self._points.auth, parent=self, provider=provider)
        self._login_worker.logged_in.connect(self._on_logged_in)
        self._login_worker.failed.connect(self._on_login_failed)
        self._login_worker.start()

    def _on_logged_in(self, user):
        _logger.info("サブスクリプション画面からログインしました: %s",
                     (user or {}).get("displayName", ""))
        # 加入状態は残高の応答で初めて分かる。取り直してから段を決める。
        # Twitch サブが判明すれば _apply_state が S2 へ進み、この画面は閉じる (要望 ②)。
        self.reload()
        # ログインで Twitch サブが判明する場合があるためメイン画面へも伝える。
        self.subscription_changed.emit()

    def _on_login_failed(self, message):
        self._apply_state()
        QMessageBox.warning(self, tr("ログインできません"),
                            tr("ログインに失敗しました。\n{message}", message=message))

    # ---- S2: Twitch サブで特典が適用されている (要望 ②) --------------------
    # この画面に用が無いので閉じ、メイン画面へ戻る。
    # メイン画面のサブスクリプションボタンが無効になり、紫のチェックマークが付く
    # (既存表示 / ver6 resolve2 §4.2)。
    def _close_for_twitch_subscription(self):
        if self._closing_for_subscription:
            return
        self._closing_for_subscription = True

        # **先にメイン画面へ知らせる。** close() が先だと closed だけが飛び、
        # チェックマークを付ける経路 (subscription_changed) と取得が二重になる
        # (ver9 resolve18 §4.4)。
        self.subscription_changed.emit()
        self.close()

    # ---- ② 購入 -----------------------------------------------------------
    def _on_purchase(self):
        if self._billing is None:
            return
        if self._url_worker is not None and self._url_worker.isRunning():
            return

        self._in_progress = True
        self.purchase_button.setEnabled(False)
        self.note_label.setText(tr("ブラウザで手続きしてください…"))

        self._url_worker = BillingUrlWorker(self._billing, "checkout", parent=self)
        self._url_worker.ready.connect(self._on_checkout_url)
        self._url_worker.failed.connect(self._on_checkout_failed)
        self._url_worker.start()

    def _on_checkout_url(self, url):
        QDesktopServices.openUrl(QUrl(url))
        self.note_label.setText(
            tr("ブラウザでお支払いを完了してください。反映を待っています…"))
        # 反映待ちはメイン画面が行う (ver9 resolve17 §4.2)。
        # ここで持つと、この画面を閉じた時点で待ち合わせが消えてしまう。
        self.checkout_started.emit()

    def _on_checkout_failed(self, message):
        self._in_progress = False
        QMessageBox.warning(
            self, tr("手続きを開始できません"),
            tr("サブスクリプションの手続きを開始できませんでした。\n{message}",
               message=message))
        # 409 (すでに加入) もここへ来る。状態を取り直して表示を作り直す。
        self.reload()

    # 反映待ちの結果をメイン画面から受け取る (ver9 resolve17 §4.2)。
    #
    # 待ち合わせ自体はメイン画面が持つ。この画面が閉じられていれば呼ばれない
    # (閉じたあとの表示は無いため、結果を届ける先も無い)。
    def apply_activation(self, activated):
        self._in_progress = False
        if activated:
            self.reload()
            return
        # 支払いを取りやめた場合もここへ来る。失敗とは書かない。
        self._apply_state()
        self.note_label.setText(
            tr("お支払いの反映をまだ確認できていません。"
               "お支払いが完了している場合は、「更新」を押すと確認できます。"))

    # 画面を閉じるときの後始末。
    #
    # 残高・契約情報・URL の取得は HTTP のタイムアウト (既定 15 秒) で必ず終わるため、
    # そのぶんだけ待つ。走ったままのスレッドを親ごと破棄すると Qt が異常終了する。
    #
    # **反映待ちはここで中断しない** (ver9 resolve17 §1.3 / §4.2)。
    # メイン画面が持つため、閉じても支払いの反映は続く。以前はここで
    # requestInterruption() を呼んでいたため、「ブラウザで支払い、アプリへ戻り、
    # 画面を閉じる」という自然な操作が反映の仕組みを自分で壊していた。
    #
    # ログイン (LoginWorker) は利用者がブラウザで許可するまで終われないため待たない。
    # 認可待ちのあいだに閉じても、ログイン自体は完了し次回の表示に反映される。
    def closeEvent(self, event):
        # destroyed は閉じても飛ばないため、閉じたことはここで知らせる (§1.2)。
        self.closed.emit()
        for worker in (self._url_worker, self._config_worker,
                       self._balance_worker, self._google_worker):
            if worker is not None and worker.isRunning():
                worker.wait(5000)
        super().closeEvent(event)
