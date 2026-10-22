# 保存のたびに素材が増えないことの単体テスト (docs/error/fromUser/resolve.md §9-5)
# 実行: python -m unittest discover -s tests
#
# 背景 (2026-10-22 の実機確認):
#   アーカイブプロジェクトを 2 回保存すると、.media フォルダに
#   m1_normalized.mp4 と m1_m1_normalized.mp4 の両方が出来ていた。
#   複製後は media.path を複製先へ張り替えるため、次の保存では
#   「複製先の名前」をもう一度組み立てて接頭辞が積み重なっていた。
#   1 回の保存で素材 1 本ぶん (数百 MB) が無駄に増える。
#
# 重点: 2 回目以降の保存で複製し直さないこと。1 回目は従来どおり複製すること。
import os
import tempfile
import types
import unittest
from unittest import mock

from src.gui.timeline import timeline_editor_dialog as ted


class _Media:

    def __init__(self, media_id, path):
        self.id = media_id
        self.path = path


class _Timeline:

    def __init__(self):
        self.source = {}


# 複製処理だけを取り出して試す (Qt を起動しない)
class _Dialog:
    _copy_media_beside_project = ted.TimelineEditorDialog._copy_media_beside_project

    def __init__(self, folder, media):
        self.controller = types.SimpleNamespace(timeline=_Timeline())
        self.preview = types.SimpleNamespace(set_status=lambda *_a: None)
        self._folder = folder
        self._media = media

    def _media_to_copy(self):
        return self._media

    def _media_dir(self, _project_path):
        return self._folder


class CopyMediaBesideProjectTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.folder = os.path.join(self.root, "proj.media")
        self.source = os.path.join(self.root, "normalized.mp4")
        with open(self.source, "wb") as f:
            f.write(b"x" * 1024)
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(ted, "QApplication", mock.MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, media):
        dialog = _Dialog(self.folder, media)
        dialog._copy_media_beside_project(os.path.join(self.root, "proj.timeline.json"))

    # 1 回目は複製する (従来どおり)
    def test_copies_once(self):
        media = _Media("m1", self.source)
        self._run([media])

        self.assertEqual(os.listdir(self.folder), ["m1_normalized.mp4"])
        self.assertEqual(media.path, os.path.join(self.folder, "m1_normalized.mp4"))

    # 2 回目は複製し直さない (本題。m1_m1_... を作らない)
    def test_does_not_copy_again_on_the_second_save(self):
        media = _Media("m1", self.source)
        self._run([media])
        first = media.path

        self._run([media])              # 同じ素材でもう一度保存する

        self.assertEqual(os.listdir(self.folder), ["m1_normalized.mp4"])
        self.assertEqual(media.path, first)

    # 何度保存しても増えない
    def test_stays_stable_over_many_saves(self):
        media = _Media("m1", self.source)
        for _ in range(5):
            self._run([media])

        self.assertEqual(len(os.listdir(self.folder)), 1)


if __name__ == "__main__":
    unittest.main()
