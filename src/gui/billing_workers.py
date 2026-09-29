# サブスクリプション (課金) まわりのワーカースレッド (ver6 resolve2 §4.1)
#
# もともと account_tab.py (設定画面のタブ) に置いていたが、メイン画面から開く
# サブスクリプション画面 (subscription_window.py) も同じものを使う。
# 画面の部品が設定画面のタブへ依存しないよう、points_indicator.py と同じ位置づけで
# ここへ移した。account_tab.py と subscription_window.py の双方がここから読む。
#
# 共通の約束:
#   ・HTTP はすべてここ (ワーカースレッド) で行う。GUI スレッドから BillingService を
#     直接呼ぶと、API へ到達できないときにタイムアウト (既定 15 秒) ぶん画面が固まる。
#   ・「開いたときに勝手に走る」取得 (config / subscription) は失敗を握って空を返す。
#     課金まわりの失敗で画面が開かなくなることを避ける (ver6 resolve §4.3)。
#   ・「利用者が押した」操作 (URL の取得) だけは失敗の理由を画面へ通知する (§4.2)。
from PySide6.QtCore import QThread, Signal

from ..utils.logger import get_logger

_logger = get_logger(__name__)


# サブスクリプションの契約情報 (価格・受付可否) の取得。
# config() は HTTP を伴うため、GUI スレッドから直接呼ばない。
class BillingConfigWorker(QThread):

    loaded = Signal(object)     # BillingConfig (取れなければ None)

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


# Stripe の決済ページ / カスタマーポータルの URL 取得。
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


# 支払いがサーバーへ反映される (unlimited が立つ) のを待つ (ver6 resolve §4.5)。
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


# 加入状態と履歴の取得 (ver6 resolve2 §4.4 / ③)。
# API が未実装 (404) でもオフラインでも None が届き、画面は履歴の表を出さない。
class SubscriptionWorker(QThread):

    loaded = Signal(object)     # {type, status, currentPeriodEnd, history[]} / None

    def __init__(self, billing, parent=None):
        super().__init__(parent)
        self._billing = billing

    def run(self):
        state = None
        try:
            state = self._billing.subscription()
        except Exception:  # noqa: BLE001 (状態が出せなくても画面は開いたままにする)
            _logger.exception("サブスクリプションの状態の取得に失敗")
        self.loaded.emit(state)
