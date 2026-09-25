# ポイントの画面まわり (ver5 resolve.md §5.6 R11 / R12 / R13) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・未ログインでは残高を出さず「ログイン」だけを見せること (§4-5)
#   ・オフラインでは最後に取得した値を薄字で出すこと
#   ・確認ダイアログの既定が「取りやめ」であること (R12)
#   ・アカウントタブが未ログイン / ログイン済みで表示を切り替えること (R13)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.gui import points_indicator
from src.gui.account_tab import AccountTab
from src.gui.points_indicator import PointsIndicator, format_balance, format_reset
from src.services import points as points_service


def _app():
    return QApplication.instance() or QApplication([])


# ネットワークへ出ない PointsService の代役
class _FakeClient:

    def __init__(self, items=None):
        self.items = list(items or [])
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        return {"items": list(self.items)}


class _FakeAuth:

    def __init__(self, logged_in=False, user=None):
        self._logged_in = logged_in
        self._user = user or {"displayName": "テスト配信者"}
        self.logged_out = 0
        self.client = _FakeClient()

    def is_logged_in(self):
        return self._logged_in

    def user(self):
        return self._user

    def logout(self):
        self.logged_out += 1
        self._logged_in = False


class _FakePoints:

    def __init__(self, logged_in=False, balance=None, last=None):
        self.auth = _FakeAuth(logged_in=logged_in)
        self._balance = balance
        self._last = last
        self.flushed = 0

    def flush_pending(self):
        self.flushed += 1

    def balance(self, force=False):
        return self._balance

    def last_balance(self):
        return self._last


_BALANCE = {"available": 125, "balance": 125, "nextResetAt": "2026-10-11T00:00:00Z",
            "unlimited": False, "subscriptionType": "None",
            "costs": {"clip": 25, "archive": 100}}


class FormatTest(unittest.TestCase):

    def test_balance_text(self):
        self.assertEqual(format_balance(_BALANCE), "残り 125 pt / 次回リセット 10/11")

    def test_subscriber_text(self):
        self.assertEqual(format_balance({"unlimited": True}), "サブスク特典で無制限")

    def test_offline_text(self):
        self.assertEqual(format_balance(None), "残高を取得できません")

    # 次回リセットが無い応答でも壊れないこと
    def test_without_reset(self):
        self.assertEqual(format_balance({"available": 30}), "残り 30 pt")
        self.assertEqual(format_reset({}), "")


class IndicatorTest(unittest.TestCase):

    def setUp(self):
        _app()

    def _indicator(self, points):
        indicator = PointsIndicator(points, {"points": {"balance_refresh_sec": 300}})
        self.addCleanup(indicator.deleteLater)
        return indicator

    # 未ログインでは残高を出さず、ログインボタンだけを見せること
    def test_logged_out(self):
        indicator = self._indicator(_FakePoints(logged_in=False))

        self.assertTrue(indicator.login_button.isVisibleTo(indicator))
        self.assertFalse(indicator.label.isVisibleTo(indicator))
        self.assertEqual(indicator.label.text(), "")

    # ログイン済みなら残高を出し、ログインボタンを隠すこと
    def test_logged_in(self):
        indicator = self._indicator(_FakePoints(logged_in=True, balance=_BALANCE))
        indicator._on_loaded(_BALANCE)

        self.assertFalse(indicator.login_button.isVisibleTo(indicator))
        self.assertEqual(indicator.label.text(), "残り 125 pt / 次回リセット 10/11")
        self.assertTrue(indicator.label.isEnabled())

    # オフラインでは最後に取得できた値を薄字 (無効表示) で出すこと
    def test_offline_uses_the_last_value(self):
        indicator = self._indicator(_FakePoints(logged_in=True, balance=None, last=_BALANCE))
        indicator._on_loaded(None)

        self.assertEqual(indicator.label.text(), "残り 125 pt / 次回リセット 10/11")
        self.assertFalse(indicator.label.isEnabled(), "オフラインでも通常表示になっています")
        self.assertIn("接続", indicator.label.toolTip())

    # 一度も取得できていなければ断り書きを出すこと
    def test_offline_without_any_value(self):
        indicator = self._indicator(_FakePoints(logged_in=True))
        indicator._on_loaded(None)

        self.assertEqual(indicator.label.text(), "残高を取得できません")


class ConfirmTest(unittest.TestCase):

    # 確認フックが無ければそのまま続ける (CLI / テスト / 従来の呼び出し)
    def test_without_a_hook(self):
        reservation = points_service.Reservation("clip", "video", watermark_required=True)
        self.assertTrue(points_service.confirmed(reservation, None))

    # 透かしが不要なら確認しないこと (R9: 残高から先回りしない)
    def test_no_prompt_when_not_required(self):
        asked = []
        reservation = points_service.Reservation("clip", "video", watermark_required=False)

        self.assertTrue(points_service.confirmed(reservation, lambda: asked.append(1) or True))
        self.assertEqual(asked, [])

    # 予約が無い (未ログイン) 場合も確認しないこと
    def test_no_prompt_without_a_reservation(self):
        asked = []
        self.assertTrue(points_service.confirmed(None, lambda: asked.append(1) or True))
        self.assertEqual(asked, [])

    # 「いいえ」なら False を返し、出力させないこと
    def test_declined(self):
        reservation = points_service.Reservation("clip", "video", watermark_required=True)
        self.assertFalse(points_service.confirmed(reservation, lambda: False))

    # 既定は「取りやめ」であること (ダイアログの既定ボタン / R12)
    def test_dialog_default_is_no(self):
        _app()
        from PySide6.QtWidgets import QMessageBox

        captured = {}

        def _question(_parent, _title, message, _buttons, default):
            captured["message"] = message
            captured["default"] = default
            return QMessageBox.No

        original = QMessageBox.question
        QMessageBox.question = staticmethod(_question)
        try:
            self.assertFalse(points_indicator.watermark_confirm()())
        finally:
            QMessageBox.question = original

        self.assertEqual(captured["default"], QMessageBox.No)
        self.assertIn("透かし", captured["message"])


class AccountTabTest(unittest.TestCase):

    def setUp(self):
        _app()

    def _tab(self, points, settings=None):
        tab = AccountTab(points, settings or {})
        self.addCleanup(tab.deleteLater)
        return tab

    # ポイントの実体が無い (単独起動) 場合も開けること
    def test_without_points(self):
        tab = self._tab(None)

        self.assertEqual(tab.status_label.text(), "ログインしていません")
        self.assertTrue(tab.login_button.isVisibleTo(tab))
        self.assertFalse(tab.logout_button.isVisibleTo(tab))

    # 未ログインでは「消費しない」案内を出すこと (§4-5)
    def test_logged_out_note(self):
        tab = self._tab(_FakePoints(logged_in=False))

        self.assertTrue(tab.note_label.isVisibleTo(tab))
        self.assertIn("透かしも入りません", tab.note_label.text())

    # ログイン済みならユーザー名と残高・単価を出すこと
    def test_logged_in(self):
        tab = self._tab(_FakePoints(logged_in=True, balance=_BALANCE))
        tab._on_balance(_BALANCE)

        self.assertIn("テスト配信者", tab.status_label.text())
        self.assertIn("残り 125 pt", tab.balance_label.text())
        self.assertIn("クリップ 25 pt", tab.balance_label.text())
        self.assertTrue(tab.logout_button.isVisibleTo(tab))

    # サブスク会員には単価を出さない (消費しないため)
    def test_subscriber(self):
        tab = self._tab(_FakePoints(logged_in=True))
        tab._on_balance({"unlimited": True, "subscriptionType": "TwitchSub"})

        self.assertIn("無制限", tab.balance_label.text())
        self.assertNotIn("単価", tab.balance_label.text())

    # 導線の URL が空なら「サブスクページを開く」は出さないこと
    def test_subscribe_button_needs_a_url(self):
        tab = self._tab(_FakePoints(logged_in=True))
        self.assertFalse(tab.subscribe_button.isVisibleTo(tab))

        tab = self._tab(_FakePoints(logged_in=True),
                        {"points": {"subscribe_url": "https://example.com/subs"}})
        self.assertTrue(tab.subscribe_button.isVisibleTo(tab))

    # 履歴は新しい順に、内容と増減を出すこと
    def test_history_rows(self):
        tab = self._tab(_FakePoints(logged_in=True))
        tab._on_history([
            {"kind": "Commit", "jobType": "Clip", "amount": -25, "balanceAfter": 100,
             "watermarked": False, "createdAt": "2026-09-25T01:00:00Z"},
            {"kind": "Grant", "amount": 150, "balanceAfter": 150,
             "createdAt": "2026-09-11T00:00:00Z"},
        ])

        self.assertEqual(tab.history_table.rowCount(), 2)
        self.assertEqual(tab.history_table.item(0, 1).text(), "消費 (クリップ)")
        self.assertEqual(tab.history_table.item(0, 2).text(), "-25")
        self.assertEqual(tab.history_table.item(1, 1).text(), "付与")
        self.assertEqual(tab.history_table.item(1, 2).text(), "+150")

    # 透かし入りの消費は履歴でわかること (サポート対応で要る)
    def test_history_marks_watermarked(self):
        tab = self._tab(_FakePoints(logged_in=True))
        tab._on_history([{"kind": "Commit", "jobType": "Archive", "amount": 0,
                          "balanceAfter": 0, "watermarked": True,
                          "createdAt": "2026-09-25T01:00:00Z"}])

        self.assertIn("透かし入り", tab.history_table.item(0, 1).text())


if __name__ == "__main__":
    unittest.main()
