# 空き容量ユーティリティの単体テスト (docs/error/fromUser/resolve.md §7)
# 実行: python -m unittest discover -s tests
# 重点:
#   ・まだ無いパスでも親まで遡って測れること (開始前の確認で使うため)
#   ・ENOSPC / ERROR_DISK_FULL を「容量不足」と判定できること
#   ・不足なら DiskSpaceError、設定 0 なら確認しないこと
#   ・測れない環境では止めないこと (測れないことを理由に機能を奪わない)
import errno
import os
import tempfile
import unittest
from unittest import mock

from src.exceptions import DiskSpaceError
from src.utils import disk


class FreeBytesTest(unittest.TestCase):

    # 実在するフォルダはそのまま測れる
    def test_existing_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsInstance(disk.free_bytes(folder), int)

    # まだ作っていない深い場所でも、実在する親まで遡って測る
    def test_missing_path_walks_up_to_an_existing_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            deep = os.path.join(folder, "not", "created", "yet")
            self.assertEqual(disk.free_bytes(deep), disk.free_bytes(folder))

    # どうしても測れないときは None (呼び出し側は「確認しない」へ倒す)
    def test_unmeasurable_returns_none(self):
        with mock.patch("shutil.disk_usage", side_effect=OSError):
            self.assertIsNone(disk.free_bytes(os.getcwd()))


class IsNoSpaceTest(unittest.TestCase):

    def test_enospc(self):
        self.assertTrue(disk.is_no_space(OSError(errno.ENOSPC, "No space left on device")))

    # Windows は copy2 経由などで errno が ENOSPC にならないことがある
    def test_windows_disk_full(self):
        error = OSError(0, "disk full")
        error.winerror = 112
        self.assertTrue(disk.is_no_space(error))

    def test_other_oserror_is_not_disk_full(self):
        self.assertFalse(disk.is_no_space(OSError(errno.EACCES, "denied")))

    def test_diskspace_error_itself(self):
        self.assertTrue(disk.is_no_space(DiskSpaceError("足りません")))

    # 包み直された例外も 1 段だけ辿る
    def test_wrapped_cause(self):
        try:
            try:
                raise OSError(errno.ENOSPC, "No space left on device")
            except OSError as inner:
                raise RuntimeError("wrapped") from inner
        except RuntimeError as outer:
            self.assertTrue(disk.is_no_space(outer))


class LooksLikeNoSpaceTest(unittest.TestCase):

    def test_ffmpeg_phrases(self):
        for text in ("av_interleaved_write_frame(): No space left on device",
                     "Error: There is not enough space on the disk.",
                     "ENOSPC"):
            with self.subTest(text=text):
                self.assertTrue(disk.looks_like_no_space(text))

    def test_unrelated_stderr(self):
        self.assertFalse(disk.looks_like_no_space(
            "Invalid data found when processing input"))

    def test_empty(self):
        self.assertFalse(disk.looks_like_no_space(""))
        self.assertFalse(disk.looks_like_no_space(None))


class EnsureFreeTest(unittest.TestCase):

    def test_raises_when_short(self):
        with mock.patch.object(disk, "free_bytes", return_value=1024):
            with self.assertRaises(DiskSpaceError) as caught:
                disk.ensure_free(os.getcwd(), 10 * disk.GIB, label="テスト")
        self.assertEqual(caught.exception.free, 1024)
        self.assertEqual(caught.exception.required, 10 * disk.GIB)

    def test_passes_when_enough(self):
        with mock.patch.object(disk, "free_bytes", return_value=100 * disk.GIB):
            disk.ensure_free(os.getcwd(), disk.GIB)   # 例外が出なければ合格

    # 設定 0 = 確認しない (従来どおり動かしたい利用者のため)
    def test_zero_requirement_skips_the_check(self):
        with mock.patch.object(disk, "free_bytes") as measured:
            disk.ensure_free(os.getcwd(), 0)
        measured.assert_not_called()

    # 測れない環境では止めない
    def test_unmeasurable_does_not_block(self):
        with mock.patch.object(disk, "free_bytes", return_value=None):
            disk.ensure_free(os.getcwd(), 10 * disk.GIB)


class RequiredFromGbTest(unittest.TestCase):

    def test_converts(self):
        self.assertEqual(disk.required_from_gb(20), 20 * disk.GIB)

    def test_zero_and_negative_and_broken(self):
        for value in (0, -5, None, "", "abc"):
            with self.subTest(value=value):
                self.assertEqual(disk.required_from_gb(value), 0)


class HumanTest(unittest.TestCase):

    def test_units(self):
        self.assertEqual(disk.human(512), "512 B")
        self.assertEqual(disk.human(2 * 1024), "2.0 KB")
        self.assertEqual(disk.human(3 * disk.GIB), "3.0 GB")
        self.assertEqual(disk.human(None), "?")


if __name__ == "__main__":
    unittest.main()
