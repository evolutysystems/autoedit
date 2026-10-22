# 自動保存の既定と後始末の単体テスト (docs/error/fromUser/resolve.md §8-2 7-3)
# 実行: python -m unittest discover -s tests
#
# 背景: 2026-10-22 の報告では、編集中にアプリが落ちると編集内容がすべて失われた
# (autosave_sec の既定が 0 = 無効だったため)。既定で有効にする。
#
# 重点:
#   ・既定で有効 (60 秒) であること
#   ・利用者が 0 を選んだらそれを尊重すること (無効にできる)
#   ・不正値で画面が壊れないこと
import unittest

from src.settings.settings_window import DEFAULT_SETTINGS
from src.timeline.builder import timeline_config


class AutosaveDefaultTest(unittest.TestCase):

    # 既定で有効。異常終了で編集内容を失わせない。
    def test_enabled_by_default(self):
        self.assertEqual(DEFAULT_SETTINGS["timeline"]["autosave_sec"], 60)
        self.assertEqual(timeline_config({})["autosave_sec"], 60)

    # 利用者が切れること (既定を変えても選択の自由は残す)
    def test_user_can_disable(self):
        self.assertEqual(
            timeline_config({"timeline": {"autosave_sec": 0}})["autosave_sec"], 0)

    def test_user_can_change_the_interval(self):
        self.assertEqual(
            timeline_config({"timeline": {"autosave_sec": 300}})["autosave_sec"], 300)

    # 壊れた値でも落ちない (0 = 無効へ倒す)
    def test_broken_values_fall_back_to_disabled(self):
        for value in (None, "", 0, False):
            with self.subTest(value=value):
                self.assertEqual(
                    timeline_config({"timeline": {"autosave_sec": value}})["autosave_sec"],
                    0)


if __name__ == "__main__":
    unittest.main()
