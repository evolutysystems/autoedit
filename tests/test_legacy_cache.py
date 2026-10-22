# 旧版が残した取得データの回収の単体テスト
# (docs/error/fromUser/resolve.md §6-6-6 / §8-2 7-7)
# 実行: python -m unittest discover -s tests
#
# 重点:
#   ・%LOCALAPPDATA%\twitch-dl は**実在するときだけ**対象にすること
#     (他ツールと共用しうる場所。無いものを作ったり仮定したりしない)
#   ・小さいものでは知らせないこと (毎回の起動で煩わせない)
#   ・回収はゴミ箱経由。完全削除はしないこと
#   ・作業フォルダの基準がインストール先の外になったこと
import os
import tempfile
import unittest
from unittest import mock

from src.archive import config, legacy_cache


class TwitchdlCacheDirTest(unittest.TestCase):

    # 実在しなければ空文字 (「無いのに在ることにする」をしない)
    def test_missing_folder_returns_empty(self):
        with tempfile.TemporaryDirectory() as base:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": base}):
                self.assertEqual(legacy_cache.twitchdl_cache_dir(), "")

    def test_existing_folder_is_returned(self):
        with tempfile.TemporaryDirectory() as base:
            path = os.path.join(base, "twitch-dl")
            os.makedirs(path)
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": base}):
                self.assertEqual(legacy_cache.twitchdl_cache_dir(), path)


class FindReclaimableTest(unittest.TestCase):

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.base = self._temp.name

    def tearDown(self):
        self._temp.cleanup()

    def _make_cache(self, size):
        path = os.path.join(self.base, "twitch-dl", "videos", "1")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "part.ts"), "wb") as handle:
            handle.write(b"x" * size)

    # 大きければ回収候補に挙がる
    def test_large_cache_is_reported(self):
        self._make_cache(4096)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.base}), \
                mock.patch.object(legacy_cache, "legacy_work_dir", return_value=""):
            found = legacy_cache.find_reclaimable({}, min_bytes=1024)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["bytes"], 4096)

    # 小さければ知らせない (起動のたびに尋ねられる方が煩わしい)
    def test_small_cache_is_ignored(self):
        self._make_cache(10)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.base}), \
                mock.patch.object(legacy_cache, "legacy_work_dir", return_value=""):
            self.assertEqual(legacy_cache.find_reclaimable({}, min_bytes=1024), [])

    # 無ければ何も言わない
    def test_nothing_found(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.base}), \
                mock.patch.object(legacy_cache, "legacy_work_dir", return_value=""):
            self.assertEqual(legacy_cache.find_reclaimable({}, min_bytes=1), [])

    # 旧い作業フォルダも対象に含む (大きい順に並ぶ)
    def test_legacy_work_dir_is_included_and_sorted(self):
        self._make_cache(1024)
        legacy = os.path.join(self.base, "old_archive_work")
        os.makedirs(legacy)
        with open(os.path.join(legacy, "vod_1.mp4"), "wb") as handle:
            handle.write(b"x" * 8192)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.base}), \
                mock.patch.object(legacy_cache, "legacy_work_dir", return_value=legacy):
            found = legacy_cache.find_reclaimable({}, min_bytes=512)
        self.assertEqual([item["bytes"] for item in found], [8192, 1024])


class ReclaimTest(unittest.TestCase):

    # ゴミ箱へ送る。完全削除はしない。
    def test_sends_to_trash(self):
        items = [{"path": r"C:\x\twitch-dl", "bytes": 100, "label": "x"}]
        with mock.patch("src.utils.trash.is_available", return_value=True), \
                mock.patch("src.utils.trash.send_to_trash",
                           return_value={"trashed": [r"C:\x\twitch-dl"],
                                         "failed": []}) as sent, \
                mock.patch("shutil.rmtree", side_effect=AssertionError("完全削除は禁止")):
            freed = legacy_cache.reclaim(items)
        sent.assert_called_once()
        self.assertEqual(freed, 100)

    # ゴミ箱が使えなければ何もしない
    def test_without_trash_nothing_happens(self):
        items = [{"path": r"C:\x", "bytes": 100, "label": "x"}]
        with mock.patch("src.utils.trash.is_available", return_value=False):
            self.assertEqual(legacy_cache.reclaim(items), 0)

    def test_empty_input(self):
        self.assertEqual(legacy_cache.reclaim([]), 0)


class WorkBaseDirTest(unittest.TestCase):

    # 作業フォルダの基準がインストール先の外 (%LOCALAPPDATA%\Stretheus) になったこと
    def test_base_is_outside_the_install_folder(self):
        with tempfile.TemporaryDirectory() as base:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": base}):
                self.assertEqual(config.work_base_dir(),
                                 os.path.join(base, "Stretheus"))

    # 絶対指定の利用者は移行の対象外 (自分で決めている)
    def test_absolute_work_dir_has_no_legacy(self):
        settings = {"archive": {"download": {"work_dir": r"D:\mywork"}}}
        self.assertEqual(config.legacy_work_dir(settings), "")

    # 相対指定なら旧い場所 (src/settings 配下) を指す
    def test_relative_work_dir_points_at_the_old_place(self):
        legacy = config.legacy_work_dir({})
        self.assertTrue(legacy.endswith(os.path.join("settings", "archive_work")))


if __name__ == "__main__":
    unittest.main()
