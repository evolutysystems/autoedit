# サブスクリプション画面の段 (ver9 resolve18) の単体テスト
# 実行: python -m unittest discover -s tests
#
# 背景 (2026-11-02 の要望):
#   Twitch アカウントを持っていない利用者も多いため、Google ログインを足して
#   その人たちもサブスクリプションに加入できるようにした (StretheusAPI resolve6)。
#   あわせて画面を段で描き分けた。
#     ① 未ログイン    → Twitch / Google の 2 ボタン
#     ② Twitch サブ中 → 画面を閉じてメイン画面へ戻る (紫のチェックマークは既存表示)
#     ③ Google        → 購入ボタン。そこからは既存処理
#
# 重点:
#   ・purchase_blocked_reason() のコードが段へ漏れなく対応すること
#   ・未ログインでログインボタンだけが出て、購入ボタンが出ないこと
#   ・Google が提供できない構成では Google ボタンを出さないこと
#   ・Twitch サブ中は subscription_changed を出してから閉じること (再入しない)
#   ・Google でログインした利用者には購入ボタンが出ること
# ネットワークへは出ない。PointsService と BillingService の応答を差し替える。
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.gui import subscription_window as sw
from src.gui.subscription_window import SubscriptionWindow
from src.services import billing as billing_module
from src.services.billing import BillingConfig


def _app():
    return QApplication.instance() or QApplication([])


class _FakeClient:

    def get(self, path, authenticated=True):
        return {}


class _FakeAuth:

    def __init__(self, logged_in=False, provider="twitch"):
        self._logged_in = logged_in
        self._provider = provider
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
        return {"displayName": "テスト利用者"}

    def provider(self):
        return self._provider if self._logged_in else ""


class _FakePoints:

    def __init__(self, logged_in=False, balance=None, provider="twitch"):
        self.auth = _FakeAuth(logged_in=logged_in, provider=provider)
        self._balance = balance

    def flush_pending(self):
        pass

    def balance(self, force=False):
        return self._balance

    def last_balance(self):
        return self._balance


class StageMappingTest(unittest.TestCase):

    # コード → 段 の対応に漏れが無いこと。真理値表 (ver6 resolve2 §6) は変えない。
    def test_every_reason_maps_to_a_distinct_stage(self):
        codes = [
            billing_module.BLOCK_NOT_LOGGED_IN,
            billing_module.BLOCK_UNKNOWN,
            billing_module.BLOCK_TWITCH,
            billing_module.BLOCK_STRIPE,
            billing_module.BLOCK_UNAVAILABLE,
            "",
        ]
        stages = [sw._STAGES[code] for code in codes]

        self.assertEqual(len(codes), len(stages))
        # 6 つのコードが 6 つの異なる段へ対応する
        self.assertEqual(len(set(stages)), len(stages))

    # S2 以外の段には必ず注記がある (S2 は閉じるので描かない)
    def test_every_stage_except_the_closing_one_has_a_note(self):
        for stage in set(sw._STAGES.values()):
            if stage == sw._S_TWITCH_SUB:
                continue
            self.assertTrue(sw._STAGE_MESSAGES.get(stage), stage)


class SubscriptionStageTest(unittest.TestCase):

    def setUp(self):
        _app()
        self._windows = []

    def tearDown(self):
        for window in self._windows:
            window.deleteLater()

    def _window(self, logged_in=False, subscription_type="None", available=True,
                google_available=True, provider="twitch"):
        balance = None
        if subscription_type is not None:
            balance = {"available": 0, "balance": 0,
                       "unlimited": subscription_type != "None",
                       "subscriptionType": subscription_type}

        points = _FakePoints(logged_in=logged_in, balance=balance, provider=provider)
        window = SubscriptionWindow(points, {})
        self._windows.append(window)

        # ワーカーの結果が届いた状態を作る (HTTP は行わない)
        window._billing_config = BillingConfig(
            enabled=available, provider="Stripe",
            price_label="月額 980 円 (税込)" if available else "", manageable=True)
        window._subscription_type = subscription_type
        window._google_available = google_available
        window._apply_state()
        return window

    # ---- ① 未ログイン ----------------------------------------------------

    def test_logged_out_shows_both_login_buttons(self):
        window = self._window(logged_in=False)

        self.assertTrue(window.twitch_button.isVisibleTo(window))
        self.assertTrue(window.google_button.isVisibleTo(window))
        self.assertTrue(window.twitch_button.isEnabled())
        self.assertTrue(window.google_button.isEnabled())

    def test_logged_out_hides_the_purchase_button(self):
        # 未ログインでは押しても意味が無い。無効なボタンを見せるより消す。
        window = self._window(logged_in=False)

        self.assertFalse(window.purchase_button.isVisibleTo(window))
        self.assertFalse(window.refresh_button.isVisibleTo(window))

    def test_logged_out_guides_twitch_subscribers_to_twitch(self):
        # Twitch サブの特典を持つ人が Google を選ぶと別の利用者になり特典を失う。
        window = self._window(logged_in=False)

        self.assertIn("Twitch", window.note_label.text())

    def test_google_is_hidden_when_the_api_cannot_provide_it(self):
        # authorize-params が 503 を返す構成では、押しても必ず失敗する。
        window = self._window(logged_in=False, google_available=False)

        self.assertTrue(window.twitch_button.isVisibleTo(window))
        self.assertFalse(window.google_button.isVisibleTo(window))

    # ---- ② Twitch サブで特典適用中 ---------------------------------------

    def test_a_twitch_subscriber_closes_the_window(self):
        points = _FakePoints(
            logged_in=True, provider="twitch",
            balance={"available": 0, "balance": 0, "unlimited": True,
                     "subscriptionType": "TwitchSub"})
        window = SubscriptionWindow(points, {})
        self._windows.append(window)

        seen = []
        window.subscription_changed.connect(lambda: seen.append(1))

        window._billing_config = BillingConfig(
            enabled=True, provider="Stripe",
            price_label="月額 980 円 (税込)", manageable=True)
        window._subscription_type = "TwitchSub"
        window._apply_state()

        # メイン画面へ先に知らせてから閉じる (チェックマークはその結果で付く)
        self.assertEqual([1], seen)
        self.assertFalse(window.isVisible())

    def test_the_closing_path_does_not_re_enter(self):
        # _apply_state は残高と契約情報が別々に届くたび呼ばれるため、
        # S2 の判定は 2 回以上起こりうる。
        points = _FakePoints(
            logged_in=True, provider="twitch",
            balance={"available": 0, "balance": 0, "unlimited": True,
                     "subscriptionType": "TwitchSub"})
        window = SubscriptionWindow(points, {})
        self._windows.append(window)

        seen = []
        window.subscription_changed.connect(lambda: seen.append(1))

        window._billing_config = BillingConfig(
            enabled=True, provider="Stripe",
            price_label="月額 980 円 (税込)", manageable=True)
        window._subscription_type = "TwitchSub"
        window._apply_state()
        window._apply_state()
        window._apply_state()

        self.assertEqual([1], seen)

    # ---- ③ Google ログイン -----------------------------------------------

    def test_a_google_user_can_purchase(self):
        window = self._window(logged_in=True, subscription_type="None", provider="google")

        self.assertTrue(window.purchase_button.isVisibleTo(window))
        self.assertTrue(window.purchase_button.isEnabled())
        self.assertFalse(window.twitch_button.isVisibleTo(window))
        self.assertFalse(window.google_button.isVisibleTo(window))

    def test_a_google_user_sees_which_account_is_signed_in(self):
        window = self._window(logged_in=True, subscription_type="None", provider="google")

        self.assertIn("Google ログイン済み", window.state_label.text())
        self.assertIn("テスト利用者", window.state_label.text())

    # Google の利用者は TwitchSub にならない (StretheusAPI resolve6.md §5)。
    # 画面としては「未加入 → 購入可能」の経路しか辿らない。
    def test_a_google_user_is_never_a_twitch_subscriber(self):
        window = self._window(logged_in=True, subscription_type="Stripe", provider="google")

        self.assertFalse(window.purchase_button.isVisibleTo(window))
        self.assertIn("加入中", window.state_label.text())

    # ---- その他の段 -------------------------------------------------------

    def test_an_unknown_state_shows_neither_login_nor_purchase(self):
        window = self._window(logged_in=True, subscription_type=None)

        self.assertFalse(window.purchase_button.isVisibleTo(window))
        self.assertFalse(window.twitch_button.isVisibleTo(window))
        self.assertIn("確認", window.note_label.text())

    def test_billing_unavailable_still_opens(self):
        window = self._window(logged_in=True, subscription_type="None", available=False)

        self.assertFalse(window.purchase_button.isVisibleTo(window))
        self.assertIn("受け付けていません", window.note_label.text())


if __name__ == "__main__":
    unittest.main()
