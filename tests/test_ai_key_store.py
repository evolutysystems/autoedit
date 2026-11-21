# 開発用の鍵の保管の単体テスト (docs/AiEditor/resolve1.md §6.5 / §20)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・鍵が平文で保存されない (DPAPI で保護する)
#   ・鍵が setting.json / DEFAULT_SETTINGS に書かれない
#   ・DPAPI が使えない環境では**保存しない** (平文で書くより持たないほうが安全)
#   ・読めない暗号文 (別の利用者・端末) は「無い」として扱う
#   ・鍵そのものをログへ出さない (§17.2)
# ネットワークは使わない。DPAPI は差し替える。
import json
import logging
import os
import tempfile
import unittest
from unittest import mock

from src.ai import key_store
from src.settings.settings_window import DEFAULT_SETTINGS

_KEY = "sk-ant-test-0123456789"


class AiKeyStoreTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "ai_keys.dat")

    def tearDown(self):
        self._dir.cleanup()

    # DPAPI を「頭に印を付けて反転するだけ」の偽物へ差し替え、
    # 「保存された中身が平文ではない」ことだけを見る。
    def _fake_dpapi(self):
        def protect(plain):
            return b"ENC:" + plain[::-1]

        def unprotect(blob):
            return blob[4:][::-1] if blob.startswith(b"ENC:") else None

        return mock.patch.multiple(key_store, protect=protect, unprotect=unprotect)

    def test_key_is_not_stored_in_plain_text(self):
        with self._fake_dpapi():
            self.assertTrue(key_store.save_key(_KEY, path=self.path))

        with open(self.path, "rb") as handle:
            blob = handle.read()

        self.assertNotIn(_KEY.encode("utf-8"), blob, "平文で保存してはならない")
        self.assertTrue(blob.startswith(b"ENC:"))

    def test_round_trip(self):
        with self._fake_dpapi():
            key_store.save_key(_KEY, path=self.path)

            self.assertEqual(_KEY, key_store.load_key(path=self.path))

    def test_missing_file_is_none(self):
        with self._fake_dpapi():
            self.assertIsNone(key_store.load_key(path=self.path))

    # 他の利用者・端末の暗号文は復号できない。無いものとして扱う。
    def test_undecryptable_blob_is_treated_as_missing(self):
        with open(self.path, "wb") as handle:
            handle.write(b"this-is-not-ours")

        with self._fake_dpapi():
            self.assertIsNone(key_store.load_key(path=self.path))

    # DPAPI が使えない環境では保存しない (平文では書かない)。
    def test_without_dpapi_nothing_is_written(self):
        with mock.patch.object(key_store, "protect", lambda plain: None):
            self.assertFalse(key_store.save_key(_KEY, path=self.path))

        self.assertFalse(os.path.exists(self.path))

    def test_empty_key_is_rejected(self):
        with self._fake_dpapi():
            self.assertFalse(key_store.save_key("   ", path=self.path))

        self.assertFalse(os.path.exists(self.path))

    def test_clear_removes_the_file(self):
        with self._fake_dpapi():
            key_store.save_key(_KEY, path=self.path)

        self.assertTrue(key_store.clear_key(path=self.path))
        self.assertFalse(os.path.exists(self.path))

    # 鍵そのものはログへ出さない。長さだけを残す。
    def test_key_is_not_logged(self):
        with self._fake_dpapi():
            with self.assertLogs("src.ai.key_store", level=logging.INFO) as captured:
                key_store.save_key(_KEY, path=self.path)

        for line in captured.output:
            self.assertNotIn(_KEY, line)

    # 置き場は auth.dat と同じフォルダ (%LOCALAPPDATA%\\Stretheus)。
    def test_default_path_is_next_to_auth(self):
        path = key_store.default_key_path()

        self.assertTrue(path.endswith("ai_keys.dat"))
        self.assertIn("Stretheus", path)


class AiKeyNotInSettingsTest(unittest.TestCase):

    # 配布 setting.json に鍵欄を持たせない (sec2 N1 / §6.5)。
    def test_settings_have_no_key(self):
        flat = json.dumps(DEFAULT_SETTINGS["ai"], ensure_ascii=False)

        self.assertNotIn("sk-ant", flat)
        self.assertNotIn("api_key", flat)

        settings_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "settings", "setting.json")
        with open(settings_path, encoding="utf-8") as handle:
            saved = handle.read()

        self.assertNotIn("sk-ant", saved)


if __name__ == "__main__":
    unittest.main()
