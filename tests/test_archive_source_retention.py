# 取得物の保持と削除の単体テスト (docs/error/fromUser/resolve.md §6-6)
# 実行: python -m unittest discover -s tests
#
# 背景: 実装中に、アーカイブ画面を作っただけで自動削除が走り、
# 開発端末の取得物 45 GB を os.remove で消してしまった。
# 「取得に何時間もかかる素材を、画面を開いただけで・取り消せない形で消す」
# という二重の危うさがあったため、次の 2 点を不変条件としてここで守る:
#   ・既定 (keep_source_days=0) では**何も消さない**
#   ・消すときは必ずゴミ箱へ送る (ゴミ箱が使えないなら消さない)
import os
import tempfile
import time
import unittest
from unittest import mock

from src.archive import config


def _write(path, size=16, age_days=0.0):
    with open(path, "wb") as handle:
        handle.write(b"x" * size)
    if age_days:
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
    return path


class SourceUsageTest(unittest.TestCase):

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.work = self._temp.name
        self.settings = {"archive": {"download": {"work_dir": self.work}}}

    def tearDown(self):
        self._temp.cleanup()

    def test_counts_only_downloaded_sources(self):
        _write(os.path.join(self.work, "vod_1.mp4"), size=100)
        _write(os.path.join(self.work, "chat_1.json"), size=50)
        _write(os.path.join(self.work, "twitch_token.json"), size=999)  # 対象外
        usage = config.source_usage(self.settings)
        self.assertEqual(usage["files"], 2)
        self.assertEqual(usage["bytes"], 150)

    # 保存済みプロジェクトや書き出したファイルを巻き込まない。
    # 消してよいのは「取り直せるもの」だけであり、
    # vod_<id>.archive.json は利用者の編集結果そのもので取り直せない。
    def test_never_touches_projects_or_exports(self):
        for name in ("vod_1.archive.json", "vod_1.archive.fcpxml",
                     "vod_1.archive.srt", "archive_vod_1.srt",
                     "vod_1.mp4.part"):
            _write(os.path.join(self.work, name), size=10)
        usage = config.source_usage(self.settings)
        self.assertEqual(usage["files"], 0)


class SweepOldSourcesTest(unittest.TestCase):

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.work = self._temp.name
        self.old = _write(os.path.join(self.work, "vod_1001.mp4"), age_days=90)
        self.new = _write(os.path.join(self.work, "vod_2002.mp4"), age_days=1)

    def tearDown(self):
        self._temp.cleanup()

    def _settings(self, days):
        return {"archive": {"download": {"work_dir": self.work,
                                         "keep_source_days": days}}}

    # 既定 (0) では何も消さない。画面を開いただけで素材が消えてはいけない。
    def test_default_keeps_everything(self):
        with mock.patch("src.utils.trash.send_to_trash") as sent:
            freed = config.sweep_old_sources(self._settings(0))
        self.assertEqual(freed, 0)
        sent.assert_not_called()
        self.assertTrue(os.path.exists(self.old))

    # 有効にしたときだけ、古いものをゴミ箱へ送る (完全削除はしない)
    def test_enabled_sends_old_files_to_the_trash(self):
        with mock.patch("src.utils.trash.is_available", return_value=True), \
                mock.patch("src.utils.trash.send_to_trash",
                           return_value={"trashed": [self.old], "failed": []}) as sent:
            freed = config.sweep_old_sources(self._settings(7))
        sent.assert_called_once_with([self.old])      # 新しい方は渡さない
        self.assertEqual(freed, 16)

    # ゴミ箱が使えない環境では何もしない (黙って完全削除はしない)
    def test_without_trash_nothing_is_deleted(self):
        with mock.patch("src.utils.trash.is_available", return_value=False):
            freed = config.sweep_old_sources(self._settings(7))
        self.assertEqual(freed, 0)
        self.assertTrue(os.path.exists(self.old))

    # os.remove を使わないこと (取り消せない削除をしない) を直接守る
    def test_never_calls_os_remove(self):
        with mock.patch("src.utils.trash.is_available", return_value=True), \
                mock.patch("src.utils.trash.send_to_trash",
                           return_value={"trashed": [self.old], "failed": []}), \
                mock.patch("os.remove", side_effect=AssertionError("完全削除は禁止")):
            config.sweep_old_sources(self._settings(7))


class DownloadConfigDefaultTest(unittest.TestCase):

    # 既定は無期限 (自動削除は利用者が明示的に有効化したときだけ)
    def test_keep_source_days_defaults_to_zero(self):
        self.assertEqual(config.download_config({})["keep_source_days"], 0)

    def test_keep_cache_on_error_defaults_to_false(self):
        self.assertFalse(config.download_config({})["keep_cache_on_error"])


if __name__ == "__main__":
    unittest.main()
