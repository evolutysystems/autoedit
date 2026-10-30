# 縦動画プロジェクトの素材複製 (ver9 resolve14) の単体テスト
# 実行: python -m unittest discover -s tests
#
# 背景:
#   アーカイブ用 Timeline の素材は、未保存なら一時ファイル、保存済みでも
#   元プロジェクトの .media/ にある。従来は作成時に元 VOD 1 本へ張り替えて
#   いたが、元 VOD は既定で自動整理されるため
#     ・元 VOD が無いとメニューごと消える
#     ・張り替えた縦プロジェクトも、元 VOD が消えると開けない
#   という穴があった。素材を縦プロジェクトの隣へ複製して自己完結させる。
#
# 重点:
#   ・複製したら media.path が <縦プロジェクト>.media/ を指すこと
#   ・元の Timeline は変わらないこと (deepcopy した側だけを書き換える)
#   ・消えない素材 (利用者の元動画・元 VOD) は複製しないこと
#   ・2 回目は複製し直さないこと / 失敗しても作成を止めないこと
import os
import tempfile
import unittest

from src.timeline import crop, vertical_builder
from src.timeline.model import MediaRef, Timeline

_SETTINGS = {"vertical": {"crop": {}}}


def _media(media_id, path):
    return MediaRef(media_id, "video", path, duration_sec=10.0,
                    width=1920, height=1080, fps=60.0, has_audio=True)


def _write(path, size=32):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"0" * size)
    return path


class CopyMediaBesideTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.project = os.path.join(self.root, "clip_vertical.timeline.json")
        self.media_dir = os.path.join(self.root, "clip_vertical.media")
        self.source = _write(os.path.join(self.root, "work", "m1_normalized.mp4"))

    def _timeline(self, media_id="m1", path=None):
        timeline = Timeline()
        timeline.media_pool = [_media(media_id, path or self.source)]
        timeline.source = {"media_id": media_id, "media_path": path or self.source}
        return timeline

    # 本題: 素材が縦プロジェクトの隣へ来て、参照もそこへ向く
    def test_copies_and_repoints(self):
        timeline = self._timeline()

        copied, total = vertical_builder.copy_media_beside(
            timeline, self.project, _SETTINGS)

        dest = os.path.join(self.media_dir, "m1_m1_normalized.mp4")
        self.assertEqual(copied, 1)
        self.assertGreater(total, 0)
        self.assertTrue(os.path.exists(dest))
        self.assertEqual(timeline.media_pool[0].path, dest)
        # 本編素材なら source の参照も合わせる (開き直しで使う)
        self.assertEqual(timeline.source["media_path"], dest)

    # 元のファイルは残る (元プロジェクトは壊さない)
    def test_source_file_is_kept(self):
        vertical_builder.copy_media_beside(self._timeline(), self.project, _SETTINGS)
        self.assertTrue(os.path.exists(self.source))

    # 2 回目は複製し直さない (同じ大きさならそのまま)
    def test_second_run_does_not_copy_again(self):
        timeline = self._timeline()
        vertical_builder.copy_media_beside(timeline, self.project, _SETTINGS)

        again = self._timeline()
        copied, _total = vertical_builder.copy_media_beside(
            again, self.project, _SETTINGS)

        self.assertEqual(copied, 0)
        # 複製し直さなくても参照は複製先へ向く
        self.assertEqual(again.media_pool[0].path,
                         os.path.join(self.media_dir, "m1_m1_normalized.mp4"))

    # 消えない素材 (利用者の元動画・張り替え先の元 VOD) は複製しない
    def test_skips_the_kept_paths(self):
        timeline = self._timeline()

        copied, _total = vertical_builder.copy_media_beside(
            timeline, self.project, _SETTINGS, skip_paths=[self.source])

        self.assertEqual(copied, 0)
        self.assertEqual(timeline.media_pool[0].path, self.source)

    # 素材が既に消えているときは警告だけ積み、作成は続ける
    def test_missing_media_is_reported(self):
        timeline = self._timeline(path=os.path.join(self.root, "gone.mp4"))
        warnings = []

        copied, _total = vertical_builder.copy_media_beside(
            timeline, self.project, _SETTINGS, warnings=warnings)

        self.assertEqual(copied, 0)
        self.assertEqual(len(warnings), 1)
        self.assertIn("gone.mp4", warnings[0])

    # 複製に失敗しても例外にしない (元のパスを指したまま保存できる)
    def test_copy_failure_does_not_raise(self):
        from unittest import mock

        timeline = self._timeline()
        warnings = []
        with mock.patch.object(vertical_builder.shutil, "copy2",
                               side_effect=OSError("disk full")):
            copied, _total = vertical_builder.copy_media_beside(
                timeline, self.project, _SETTINGS, warnings=warnings)

        self.assertEqual(copied, 0)
        self.assertEqual(timeline.media_pool[0].path, self.source)
        self.assertEqual(len(warnings), 1)

    # 素材が複数でも全部ついてくる
    def test_copies_every_media(self):
        timeline = self._timeline()
        second = _write(os.path.join(self.root, "work", "m2_normalized.mp4"))
        timeline.media_pool.append(_media("m2", second))

        copied, _total = vertical_builder.copy_media_beside(
            timeline, self.project, _SETTINGS)

        self.assertEqual(copied, 2)
        self.assertTrue(all(path.startswith(self.media_dir)
                            for path in (m.path for m in timeline.media_pool)))


class CropConfigTest(unittest.TestCase):

    # 既定は複製 (自己完結)
    def test_defaults(self):
        cfg = crop.config({})
        self.assertEqual(cfg["media_mode"], crop.MEDIA_COPY)
        self.assertTrue(cfg["keep_media"])

    # 従来の張り替えへ戻せる
    def test_rebase_mode(self):
        cfg = crop.config({"vertical": {"crop": {"media_mode": "rebase"}}})
        self.assertEqual(cfg["media_mode"], crop.MEDIA_REBASE)

    # 未知の値は既定へ寄せる (設定の書き間違いで挙動を変えない)
    def test_unknown_mode_falls_back(self):
        cfg = crop.config({"vertical": {"crop": {"media_mode": "どこか"}}})
        self.assertEqual(cfg["media_mode"], crop.MEDIA_COPY)

    def test_keep_media_can_be_disabled(self):
        cfg = crop.config({"vertical": {"crop": {"keep_media": False}}})
        self.assertFalse(cfg["keep_media"])


if __name__ == "__main__":
    unittest.main()
