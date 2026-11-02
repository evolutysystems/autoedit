# 支払いの反映が画面へ届く経路 (ver9 resolve17) の単体テスト
# 実行: python -m unittest discover -s tests
#
# 背景 (2026-11-02 の報告):
#   Stripe で支払って加入したのに、メイン画面のサブスクリプションボタンが
#   押せるままで、表示も即時には変わらなかった。
#
#   原因は 2 つ。
#   ① main_window が SubscriptionWindow.destroyed に「閉じたら取り直す」を
#      つないでいたが、この画面は WA_DeleteOnClose を設定しておらず
#      呼び出し側が参照を保持するため、destroyed は閉じた時点では飛ばない。
#   ② 反映待ち (ActivationWorker) を画面が持ち、closeEvent が
#      requestInterruption() を呼んでいたため、画面を閉じると待ち合わせが消えた。
#
# 重点:
#   ・閉じたときに closed が飛ぶこと (① の修正。destroyed に頼らない)
#   ・購入の開始で checkout_started が飛ぶこと (② 待ち合わせを外へ渡す)
#   ・closeEvent が反映待ちを中断しないこと (② 画面側に待ち合わせを持たない)
#   ・apply_activation が手続き中フラグを戻すこと (閉じずに待った場合の表示)
# ネットワークへは出ない。
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.gui.subscription_window import SubscriptionWindow
from src.services.billing import BillingConfig


def _app():
    return QApplication.instance() or QApplication([])


class _FakeClient:

    def get(self, path, authenticated=True):
        return {}


class _FakeAuth:

    def __init__(self, logged_in=True):
        self._logged_in = logged_in
        self.client = _FakeClient()

    def is_logged_in(self):
        return self._logged_in

    def has_session(self):
        return True

    def is_anonymous(self):
        return not self._logged_in

    def ensure_session(self):
        return True

    def user(self):
        return {"displayName": "テスト配信者"}

    # ver9 resolve18 §4.1 でログインのプロバイダを持つようになった
    def provider(self):
        return "twitch" if self._logged_in else ""


class _FakePoints:

    def __init__(self, balance=None):
        self.auth = _FakeAuth()
        self._balance = balance

    def flush_pending(self):
        pass

    def balance(self, force=False):
        return self._balance

    def last_balance(self):
        return self._balance


class SubscriptionWindowReflectTest(unittest.TestCase):

    def setUp(self):
        _app()
        self._windows = []

    def tearDown(self):
        for window in self._windows:
            window.deleteLater()

    def _window(self, subscription_type="None"):
        points = _FakePoints(balance={
            "available": 0, "balance": 0, "unlimited": subscription_type != "None",
            "subscriptionType": subscription_type})
        window = SubscriptionWindow(points, {})
        self._windows.append(window)
        # ワーカーの結果が届いた状態を作る (HTTP は行わない)
        window._billing_config = BillingConfig(
            enabled=True, provider="Stripe",
            price_label="月額 980 円 (税込)", manageable=True)
        window._subscription_type = subscription_type
        window._apply_state()
        return window

    # ① 閉じたときに closed が飛ぶ。destroyed は閉じても飛ばないため、
    #    これが「閉じた時点で残高を取り直す」唯一の手がかりになる。
    def test_closing_emits_closed(self):
        window = self._window()
        seen = []
        window.closed.connect(lambda: seen.append(1))
        window.close()
        self.assertEqual([1], seen)

    # 参照を保持したままでも closed は飛ぶ (destroyed が飛ばない条件そのもの)
    def test_closed_fires_while_the_window_is_still_alive(self):
        window = self._window()
        seen = []
        window.closed.connect(lambda: seen.append(1))
        window.close()
        # 画面はまだ生きている (WA_DeleteOnClose を付けていない)
        self.assertFalse(window.isVisible())
        self.assertEqual([1], seen)

    # ② 購入の開始で checkout_started が飛び、反映待ちを外へ渡せる
    def test_checkout_url_hands_the_wait_outside(self):
        window = self._window()
        seen = []
        window.checkout_started.connect(lambda: seen.append(1))
        window._on_checkout_url("https://checkout.stripe.com/c/pay/cs_test_x")
        self.assertEqual([1], seen)
        self.assertIn("反映を待っています", window.note_label.text())

    # ② 画面は反映待ちのワーカーを持たない (持つと閉じたときに消える)
    def test_the_window_does_not_own_the_activation_wait(self):
        window = self._window()
        self.assertFalse(hasattr(window, "_activation_worker"))
        self.assertFalse(hasattr(window, "_start_activation"))

    # 反映が確認できたら、手続き中フラグが戻り表示が作り直される
    def test_apply_activation_clears_the_in_progress_flag(self):
        window = self._window(subscription_type="None")
        window._in_progress = True
        window.apply_activation(True)
        self.assertFalse(window._in_progress)

    # 反映が確認できなかった場合は、やり直す手段を案内する
    def test_apply_activation_points_at_the_refresh_button(self):
        window = self._window(subscription_type="None")
        window._in_progress = True
        window.apply_activation(False)
        self.assertFalse(window._in_progress)
        self.assertIn("更新", window.note_label.text())

    # 「更新」ボタンがあること (利用者が自分で確かめる手段)
    def test_the_refresh_button_exists(self):
        window = self._window()
        self.assertTrue(window.refresh_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
