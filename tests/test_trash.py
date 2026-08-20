# ゴミ箱への移動 (src/utils/trash.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver3/resolve9.md §8.1 / 回答 Q2):
#   ・一時ファイル・フォルダをゴミ箱へ送れること (Windows のみ)
#   ・存在しないパス・長すぎるパスが failed に入り、例外にならないこと
#   ・非 Windows では is_available() が False で、全件 failed になること
import os
import shutil
import tempfile
import unittest
from unittest import mock

from src.utils import trash


class TrashTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trash_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _touch(self, name):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(b"0" * 16)
        return path

    @unittest.skipUnless(os.name == "nt", "ゴミ箱は Windows のみ")
    def test_sends_file_and_folder_to_trash(self):
        target_file = self._touch("sample.timeline.json")
        target_dir = os.path.join(self.tmp, "sample.media")
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, "m1_audio.m4a"), "wb") as f:
            f.write(b"0" * 16)

        result = trash.send_to_trash([target_dir, target_file])
        self.assertEqual(result["failed"], [])
        self.assertEqual(len(result["trashed"]), 2)
        self.assertFalse(os.path.exists(target_file))
        self.assertFalse(os.path.isdir(target_dir))

    def test_missing_path_is_reported_not_raised(self):
        result = trash.send_to_trash([os.path.join(self.tmp, "no_such.json")])
        self.assertEqual(result["trashed"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("見つかりません", result["failed"][0]["reason"])

    def test_too_long_path_is_reported(self):
        # SHFileOperationW は MAX_PATH を超えるパスを扱えない
        long_path = os.path.join(self.tmp, "x" * 300)
        result = trash.send_to_trash([long_path])
        self.assertEqual(result["trashed"], [])
        self.assertEqual(len(result["failed"]), 1)

    def test_empty_input(self):
        result = trash.send_to_trash([])
        self.assertEqual(result, {"trashed": [], "failed": []})

    def test_unavailable_platform_reports_failure(self):
        target = self._touch("keep.json")
        with mock.patch.object(trash, "is_available", return_value=False):
            result = trash.send_to_trash([target])
        self.assertEqual(result["trashed"], [])
        self.assertEqual(len(result["failed"]), 1)
        # 送れなかったものは消さない (完全削除するかは呼び出し側が確認する)
        self.assertTrue(os.path.exists(target))

    def test_is_available_false_on_non_windows(self):
        with mock.patch.object(os, "name", "posix"):
            self.assertFalse(trash.is_available())


if __name__ == "__main__":
    unittest.main()
