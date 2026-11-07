# API 接続先の決まり方の単体テスト (sec2 N1)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・接続先を決めるのは src/services/config.py の _DEFAULT_BASE_URL 1 箇所
#     (以前は setting.json / DEFAULT_SETTINGS / _normalize_legacy_values の 3 か所に
#      散っており、1 か所だけ直しても向き先が変わらなかった / plan.md §7.8.4)
#   ・setting.json と DEFAULT_SETTINGS は空を既定とし、配布物へ URL を焼き付けない
#   ・別の環境を指すときは環境変数 STRETHEUS_API_BASE_URL を使う
#   ・凍結配布では環境変数を無視する (向き先を外から変えさせない / sec2 S2)
#   ・既定として配ったことのある URL が保存されていたら空へ寄せ、既定に追従させる
#
# **どの環境が現運用かはここで固定しない。** 切り替えのたびにテストを直す羽目になるため、
# 「config._DEFAULT_BASE_URL へ倒れること」だけを見る。
# 外部ネットワークは使わない。
import unittest
from unittest import mock

from src.services import config
from src.settings.settings_window import (DEFAULT_SETTINGS, _MANAGED_API_BASE_URLS,
                                          _migrate_api_base_url)

_OTHER_URL = "https://stretheus-another.example.invalid"


# 環境変数と「ログを出したか」の記録を毎回まっさらにする。
# _env_override_logged はモジュール変数のため、残すと他のテストの結果が変わる。
class _CleanEnv:

    def __enter__(self):
        self._patch = mock.patch.dict("os.environ", {}, clear=False)
        self._patch.start()
        import os
        os.environ.pop(config._ENV_BASE_URL, None)
        config._env_override_logged = False
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        config._env_override_logged = False
        return False


class ApiBaseUrlTest(unittest.TestCase):

    # 設定が空なら既定へ倒れる (配布物がこの経路を通る)
    def test_empty_settings_falls_back_to_default(self):
        with _CleanEnv():
            for settings in ({}, {"api": {}}, {"api": {"base_url": ""}}, {"api": {"base_url": "   "}}):
                self.assertEqual(config.api_config(settings)["base_url"],
                                 config._DEFAULT_BASE_URL, settings)

    # 既定値そのものが空だと接続先を失う (集約先が空では困る)
    def test_default_base_url_is_set(self):
        self.assertTrue(config._DEFAULT_BASE_URL.startswith("https://"))

    # setting.json に値があればそれを使う (従来どおり)
    def test_settings_value_is_used(self):
        with _CleanEnv():
            self.assertEqual(
                config.api_config({"api": {"base_url": _OTHER_URL}})["base_url"], _OTHER_URL)

    # 末尾のスラッシュは落とす (パス結合で // にしないため)
    def test_trailing_slash_is_stripped(self):
        with _CleanEnv():
            self.assertEqual(
                config.api_config({"api": {"base_url": _OTHER_URL + "/"}})["base_url"], _OTHER_URL)

    # 環境変数は setting.json より優先する (開発端末で別環境を指す経路)
    def test_env_overrides_settings(self):
        import os
        with _CleanEnv():
            os.environ[config._ENV_BASE_URL] = _OTHER_URL
            self.assertEqual(
                config.api_config({"api": {"base_url": config._DEFAULT_BASE_URL}})["base_url"],
                _OTHER_URL)

    # 環境変数が空文字なら無視する (設定ミスで接続先を見失わない)
    def test_blank_env_is_ignored(self):
        import os
        with _CleanEnv():
            os.environ[config._ENV_BASE_URL] = "  "
            self.assertEqual(config.api_config({})["base_url"], config._DEFAULT_BASE_URL)

    # 凍結配布では環境変数を無視する (配布物の向き先を外から変えさせない)
    def test_env_is_ignored_when_frozen(self):
        import os
        with _CleanEnv():
            os.environ[config._ENV_BASE_URL] = _OTHER_URL
            with mock.patch.object(config.sys, "frozen", True, create=True):
                self.assertEqual(config.api_config({})["base_url"], config._DEFAULT_BASE_URL)

    # timeout_sec は接続先の変更に巻き込まれない
    def test_timeout_is_independent(self):
        with _CleanEnv():
            self.assertEqual(config.api_config({"api": {"timeout_sec": 30}})["timeout_sec"], 30.0)
            self.assertEqual(config.api_config({})["timeout_sec"], 15.0)


class DefaultSettingsTest(unittest.TestCase):

    # 既定値に接続先を書かない (書くと配布物へ焼き付く / sec2 N1)
    def test_default_settings_base_url_is_empty(self):
        self.assertEqual(DEFAULT_SETTINGS["api"]["base_url"], "")


class ManagedBaseUrlMigrationTest(unittest.TestCase):

    # 既定として配ったことのある URL は空へ寄せ、config.py の既定に追従させる
    def test_managed_url_is_cleared(self):
        for url in _MANAGED_API_BASE_URLS:
            merged = {"api": {"base_url": url, "timeout_sec": 15}}
            self.assertTrue(_migrate_api_base_url(merged), url)
            self.assertEqual(merged["api"]["base_url"], "")

    # 末尾スラッシュ・大文字・前後の空白でも同じ値として扱う
    def test_managed_url_variants_are_cleared(self):
        url = _MANAGED_API_BASE_URLS[0]
        for value in (url + "/", url.upper(), "  " + url + "  "):
            merged = {"api": {"base_url": value}}
            self.assertTrue(_migrate_api_base_url(merged), value)
            self.assertEqual(merged["api"]["base_url"], "")

    # 利用者が入れた独自の接続先は尊重する (上書きしない)
    def test_custom_url_is_kept(self):
        merged = {"api": {"base_url": _OTHER_URL}}
        self.assertFalse(_migrate_api_base_url(merged))
        self.assertEqual(merged["api"]["base_url"], _OTHER_URL)

    # 既に空・api セクションが無い場合は何もしない
    def test_empty_or_missing_is_untouched(self):
        for merged in ({"api": {"base_url": ""}}, {"api": {}}, {}, {"api": None}):
            self.assertFalse(_migrate_api_base_url(merged))

    # 移行後は既定へ倒れる (切り替え時に既存利用者が置き去りにならないことの確認)
    def test_cleared_value_resolves_to_default(self):
        with _CleanEnv():
            merged = {"api": {"base_url": _MANAGED_API_BASE_URLS[0]}}
            _migrate_api_base_url(merged)
            self.assertEqual(config.api_config(merged)["base_url"], config._DEFAULT_BASE_URL)


class ShippedSettingsTest(unittest.TestCase):

    # リポジトリの setting.json に接続先を書かない。
    # ここに値が入ったままビルドすると配布物へ焼き付くため、テストで止める。
    def test_repository_setting_json_has_no_base_url(self):
        import json
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "src", "settings", "setting.json")
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        self.assertEqual(data.get("api", {}).get("base_url", ""), "")


if __name__ == "__main__":
    unittest.main()
