# 元 VOD の寿命管理の単体テスト (docs/error/fromUser/resolve.md §6-6)
# 実行: python -m unittest discover -s tests
#
# 方針: 「続きから編集するのに要るものはプロジェクトのフォルダへ揃える。
#        揃ったら元 VOD は手放す」
#
# 重点:
#   ・プロジェクトが**自己完結してから**しか捨てないこと
#     (音声サイドカーしか無い = 開くとき元 VOD から切り直す = まだ要る)
#   ・捨てるときはゴミ箱へ送ること (完全削除はしない)
#   ・「元動画を残す」を選んだ利用者のものは捨てないこと
import json
import os
import tempfile
import unittest
from unittest import mock

from src.archive import vod_retention


def _write(path, size=16):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"x" * size)
    return path


class VodRetentionTest(unittest.TestCase):

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = self._temp.name
        self.work = os.path.join(self.root, "archive_work")
        self.projects = os.path.join(self.root, "projects")
        os.makedirs(self.work, exist_ok=True)
        os.makedirs(self.projects, exist_ok=True)
        self.vod = _write(os.path.join(self.work, "vod_777.mp4"), size=4096)
        self.settings = {
            "archive": {"download": {"work_dir": self.work}},
            "general": {"output_directory": self.projects},
            "timeline": {},
        }

    def tearDown(self):
        self._temp.cleanup()

    # 保存名の決まりが「編集画面が保存する名前」と一致していること。
    # ここがズレると、在るプロジェクトを見落として VOD を消してしまう。
    def test_project_path_matches_the_dialog_naming(self):
        path = vod_retention.project_path_for(self.vod, self.settings)
        self.assertEqual(os.path.basename(path), "vod_777.archive.timeline.json")
        self.assertEqual(os.path.dirname(path), self.projects)

    def _make_project(self, media_names):
        path = os.path.join(self.projects, "vod_777.archive.timeline.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 1}, handle)
        media_dir = os.path.join(self.projects, "vod_777.archive.media")
        for name in media_names:
            _write(os.path.join(media_dir, name))
        return path

    # プロジェクトが無ければ当然まだ要る
    def test_keeps_the_vod_without_a_project(self):
        self.assertEqual(vod_retention.find_unneeded(self.settings), [])

    # 音声サイドカーだけでは不足 (開くとき元 VOD から映像を切り直すため)
    def test_audio_only_project_is_not_self_contained(self):
        self._make_project(["m1_audio.m4a", "m2_audio.m4a"])
        self.assertEqual(vod_retention.find_unneeded(self.settings), [])

    # 映像まで在れば自己完結 = 元 VOD は不要
    def test_project_with_video_makes_the_vod_unneeded(self):
        self._make_project(["m1_audio.m4a", "m1_normalized.mp4"])
        found = vod_retention.find_unneeded(self.settings)
        self.assertEqual([item["path"] for item in found], [self.vod])
        self.assertEqual(found[0]["bytes"], 4096)

    # 捨てるときはゴミ箱へ (os.remove は使わない)
    def test_sweep_sends_to_the_trash(self):
        self._make_project(["m1_normalized.mp4"])
        with mock.patch("src.utils.trash.is_available", return_value=True), \
                mock.patch("src.utils.trash.send_to_trash",
                           return_value={"trashed": [self.vod], "failed": []}) as sent, \
                mock.patch("os.remove", side_effect=AssertionError("完全削除は禁止")):
            freed = vod_retention.sweep_unneeded(self.settings)
        sent.assert_called_once_with([self.vod])
        self.assertEqual(freed, 4096)

    # 「元動画を残す」を選んだ利用者のものは触らない (セクション追加を使い続けられる)
    def test_keep_vod_setting_disables_the_sweep(self):
        self._make_project(["m1_normalized.mp4"])
        self.settings["archive"]["download"]["keep_vod"] = True
        with mock.patch("src.utils.trash.send_to_trash") as sent:
            freed = vod_retention.sweep_unneeded(self.settings)
        self.assertEqual(freed, 0)
        sent.assert_not_called()
        self.assertTrue(os.path.exists(self.vod))

    # ゴミ箱を使えない環境では何もしない
    def test_without_trash_nothing_is_deleted(self):
        self._make_project(["m1_normalized.mp4"])
        with mock.patch("src.utils.trash.is_available", return_value=False):
            self.assertEqual(vod_retention.sweep_unneeded(self.settings), 0)
        self.assertTrue(os.path.exists(self.vod))

    # チャット JSON は採点に使うため対象外 (小さく、取り直しも面倒)
    def test_chat_json_is_never_swept(self):
        _write(os.path.join(self.work, "chat_777.json"))
        self._make_project(["m1_normalized.mp4"])
        found = [os.path.basename(item["path"])
                 for item in vod_retention.find_unneeded(self.settings)]
        self.assertEqual(found, ["vod_777.mp4"])


if __name__ == "__main__":
    unittest.main()
