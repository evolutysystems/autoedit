# 区間取得キャッシュの使い回しで「区間が途切れています」になる件 (docs/error/20261125b)
# 実行: python -m unittest discover -s tests
#
# 要件 (docs/error/20261125b/resolve.md §6):
#   ・取得する区間が前回と違えばキャッシュを捨てる (本件の修正)
#   ・**同じ区間なら残す** = 再試行が前回の続きから進む (error 20261118 §9 R5)
#   ・控えが無いフォルダ (前のセッションの残り) も捨てる
# ネットワークも ffmpeg も呼ばない。
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import vod_range


class ResetCacheForWindowTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self._dir.name, "vod_range_13")
        os.makedirs(os.path.join(self.cache, "2938439428", "chunked"))

    def tearDown(self):
        self._dir.cleanup()

    # 前回の取得が残したセグメントを模す
    def _leftovers(self, *indexes):
        folder = os.path.join(self.cache, "2938439428", "chunked")
        # 捨てたあとは消えているので作り直す (取得のたびに twitch-dl が作る)
        os.makedirs(folder, exist_ok=True)
        for index in indexes:
            with open(os.path.join(folder, "%05d.mp4" % index), "wb") as handle:
                handle.write(b"seg")

    def _segments(self):
        folder = os.path.join(self.cache, "2938439428", "chunked")
        if not os.path.isdir(folder):
            return []

        return sorted(os.listdir(folder))

    # T1: 控えが無いフォルダは中身ごと捨てる
    def test_cache_without_a_record_is_cleared(self):
        self._leftovers(334, 335, 336)
        self.assertTrue(vod_range.reset_cache_for_window(self.cache, 4227.0, 6306.0))
        self.assertEqual(self._segments(), [])

    # T2: 同じ区間なら残す (再試行は前回の続きから / 20261118 §9 R5)
    def test_same_window_keeps_the_cache(self):
        vod_range.reset_cache_for_window(self.cache, 4227.0, 6306.0)
        self._leftovers(422, 423, 424)
        self.assertFalse(vod_range.reset_cache_for_window(self.cache, 4227.0, 6306.0))
        self.assertEqual(self._segments(),
                         ["00422.mp4", "00423.mp4", "00424.mp4"])

    # T3: 違う区間なら中身ごと捨てる (本件の再現と修正)
    def test_different_window_clears_the_cache(self):
        vod_range.reset_cache_for_window(self.cache, 3347.0, 3363.0)
        self._leftovers(334, 335, 336)
        self.assertTrue(vod_range.reset_cache_for_window(self.cache, 4227.0, 4263.0))
        self.assertEqual(self._segments(), [])

    # T4: 捨てたあともフォルダは残り、控えが今回の区間で書き直される
    def test_record_is_rewritten(self):
        vod_range.reset_cache_for_window(self.cache, 3347.0, 3363.0)
        vod_range.reset_cache_for_window(self.cache, 4227.0, 4263.0)
        self.assertTrue(os.path.isdir(self.cache))
        self.assertEqual(vod_range._read_window(self.cache), (4227.0, 4263.0))
        # 書き直したあとは「同じ区間」と判定される
        self.assertFalse(vod_range.reset_cache_for_window(self.cache, 4227.0, 4263.0))

    # T5: 控えを書けなくても例外にしない (取得は続ける)
    def test_write_failure_does_not_raise(self):
        with mock.patch("builtins.open", side_effect=OSError("書けない")):
            vod_range._write_window(self.cache, (1.0, 2.0))

    # T6: 丸め違いでは取り直しにならない (0.5 秒の許容)
    def test_small_difference_is_the_same_window(self):
        vod_range.reset_cache_for_window(self.cache, 4227.0, 6306.0)
        self._leftovers(422)
        self.assertFalse(vod_range.reset_cache_for_window(self.cache, 4227.2, 6305.8))
        self.assertEqual(self._segments(), ["00422.mp4"])
        # 許容を超えれば捨てる
        self.assertTrue(vod_range.reset_cache_for_window(self.cache, 4228.0, 6306.0))
        self.assertEqual(self._segments(), [])

    # 壊れた控えは「控えが無い」と同じ扱い
    def test_broken_record_is_treated_as_missing(self):
        with open(os.path.join(self.cache, vod_range._WINDOW_NAME), "w",
                  encoding="utf-8") as handle:
            handle.write("こわれている")
        self._leftovers(334)
        self.assertTrue(vod_range.reset_cache_for_window(self.cache, 4227.0, 4263.0))
        self.assertEqual(self._segments(), [])

    # フォルダがまだ無くても落ちない
    def test_missing_folder_is_created(self):
        fresh = os.path.join(self._dir.name, "vod_range_99")
        self.assertTrue(vod_range.reset_cache_for_window(fresh, 1.0, 2.0))
        self.assertEqual(vod_range._read_window(fresh), (1.0, 2.0))


class VerifyContinuousTest(unittest.TestCase):
    """検査そのものは緩めない (resolve.md §2.3)"""

    # 本件で出た並び。残骸が混ざったら必ず止める
    def test_mixed_segments_are_rejected(self):
        from src.exceptions import TwitchError

        names = ["00334.mp4", "00335.mp4", "00336.mp4", "00422.mp4", "00423.mp4"]
        with self.assertRaises(TwitchError) as caught:
            vod_range.verify_continuous(names)
        self.assertIn("336", str(caught.exception))
        self.assertIn("422", str(caught.exception))

    # 連番なら通る
    def test_continuous_segments_pass(self):
        self.assertEqual(
            vod_range.verify_continuous(["00422.mp4", "00423.mp4", "00424.mp4"]),
            [422, 423, 424])


if __name__ == "__main__":
    unittest.main()
