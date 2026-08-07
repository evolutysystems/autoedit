# TimeMap (src/timeline/timemap.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 元動画時間 ↔ Timeline 時間の写像 (resolve.md §6.3.2)。
import unittest

from src.timeline.model import Clip, ORIGIN_OPENING, ORIGIN_SILENCE_CUT
from src.timeline.timemap import TimeMap

# 無音カットの結果を模した残す区間 (元動画時間)
#   0.0〜12.48 / 15.22〜48.90 / 60.00〜70.00
# 取り除かれるのは 12.48〜15.22 と 48.90〜60.00
_KEEP = [(0.0, 12.48), (15.22, 48.90), (60.0, 70.0)]


class FromSegmentsTest(unittest.TestCase):

    def setUp(self):
        self.timemap = TimeMap.from_segments(_KEEP, media_id="m1")

    # Timeline 時間は区間尺の累積になる
    def test_timeline_is_cumulative(self):
        self.assertAlmostEqual(self.timemap.timeline_duration(), 12.48 + 33.68 + 10.0, places=6)

    # 第 2 区間の先頭は Timeline 上で第 1 区間の直後に来る
    def test_to_timeline_closes_the_gap(self):
        self.assertAlmostEqual(self.timemap.to_timeline(15.22), 12.48, places=6)
        self.assertAlmostEqual(self.timemap.to_timeline(20.22), 17.48, places=6)

    # 取り除いた区間 (無音) は写像先を持たない
    def test_cut_region_has_no_timeline_time(self):
        self.assertIsNone(self.timemap.to_timeline(13.0))
        self.assertIsNone(self.timemap.to_timeline(55.0))

    # 逆写像は (media_id, ソース時間) を返す
    def test_to_source_returns_media_and_time(self):
        self.assertEqual(self.timemap.to_source(0.0), ("m1", 0.0))
        media_id, source_sec = self.timemap.to_source(12.48)
        self.assertEqual(media_id, "m1")
        self.assertAlmostEqual(source_sec, 15.22, places=6)

    # 往復して元へ戻る
    def test_round_trip(self):
        for source_sec in (0.5, 12.0, 15.5, 48.0, 65.0):
            timeline_sec = self.timemap.to_timeline(source_sec)
            self.assertIsNotNone(timeline_sec, f"{source_sec} が写像できない")
            self.assertEqual(self.timemap.to_source(timeline_sec)[0], "m1")
            self.assertAlmostEqual(
                self.timemap.to_source(timeline_sec)[1], source_sec, places=6)

    # base_offset (OP の尺) を与えると全体が後ろへずれる
    def test_base_offset_shifts_everything(self):
        shifted = TimeMap.from_segments(_KEEP, media_id="m1", base_offset=4.0)
        self.assertAlmostEqual(shifted.to_timeline(0.0), 4.0, places=6)
        self.assertAlmostEqual(shifted.to_timeline(15.22), 16.48, places=6)


class SplitIntervalTest(unittest.TestCase):
    """カットを跨ぐ区間は Timeline 上の区間列へ分割される (字幕配置で使う)"""

    def setUp(self):
        self.timemap = TimeMap.from_segments(_KEEP, media_id="m1")

    # カットを跨がない区間は 1 つのまま
    def test_interval_inside_one_segment(self):
        pieces = self.timemap.split_interval(1.0, 3.0)
        self.assertEqual(len(pieces), 1)
        self.assertAlmostEqual(pieces[0][0], 1.0, places=6)
        self.assertAlmostEqual(pieces[0][1], 3.0, places=6)

    # カットを跨ぐ区間は Timeline 上で連続するため 1 つに統合される
    def test_interval_spanning_a_cut_becomes_contiguous(self):
        pieces = self.timemap.split_interval(12.0, 16.0)
        self.assertEqual(len(pieces), 1, "詰めた結果 Timeline 上では連続するはず")
        self.assertAlmostEqual(pieces[0][0], 12.0, places=6)
        # 12.0〜12.48 (0.48s) + 15.22〜16.0 (0.78s) = 1.26s ぶん
        self.assertAlmostEqual(pieces[0][1], 13.26, places=6)

    # 完全にカット内へ収まる区間は消える
    def test_interval_fully_inside_cut_disappears(self):
        self.assertEqual(self.timemap.split_interval(13.0, 14.0), [])

    # 逆転・ゼロ長の区間は空を返す
    def test_invalid_interval(self):
        self.assertEqual(self.timemap.split_interval(5.0, 5.0), [])
        self.assertEqual(self.timemap.split_interval(5.0, 4.0), [])


class FromClipsTest(unittest.TestCase):
    """編集後の Timeline (OP を含む) からの写像"""

    def setUp(self):
        self.clips = [
            Clip("c0", "m3", 0.0, 4.0, 0.0, 4.0, origin={"type": ORIGIN_OPENING}),
            Clip("c1", "m1", 4.0, 12.48, 0.0, 12.48, origin={"type": ORIGIN_SILENCE_CUT}),
            Clip("c2", "m1", 16.48, 33.68, 15.22, 48.9, origin={"type": ORIGIN_SILENCE_CUT}),
        ]
        self.timemap = TimeMap.from_clips(self.clips, body_media_id="m1")

    # OP 区間の再生ヘッドは OP 素材の時刻を返す (プレビューが正しい素材を出せる)
    def test_playhead_inside_opening_returns_opening_media(self):
        self.assertEqual(self.timemap.to_source(2.0), ("m3", 2.0))

    # 本編区間は元動画の時刻を返す
    def test_playhead_inside_body_returns_source_media(self):
        media_id, source_sec = self.timemap.to_source(5.0)
        self.assertEqual(media_id, "m1")
        self.assertAlmostEqual(source_sec, 1.0, places=6)

    # 本編の元動画時間は OP の尺ぶん後ろの Timeline 時間へ写る
    def test_body_source_maps_past_the_opening(self):
        self.assertAlmostEqual(self.timemap.to_timeline(0.0), 4.0, places=6)
        self.assertAlmostEqual(self.timemap.to_timeline(15.22), 16.48, places=6)

    # 再生ヘッド位置のクリップを引ける
    def test_clip_at(self):
        self.assertEqual(self.timemap.clip_at(2.0).id, "c0")
        self.assertEqual(self.timemap.clip_at(5.0).id, "c1")
        self.assertEqual(self.timemap.clip_at(20.0).id, "c2")

    # 空白 (どのクリップにも属さない時刻) は None
    def test_gap_returns_none(self):
        clips = [
            Clip("c1", "m1", 0.0, 5.0, 0.0, 5.0),
            Clip("c2", "m1", 10.0, 5.0, 20.0, 25.0),
        ]
        timemap = TimeMap.from_clips(clips, body_media_id="m1")
        self.assertIsNone(timemap.to_source(7.0))
        self.assertIsNone(timemap.clip_at(7.0))

    # body_media_id 省略時は総尺最大のメディアを本編とみなす
    def test_dominant_media_is_inferred(self):
        timemap = TimeMap.from_clips(self.clips)
        self.assertAlmostEqual(timemap.to_timeline(0.0), 4.0, places=6)


if __name__ == "__main__":
    unittest.main()
