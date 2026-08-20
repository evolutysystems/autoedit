# 認識時刻の写像 (src/modules/subtitle_generator.map_items_to_timeline) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/error/20260812/resolve2.md §10-1):
#   ・素材そのままの認識で得た時刻が「残す区間を詰めた軸」へ正しく写ること (I1)
#   ・カットを跨ぐ字幕が分割されず 1 区間へまとまること
#   ・カット区間へ落ちた字幕が除外されること (幻聴字幕もここで落ちる)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.modules import subtitle_generator

# 素材 30 秒のうち [0,5) と [10,15) を残す = 詰めた後は 10 秒
_KEEP = [(0.0, 5.0), (10.0, 15.0)]


# item 1 件ぶんの入力を作る (写像は時刻以外のキーをそのまま持ち越す)
def _item(start, end, text="あ"):
    return {"start": start, "end": end, "text": text,
            "use": True, "role": "streamer", "font": "", "font_size": None}


class MapItemsToTimelineTest(unittest.TestCase):

    def _map(self, items, keep=None):
        return subtitle_generator.map_items_to_timeline(
            items, _KEEP if keep is None else keep)

    # 無音カット無効 (全長 1 区間) では時刻が変わらない
    def test_identity_when_nothing_is_cut(self):
        mapped, dropped = self._map([_item(3.0, 7.0)], keep=[(0.0, 30.0)])
        self.assertEqual(dropped, 0)
        self.assertAlmostEqual(mapped[0]["start"], 3.0, places=6)
        self.assertAlmostEqual(mapped[0]["end"], 7.0, places=6)

    # 最初の残す区間の内側はそのままの時刻になる
    def test_inside_first_segment(self):
        mapped, dropped = self._map([_item(1.0, 3.0)])
        self.assertEqual(dropped, 0)
        self.assertAlmostEqual(mapped[0]["start"], 1.0, places=6)
        self.assertAlmostEqual(mapped[0]["end"], 3.0, places=6)

    # 2 番目の残す区間はカットぶん (5 秒) だけ手前へ詰まる
    def test_second_segment_is_shifted(self):
        mapped, _dropped = self._map([_item(12.0, 14.0)])
        self.assertAlmostEqual(mapped[0]["start"], 7.0, places=6)
        self.assertAlmostEqual(mapped[0]["end"], 9.0, places=6)

    # カットを跨ぐ字幕は詰めた結果 Timeline 上で連続するため 1 区間へまとまる
    def test_across_cut_is_merged_into_one(self):
        mapped, dropped = self._map([_item(4.0, 11.0)])
        self.assertEqual(dropped, 0)
        self.assertEqual(len(mapped), 1)
        self.assertAlmostEqual(mapped[0]["start"], 4.0, places=6)
        self.assertAlmostEqual(mapped[0]["end"], 6.0, places=6)

    # 全体がカット区間へ落ちる字幕は載せない (無音への幻聴字幕もここで落ちる)
    def test_inside_cut_is_dropped(self):
        mapped, dropped = self._map([_item(6.0, 9.0)])
        self.assertEqual(mapped, [])
        self.assertEqual(dropped, 1)

    # 一部がカットへかかる字幕はかかった分だけ短くなる (resolve2.md §13 R1)
    def test_partially_cut_is_shortened(self):
        mapped, dropped = self._map([_item(4.0, 7.0)])
        self.assertEqual(dropped, 0)
        self.assertAlmostEqual(mapped[0]["start"], 4.0, places=6)
        self.assertAlmostEqual(mapped[0]["end"], 5.0, places=6)

    # 残す区間が無い (防御的なケース) では全件除外し例外にしない
    def test_no_segments_drops_everything(self):
        mapped, dropped = self._map([_item(1.0, 2.0)], keep=[])
        self.assertEqual(mapped, [])
        self.assertEqual(dropped, 1)

    # 時刻以外のキー (text/role/use など) は写像で失われない
    def test_other_keys_are_preserved(self):
        mapped, _dropped = self._map([_item(12.0, 14.0, text="テロップ")])
        self.assertEqual(mapped[0]["text"], "テロップ")
        self.assertEqual(mapped[0]["role"], "streamer")
        self.assertTrue(mapped[0]["use"])

    # 写像後も時系列順を保ち、重ならない (I3 の前提)
    def test_order_is_kept(self):
        mapped, _dropped = self._map(
            [_item(1.0, 2.0, "a"), _item(11.0, 12.0, "b"), _item(13.0, 14.0, "c")])
        self.assertEqual([m["text"] for m in mapped], ["a", "b", "c"])
        starts = [m["start"] for m in mapped]
        self.assertEqual(starts, sorted(starts))


if __name__ == "__main__":
    unittest.main()
