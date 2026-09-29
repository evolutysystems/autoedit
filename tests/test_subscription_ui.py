# サブスクリプション導線の画面まわり (ver6 resolve2) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・メイン画面のボタンが加入種別で「無効 + チェックマーク」になること (要望 ①後 / ②後)
#   ・チェックマークの色が Twitch は #9147FF 固定、Stripe はテーマ追随であること
#   ・無効化してもチェックマークの色が褪せないこと (§4.5)
#   ・サブスクリプション画面が加入状態に応じて購入ボタンを閉じること (要望 ①)
#   ・残高の取得 1 回につき balance_changed が 1 回だけ流れること (§4.3)
# ネットワークへは出ない。PointsService と BillingService の応答を差し替える。
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import QApplication

from src.gui import theme
from src.gui.points_indicator import PointsIndicator
from src.gui.subscription_window import SubscriptionWindow
from src.services import billing as billing_module
from src.services.billing import BillingConfig


def _app():
    return QApplication.instance() or QApplication([])


class _FakeClient:

    def get(self, path, authenticated=True):
        return {}


class _FakeAuth:

    def __init__(self, logged_in=False):
        self._logged_in = logged_in
        self.client = _FakeClient()

    def is_logged_in(self):
        return self._logged_in

    def user(self):
        return {"displayName": "テスト配信者"}


class _FakePoints:

    def __init__(self, logged_in=False, balance=None):
        self.auth = _FakeAuth(logged_in=logged_in)
        self._balance = balance

    def flush_pending(self):
        pass

    def balance(self, force=False):
        return self._balance

    def last_balance(self):
        return self._balance


def _balance(subscription_type):
    return {"available": 0, "balance": 0, "unlimited": subscription_type != "None",
            "subscriptionType": subscription_type}


# QPixmap から最初に見つかる不透明なピクセルの色を返す (チェックマークの色の確認用)
def _first_opaque_color(pixmap):
    image = pixmap.toImage()
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            if pixel.alpha() > 200:
                return pixel.name().lower()
    return ""


# 加入種別からチェックマークの種類を決める (theme.subscription_check_kind)
class CheckKindTest(unittest.TestCase):

    def test_twitch(self):
        self.assertEqual(theme.CHECK_KIND_TWITCH,
                         theme.subscription_check_kind(_balance("TwitchSub")))

    def test_stripe(self):
        self.assertEqual(theme.CHECK_KIND_STRIPE,
                         theme.subscription_check_kind(_balance("Stripe")))

    def test_not_subscribed(self):
        self.assertEqual("", theme.subscription_check_kind(_balance("None")))

    # 残高が取れていないときに「未加入」と断定しない (印を出さないだけ)
    def test_without_a_balance(self):
        self.assertEqual("", theme.subscription_check_kind(None))


# チェックマークのアイコン (theme.check_icon)
class CheckIconTest(unittest.TestCase):

    def setUp(self):
        self._app = _app()

    # Twitch は要望で指定されたブランド色 (#9147FF) を使う
    def test_twitch_color(self):
        icon = theme.check_icon(theme.CHECK_KIND_TWITCH)
        self.assertEqual(theme.TWITCH_COLOR.lower(),
                         _first_opaque_color(icon.pixmap(QSize(18, 18))))

    # Twitch のブランド色は明暗で変わらない
    def test_twitch_color_is_the_same_in_both_modes(self):
        colors = set()
        for mode in ("dark", "light"):
            theme.invalidate_cache()
            icon = theme.check_icon(theme.CHECK_KIND_TWITCH,
                                    {"ui": {"theme_mode": mode}})
            colors.add(_first_opaque_color(icon.pixmap(QSize(18, 18))))
        theme.invalidate_cache()
        self.assertEqual({theme.TWITCH_COLOR.lower()}, colors)

    # Stripe はテーマの success を使う (明暗で変わる)
    def test_stripe_follows_the_theme(self):
        found = {}
        for mode in ("dark", "light"):
            settings = {"ui": {"theme_mode": mode}}
            theme.invalidate_cache()
            icon = theme.check_icon(theme.CHECK_KIND_STRIPE, settings)
            found[mode] = _first_opaque_color(icon.pixmap(QSize(18, 18)))
            self.assertEqual(theme.color("success", settings).name().lower(),
                             found[mode])
        theme.invalidate_cache()
        self.assertNotEqual(found["dark"], found["light"])

    def test_empty_kind_has_no_icon(self):
        self.assertTrue(theme.check_icon("").isNull())

    # 加入済みのボタンは無効にするため、無効時の絵が褪せてはいけない (§4.5)
    def test_the_disabled_pixmap_keeps_its_color(self):
        icon = theme.check_icon(theme.CHECK_KIND_TWITCH)
        normal = icon.pixmap(QSize(18, 18)).toImage()
        disabled = icon.pixmap(QSize(18, 18), QIcon.Disabled).toImage()
        self.assertEqual(normal, disabled)


# メイン画面のサブスクリプションボタン。
# MainWindow 全体を起こすとパイプラインの依存まで要るため、
# 反映処理 (_refresh_subscription_button) だけを取り出して確かめる。
class _ButtonHost:

    def __init__(self):
        from PySide6.QtWidgets import QPushButton

        from src.gui.main_window import MainWindow

        self.button = QPushButton("サブスクリプション")
        self.subscription_button = self.button
        self._settings = {}
        self._running_tabs = set()
        self._subscription_kind = ""
        self._refresh = MainWindow._refresh_subscription_button.__get__(self)

    def apply(self, balance):
        self._refresh(balance)
        return self.button


class SubscriptionButtonTest(unittest.TestCase):

    def setUp(self):
        self._app = _app()
        self._host = _ButtonHost()

    def test_twitch_subscriber_cannot_press_it(self):
        button = self._host.apply(_balance("TwitchSub"))
        self.assertFalse(button.isEnabled())
        self.assertFalse(button.icon().isNull())
        self.assertEqual(theme.TWITCH_COLOR.lower(),
                         _first_opaque_color(button.icon().pixmap(QSize(18, 18))))

    def test_stripe_subscriber_cannot_press_it(self):
        button = self._host.apply(_balance("Stripe"))
        self.assertFalse(button.isEnabled())
        self.assertEqual(theme.color("success", {}).name().lower(),
                         _first_opaque_color(button.icon().pixmap(QSize(18, 18))))

    def test_without_a_subscription_it_is_pressable(self):
        button = self._host.apply(_balance("None"))
        self.assertTrue(button.isEnabled())
        self.assertTrue(button.icon().isNull())

    # 残高が取れないあいだは印を出さず、押せるままにする
    def test_without_a_balance_it_is_pressable(self):
        button = self._host.apply(None)
        self.assertTrue(button.isEnabled())
        self.assertTrue(button.icon().isNull())

    # 出力の実行中は加入状態を変えさせない
    def test_it_is_disabled_while_running(self):
        self._host._running_tabs = {"clip"}
        self.assertFalse(self._host.apply(_balance("None")).isEnabled())

    # 加入 → ログアウト (残高なし) で印が消えること
    def test_the_check_is_cleared(self):
        self._host.apply(_balance("Stripe"))
        self.assertTrue(self._host.apply(None).icon().isNull())


# 残高インジケータのシグナル (メイン画面へ加入状態を渡す経路)
class BalanceChangedTest(unittest.TestCase):

    def setUp(self):
        self._app = _app()

    # 未ログインでも 1 回流す (ログアウト後に印を消すため)
    def test_it_emits_when_logged_out(self):
        indicator = PointsIndicator(_FakePoints(logged_in=False))
        received = []
        indicator.balance_changed.connect(received.append)
        indicator.refresh(force=True)
        self.assertEqual([None], received)

    def test_it_emits_the_balance_once(self):
        points = _FakePoints(logged_in=True, balance=_balance("TwitchSub"))
        indicator = PointsIndicator(points)
        received = []
        indicator.balance_changed.connect(received.append)
        indicator._on_loaded(points.balance())
        self.assertEqual(1, len(received))
        self.assertEqual("TwitchSub", received[0]["subscriptionType"])


# サブスクリプション画面 (要望 B / C / ① / ②)
class SubscriptionWindowTest(unittest.TestCase):

    def setUp(self):
        self._app = _app()
        self._windows = []

    def tearDown(self):
        for window in self._windows:
            window.close()

    # 未ログインで作ってからログイン状態を差し替える。
    # ログイン済みで作ると reload() がワーカースレッドを起こしてしまい、
    # 待ち合わせのない破棄で Qt が異常終了する。ここで確かめたいのは表示の分岐だけ。
    def _window(self, logged_in=False, subscription_type=None, available=True):
        points = _FakePoints(logged_in=False)
        window = SubscriptionWindow(points, {})
        self._windows.append(window)
        points.auth._logged_in = logged_in
        # ワーカーの結果が届いた状態を作る (HTTP は行わない)
        window._billing_config = BillingConfig(
            enabled=available, provider="Stripe",
            price_label="月額 980 円 (税込)" if available else "", manageable=True)
        window._subscription_type = subscription_type
        window._apply_state()
        return window

    def test_logged_out_shows_the_twitch_login(self):
        window = self._window(logged_in=False)
        self.assertTrue(window.twitch_button.isEnabled())
        self.assertFalse(window.purchase_button.isEnabled())
        self.assertIn("ログイン", window.note_label.text())

    def test_the_twitch_button_is_branded(self):
        window = self._window(logged_in=False)
        self.assertEqual(theme.TWITCH_BUTTON, window.twitch_button.objectName())

    # 要望 ①: Twitch のサブスクリプション中は購入させない
    def test_a_twitch_subscriber_cannot_purchase(self):
        window = self._window(logged_in=True, subscription_type="TwitchSub")
        self.assertFalse(window.purchase_button.isEnabled())
        self.assertIn("Twitch", window.note_label.text())
        self.assertIn("購入は不要", window.note_label.text())

    def test_a_stripe_subscriber_cannot_purchase(self):
        window = self._window(logged_in=True, subscription_type="Stripe")
        self.assertFalse(window.purchase_button.isEnabled())

    def test_a_non_subscriber_can_purchase(self):
        window = self._window(logged_in=True, subscription_type="None")
        self.assertTrue(window.purchase_button.isEnabled())
        self.assertEqual("月額 980 円 (税込)", window.price_label.text())

    # 加入種別が届いていないあいだは購入を開かない (オフラインもここ)
    def test_an_unknown_state_cannot_purchase(self):
        window = self._window(logged_in=True, subscription_type=None)
        self.assertFalse(window.purchase_button.isEnabled())
        self.assertIn("確認", window.note_label.text())

    # 受付停止 (API 未実装 / Stripe 未設定) でも画面は開く
    def test_it_opens_even_when_billing_is_unavailable(self):
        window = self._window(logged_in=True, subscription_type="None",
                              available=False)
        self.assertFalse(window.purchase_button.isEnabled())
        self.assertEqual("", window.price_label.text())
        self.assertIn("受け付けていません", window.note_label.text())

    # ログイン済みなら誰でログインしているかを出し、ボタンは押せなくする
    def test_it_shows_who_is_logged_in(self):
        window = self._window(logged_in=True, subscription_type="None")
        self.assertIn("テスト配信者", window.twitch_button.text())
        self.assertFalse(window.twitch_button.isEnabled())

    # 文言は billing.py が返す理由コードから引く (対応が抜けていないこと)
    def test_every_reason_has_a_message(self):
        from src.gui.subscription_window import _BLOCK_MESSAGES

        for code in (billing_module.BLOCK_NOT_LOGGED_IN,
                     billing_module.BLOCK_UNKNOWN,
                     billing_module.BLOCK_UNAVAILABLE,
                     billing_module.BLOCK_TWITCH,
                     billing_module.BLOCK_STRIPE):
            self.assertTrue(_BLOCK_MESSAGES.get(code), code)


if __name__ == "__main__":
    unittest.main()
