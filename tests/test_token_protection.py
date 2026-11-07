# ローカルへ保存する機微情報の保護の単体テスト (sec2 S4 / N2)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・Twitch アクセストークンは DPAPI で暗号化して保存する (平文では書かない)
#   ・サブスク判定のキャッシュも DPAPI で保護する (書き換えでの透かし回避を防ぐ)
#   ・旧版が書いた平文ファイルは読み込み時に暗号化して置き換える
#   ・暗号化できない環境では保存しない (平文へ落ちない)
#   ・オフライン救済の既定は 24 時間 (旧既定 72 は読み込み時に詰める)
# 外部ネットワークは使わない。DPAPI は実機の Windows API をそのまま使う。
import json
import os
import tempfile
import unittest
from unittest import mock

from src.archive.twitch_auth import TwitchAuth
from src.services import points as points_module
from src.services.auth_store import protect, unprotect
from src.services.points import PointsService
from src.settings.settings_window import (DEFAULT_SETTINGS, _LEGACY_SUBSCRIPTION_CACHE_HOURS,
                                          _migrate_subscription_cache_hours)

_TOKEN = {"access_token": "abc123", "scope": "user:read:broadcast", "token_type": "bearer"}


class TwitchTokenProtectionTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="stretheus_test_")
        self.path = os.path.join(self._dir, "twitch_token.json")
        self.addCleanup(lambda: __import__("shutil").rmtree(self._dir, ignore_errors=True))

    def _auth(self):
        return TwitchAuth("client-id", token_path=self.path)

    # 保存したファイルは平文 JSON ではない (そのまま読めない)
    def test_token_is_not_stored_in_plain_text(self):
        auth = self._auth()
        auth._token = dict(_TOKEN)
        auth._write_token_file()

        with open(self.path, "rb") as f:
            raw = f.read()
        self.assertNotIn(b"abc123", raw)
        with self.assertRaises((ValueError, UnicodeDecodeError)):
            json.loads(raw.decode("utf-8"))
        # DPAPI で復号すれば元に戻る
        self.assertEqual(json.loads(unprotect(raw).decode("utf-8")), _TOKEN)

    # 書いたものを読み戻せる
    def test_round_trip(self):
        auth = self._auth()
        auth._token = dict(_TOKEN)
        auth._write_token_file()

        self.assertEqual(self._auth()._token, _TOKEN)
        self.assertTrue(self._auth().is_logged_in())

    # 旧版の平文 JSON を読めて、その場で暗号化へ置き換える (移行)
    def test_legacy_plain_file_is_migrated(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(_TOKEN, f)

        auth = self._auth()
        self.assertEqual(auth._token, _TOKEN)       # 読めている
        with open(self.path, "rb") as f:
            raw = f.read()
        self.assertNotIn(b"abc123", raw)            # 平文が残っていない
        self.assertEqual(json.loads(unprotect(raw).decode("utf-8")), _TOKEN)

    # 暗号化できない環境では保存しない (平文へ落ちない)
    def test_no_plain_fallback_when_protect_fails(self):
        auth = self._auth()
        auth._token = dict(_TOKEN)
        with mock.patch("src.archive.twitch_auth.protect", return_value=None):
            auth._write_token_file()
        self.assertFalse(os.path.exists(self.path))

    # 壊れた/他ユーザーのファイルは None (再ログインで復旧する)
    def test_unreadable_file_returns_none(self):
        with open(self.path, "wb") as f:
            f.write(b"\x00\x01 not a token")
        self.assertIsNone(self._auth()._token)

    # ログアウトでファイルごと消える
    def test_logout_removes_file(self):
        auth = self._auth()
        auth._token = dict(_TOKEN)
        auth._write_token_file()
        auth.logout()
        self.assertFalse(os.path.exists(self.path))


class _FakeAuth:

    def __init__(self, anonymous=False):
        self._anonymous = anonymous

    def is_anonymous(self):
        return self._anonymous


class SubscriptionCacheProtectionTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="stretheus_test_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self._dir, ignore_errors=True))
        self.service = PointsService(_FakeAuth(), {}, store_dir=self._dir)
        self.state_path = os.path.join(self._dir, "points.json")

    # サブスク判定は平文で保存しない (書き換えでの透かし回避を防ぐ)
    def test_state_is_not_stored_in_plain_text(self):
        self.service._write_state({"subscribed": True, "checkedAt": "2026-11-07T00:00:00+00:00"})

        with open(self.state_path, "rb") as f:
            raw = f.read()
        self.assertNotIn(b"subscribed", raw)
        self.assertEqual(json.loads(unprotect(raw).decode("utf-8"))["subscribed"], True)

    # 書いたものを読み戻せる
    def test_round_trip(self):
        self.service._write_state({"subscribed": True, "checkedAt": "x"})
        self.assertEqual(self.service._read_state(), {"subscribed": True, "checkedAt": "x"})

    # 旧版の平文 JSON を読めて、その場で暗号化へ置き換える (移行)
    def test_legacy_plain_state_is_migrated(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({"subscribed": True, "checkedAt": "x"}, f)

        self.assertEqual(self.service._read_state()["subscribed"], True)
        with open(self.state_path, "rb") as f:
            raw = f.read()
        self.assertNotIn(b"subscribed", raw)

    # 暗号化できない環境では保存しない (= オフライン時は透かしが入る安全側)
    def test_no_plain_fallback_when_protect_fails(self):
        with mock.patch("src.services.points.protect", return_value=None):
            self.service._write_state({"subscribed": True, "checkedAt": "x"})
        self.assertFalse(os.path.exists(self.state_path))

    # 壊れたファイルは空の状態として扱う
    def test_unreadable_state_returns_empty(self):
        with open(self.state_path, "wb") as f:
            f.write(b"\x00\x01 broken")
        self.assertEqual(self.service._read_state(), {})

    # 送信待ちキューは平文のまま (保存できないと利用者の不利益になるため)
    def test_pending_queue_stays_plain(self):
        self.service._write_queue([{"action": "commit", "reservationId": "r1"}])
        with open(os.path.join(self._dir, "pending_commits.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)[0]["reservationId"], "r1")


class SubscriptionCacheHoursTest(unittest.TestCase):

    # 既定は 24 時間 (sec2 N2 で 72 から短縮)
    def test_defaults_are_24_hours(self):
        self.assertEqual(points_module._DEFAULT_SUBSCRIPTION_CACHE_HOURS, 24)
        self.assertEqual(DEFAULT_SETTINGS["points"]["subscription_cache_hours"], 24)

    # 旧既定 72 を保存している利用者は現行既定へ詰める
    def test_legacy_72_is_migrated(self):
        merged = {"points": {"subscription_cache_hours": _LEGACY_SUBSCRIPTION_CACHE_HOURS}}
        self.assertTrue(_migrate_subscription_cache_hours(merged))
        self.assertEqual(merged["points"]["subscription_cache_hours"], 24)

    # 利用者が自分で変えた値は尊重する
    def test_custom_value_is_kept(self):
        for value in (1, 12, 48, 96, 0):
            merged = {"points": {"subscription_cache_hours": value}}
            self.assertFalse(_migrate_subscription_cache_hours(merged), value)
            self.assertEqual(merged["points"]["subscription_cache_hours"], value)

    # 型違い・欠落では何もしない
    def test_invalid_shapes_are_untouched(self):
        for merged in ({"points": {"subscription_cache_hours": "72"}},
                       {"points": {"subscription_cache_hours": True}},
                       {"points": {}}, {}, {"points": None}):
            self.assertFalse(_migrate_subscription_cache_hours(merged))


if __name__ == "__main__":
    unittest.main()
