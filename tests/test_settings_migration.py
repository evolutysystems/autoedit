# 設定の既定補完・レガシー値の自己修復 (src/settings/settings_window.py) の単体テスト
# 実行: python -m unittest discover -s tests
#
# 重点 (error 20260820):
#   ・更新インストールではインストーラが旧 setting.json を復元するため、
#     配布 setting.json にしか無い値は新しい版へ更新しても届かない。
#     アプリ側 (DEFAULT_SETTINGS + 起動時の補完) で自己修復できることを守る。
#   ・利用者が意図して入れた値は上書きしないこと。
import copy
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.settings import settings_window as sw


def _existing(**auth_overrides):
    # 既存インストールの setting.json 相当 (キーは在るが値が古い状態)
    data = copy.deepcopy(sw.DEFAULT_SETTINGS)
    data["archive"] = copy.deepcopy(data["archive"])
    data["archive"]["auth"] = dict(data["archive"]["auth"], **auth_overrides)
    return data


class TwitchClientIdTest(unittest.TestCase):

    def setUp(self):
        self.default_id = sw.DEFAULT_SETTINGS["archive"]["auth"]["client_id"]

    # 配布物は Client-ID を持っていること (空だとログインできない)
    def test_default_settings_has_client_id(self):
        self.assertTrue(self.default_id,
                        "DEFAULT_SETTINGS に Twitch Client-ID がありません")

    # 更新環境 (空のまま残っている) は起動時に既定で埋まる
    def test_empty_client_id_is_filled(self):
        merged = sw._merge_with_defaults(_existing(client_id=""))
        # マージだけでは既存の空文字が残る (キーが在るため既定が届かない)
        self.assertEqual(merged["archive"]["auth"]["client_id"], "")
        changed = sw._normalize_legacy_values(merged)
        self.assertTrue(changed, "補完した場合は永続化のため True を返すこと")
        self.assertEqual(merged["archive"]["auth"]["client_id"], self.default_id)

    # 空白だけの値も未設定とみなす
    def test_whitespace_client_id_is_filled(self):
        merged = sw._merge_with_defaults(_existing(client_id="   "))
        sw._normalize_legacy_values(merged)
        self.assertEqual(merged["archive"]["auth"]["client_id"], self.default_id)

    # 利用者が自前の Client-ID を入れている場合は上書きしない
    def test_user_client_id_is_kept(self):
        merged = sw._merge_with_defaults(_existing(client_id="my-own-client-id"))
        sw._normalize_legacy_values(merged)
        self.assertEqual(merged["archive"]["auth"]["client_id"], "my-own-client-id")

    # archive セクションごと無い古いファイルでも落ちない
    def test_missing_archive_section(self):
        merged = sw._merge_with_defaults({"general": {}})
        sw._normalize_legacy_values(merged)
        self.assertEqual(merged["archive"]["auth"]["client_id"], self.default_id)

    # 補完すべきものが無ければ False (無用な保存を起こさない)
    def test_no_change_reports_false(self):
        merged = sw._merge_with_defaults(_existing())
        self.assertFalse(sw._fill_twitch_client_id(merged))


class LegacyFfmpegPathTest(unittest.TestCase):

    # 同じ経路で直している既存の自己修復 (20260708) が壊れていないこと
    def test_bare_ffmpeg_name_is_normalized(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data["ffmpeg"] = dict(data["ffmpeg"], executable="ffmpeg",
                              ffprobe_executable="ffprobe.exe")
        merged = sw._merge_with_defaults(data)
        self.assertTrue(sw._normalize_legacy_values(merged))
        self.assertEqual(merged["ffmpeg"]["executable"],
                         sw.DEFAULT_SETTINGS["ffmpeg"]["executable"])
        self.assertEqual(merged["ffmpeg"]["ffprobe_executable"],
                         sw.DEFAULT_SETTINGS["ffmpeg"]["ffprobe_executable"])


class NestedDefaultsTest(unittest.TestCase):

    # timeline の入れ子は新バージョンで増えたキーが補完される (ver3 resolve2 §6)
    def test_timeline_nested_keys_are_filled(self):
        data = copy.deepcopy(sw.DEFAULT_SETTINGS)
        data["timeline"] = copy.deepcopy(data["timeline"])
        data["timeline"]["project"] = {"save_button": True}      # 古い形
        merged = sw._merge_with_defaults(data)
        self.assertTrue(sw._normalize_legacy_values(merged))
        project = merged["timeline"]["project"]
        # resolve9 で増えたキーが埋まっていること
        for key in ("keep_audio", "delete_to_trash", "archive_suffix", "library"):
            self.assertIn(key, project, f"{key} が補完されていません")
        self.assertIn("max_items", project["library"])


if __name__ == "__main__":
    unittest.main()
