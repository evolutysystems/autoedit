# サブスクリプション (Stripe) のクライアント側ファサード (ver6 resolve §4) の単体テスト
# 実行: python -m unittest discover -s tests
# 要望 (ver6 resolve):
#   ・config() は例外を投げない。API 未実装 (404) もオフラインも enabled=False (§4.2 / §4.3)
#   ・start_checkout / open_portal は利用者が押した操作なので例外を通す (§4.2)
#   ・反映待ちは unlimited が立つまでポーリングし、通信エラーでは止まらない (§4.4)
# ネットワークは使わない。API クライアントと時間待ちを差し替えて応答だけを与える。
import unittest

from src.exceptions import ApiError, ApiOfflineError
from src.services import billing as billing_module
from src.services.billing import (
    CODE_ALREADY_SUBSCRIBED,
    TYPE_NONE,
    TYPE_STRIPE,
    BillingService,
)

_SETTINGS = {
    "billing": {
        "config_cache_sec": 3600,
        "activation_poll_sec": 1,
        "activation_timeout_sec": 5,
    }
}

_CONFIG = {
    "enabled": True,
    "provider": "Stripe",
    "priceLabel": "月額 980 円 (税込)",
    "manageable": True,
}


# 時間を進める役。sleep された分だけ monotonic が進む。
# 実時間を待たずに「タイムアウトまでのポーリング回数」を確かめられる。
class _FakeClock:

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += float(seconds)


# 応答をテストから指定できる API クライアント。
class _FakeClient:

    def __init__(self):
        self.responses = {}
        self.errors = {}
        self.calls = []

    def get(self, path, authenticated=True):
        return self._respond("GET", path)

    def post(self, path, body=None, authenticated=True):
        return self._respond("POST", path)

    def _respond(self, method, path):
        self.calls.append((method, path))
        error = self.errors.get(path)
        if error is not None:
            raise error
        return self.responses.get(path)

    def count(self, path):
        return len([call for call in self.calls if call[1] == path])


class _FakeAuth:

    def __init__(self):
        self.client = _FakeClient()

    def is_logged_in(self):
        return True


# PointsService の代わり。balance() の戻り値をテストから差し替える。
class _FakePoints:

    def __init__(self, auth):
        self.auth = auth
        # 1 回目以降に返す残高を順に積む。尽きたら最後の値を返し続ける。
        self.balances = [{"unlimited": False, "subscriptionType": TYPE_NONE}]
        self.error = None
        self.balance_calls = 0

    def balance(self, force=False):
        self.balance_calls += 1
        if self.error is not None:
            raise self.error
        index = min(self.balance_calls - 1, len(self.balances) - 1)
        return self.balances[index]

    def last_balance(self):
        index = min(max(self.balance_calls - 1, 0), len(self.balances) - 1)
        return self.balances[index]


class BillingServiceTest(unittest.TestCase):

    def setUp(self):
        self._auth = _FakeAuth()
        self._points = _FakePoints(self._auth)
        self._service = BillingService(self._points, _SETTINGS)
        # 実時間を待たずに済ませる。sleep した分だけ時計が進む。
        self._clock = _FakeClock()
        self._original_time = billing_module.time
        billing_module.time = self._clock

    def tearDown(self):
        billing_module.time = self._original_time

    # ---- config ----------------------------------------------------------

    def test_config_is_disabled_when_the_api_is_missing(self):
        # API が未実装のあいだ (404) は導線を出さない。例外は出さない。
        self._auth.client.errors["/api/billing/config"] = ApiError(
            "not found", status=404)

        config = self._service.config()

        self.assertFalse(config.enabled)
        self.assertFalse(config.is_available())
        self.assertFalse(self._service.is_available())

    def test_config_is_disabled_when_offline(self):
        self._auth.client.errors["/api/billing/config"] = ApiOfflineError("offline")

        self.assertFalse(self._service.config().enabled)

    def test_config_without_price_label_is_not_available(self):
        # 価格の表示文が無い応答は不完全とみなし、導線を出さない。
        self._auth.client.responses["/api/billing/config"] = {
            "enabled": True, "provider": "Stripe", "priceLabel": "",
        }

        self.assertFalse(self._service.is_available())

    def test_config_is_read_once_and_cached(self):
        self._auth.client.responses["/api/billing/config"] = _CONFIG

        first = self._service.config()
        second = self._service.config()

        self.assertTrue(first.is_available())
        self.assertEqual("月額 980 円 (税込)", second.price_label)
        self.assertTrue(second.manageable)
        self.assertEqual(1, self._auth.client.count("/api/billing/config"))

    def test_config_is_reread_when_forced(self):
        self._auth.client.responses["/api/billing/config"] = _CONFIG

        self._service.config()
        self._service.config(force=True)

        self.assertEqual(2, self._auth.client.count("/api/billing/config"))

    # ---- 決済画面の URL ---------------------------------------------------

    def test_start_checkout_returns_the_url(self):
        self._auth.client.responses["/api/billing/checkout-session"] = {
            "url": "https://checkout.stripe.com/c/pay/test",
        }

        self.assertEqual("https://checkout.stripe.com/c/pay/test",
                         self._service.start_checkout())

    def test_start_checkout_raises_when_already_subscribed(self):
        # 押した操作なので、ここだけは理由を外へ出す。
        self._auth.client.errors["/api/billing/checkout-session"] = ApiError(
            "already subscribed", status=409, code=CODE_ALREADY_SUBSCRIBED)

        with self.assertRaises(ApiError) as caught:
            self._service.start_checkout()

        self.assertEqual(CODE_ALREADY_SUBSCRIBED, caught.exception.code)

    def test_start_checkout_raises_when_the_url_is_missing(self):
        self._auth.client.responses["/api/billing/checkout-session"] = {}

        with self.assertRaises(ApiError):
            self._service.start_checkout()

    def test_open_portal_returns_the_url(self):
        self._auth.client.responses["/api/billing/portal-session"] = {
            "url": "https://billing.stripe.com/p/session/test",
        }

        self.assertEqual("https://billing.stripe.com/p/session/test",
                         self._service.open_portal())

    # ---- 反映待ち ---------------------------------------------------------

    def test_wait_returns_immediately_when_already_active(self):
        self._points.balances = [{"unlimited": True, "subscriptionType": TYPE_STRIPE}]

        self.assertTrue(self._service.wait_for_activation())
        self.assertEqual(1, self._points.balance_calls)
        self.assertEqual([], self._clock.slept)

    def test_wait_polls_until_the_webhook_lands(self):
        # 3 回目の照会で unlimited が立つ。
        self._points.balances = [
            {"unlimited": False, "subscriptionType": TYPE_NONE},
            {"unlimited": False, "subscriptionType": TYPE_NONE},
            {"unlimited": True, "subscriptionType": TYPE_STRIPE},
        ]

        self.assertTrue(self._service.wait_for_activation())
        self.assertEqual(3, self._points.balance_calls)

    def test_wait_gives_up_without_raising(self):
        # 支払いを取りやめてブラウザを閉じた場合もここへ来る。異常ではない。
        self._points.balances = [{"unlimited": False, "subscriptionType": TYPE_NONE}]

        self.assertFalse(self._service.wait_for_activation())

    def test_wait_survives_a_connection_error(self):
        # 瞬断で待つのをやめると「加入したのに反映されない」ように見える。
        self._points.error = ApiOfflineError("offline")

        self.assertFalse(self._service.wait_for_activation())
        self.assertGreater(self._points.balance_calls, 1)

    def test_wait_stops_when_asked_to(self):
        self._points.balances = [{"unlimited": False, "subscriptionType": TYPE_NONE}]

        self.assertFalse(
            self._service.wait_for_activation(should_continue=lambda: False))
        self.assertEqual(0, self._points.balance_calls)

    def test_wait_is_skipped_when_the_timeout_is_zero(self):
        service = BillingService(
            self._points, {"billing": {"activation_timeout_sec": 0}})

        self.assertFalse(service.wait_for_activation())
        self.assertEqual(0, self._points.balance_calls)

    # ---- 種別 -------------------------------------------------------------

    def test_subscription_type_comes_from_the_last_balance(self):
        self._points.balances = [{"unlimited": True, "subscriptionType": TYPE_STRIPE}]
        self._service.is_subscribed()

        self.assertEqual(TYPE_STRIPE, self._service.subscription_type())

    def test_subscription_type_is_none_without_a_balance(self):
        self._points.balances = [None]

        self.assertEqual(TYPE_NONE, self._service.subscription_type())


if __name__ == "__main__":
    unittest.main()
