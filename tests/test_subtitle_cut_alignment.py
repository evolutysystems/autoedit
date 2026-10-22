# カット跨ぎの分割とカット頭への整列 (src/modules/subtitle_generator) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver8/resolve6.md §7-1):
#   ・カットを跨いだ認識セグメントが単語単位で分かれ、後ろの片の開始が
#     カット頭と一致すること (§2-1 の症状がなくなること)
#   ・単語タイムスタンプが無いときは従来どおり 1 本で通ること (R4)
#   ・1 カット内のテロップの本数は変えないこと (§3-3)
#   ・表示がカットの終端を越えないこと (R3)
#   ・cut_bounds を渡さない呼び出しは現行と完全に同一であること (回帰)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.modules import subtitle_generator
from src.timeline.timemap import TimeMap

# 素材 30 秒のうち [0,5) と [10,15) を残す = 詰めた後は 10 秒
# 詰めた軸では カット1 = [0,5) / カット2 = [5,10)
_KEEP = [(0.0, 5.0), (10.0, 15.0)]
_BOUNDS = [(0.0, 5.0), (5.0, 10.0)]


# 単語 1 件 (認識が返す形)
def _word(start, end, text):
    return {"start": start, "end": end, "text": text}


# 認識セグメント 1 件。words を渡すと「単語が取れた」状態になる
def _item(start, end, text="あ", words=None):
    entry = {"start": start, "end": end, "text": text}
    if words is not None:
        entry["words"] = words
    return entry


class SplitByKeepSegmentsTest(unittest.TestCase):

    def _split(self, items, keep=None, **kwargs):
        return subtitle_generator.split_items_by_keep_segments(
            items, _KEEP if keep is None else keep, **kwargs)

    # 跨ぎ + 単語あり → カットごとに分かれ、後ろの片は 2 本目のカットの頭へ乗る
    # (素材 10.0 = 詰めた軸 5.0 = カット2 の頭)
    def test_across_cut_is_split_by_words(self):
        words = [_word(4.0, 4.6, "まえ"), _word(10.0, 10.8, "あと")]
        items, added = self._split([_item(4.0, 10.8, words=words)])
        self.assertEqual(added, 1)
        self.assertEqual(len(items), 2)
        self.assertAlmostEqual(items[0]["start"], 4.0, places=6)
        self.assertAlmostEqual(items[0]["end"], 4.6, places=6)
        self.assertEqual(items[0]["text"], "まえ")
        self.assertAlmostEqual(items[1]["start"], 10.0, places=6)
        self.assertAlmostEqual(items[1]["end"], 10.8, places=6)
        self.assertEqual(items[1]["text"], "あと")

    # 分割後に写像すると、後ろの片の開始がカット頭 (5.0) とぴったり一致する
    def test_split_then_map_lands_on_the_cut_head(self):
        words = [_word(4.0, 4.6, "まえ"), _word(10.0, 10.8, "あと")]
        items, _added = self._split([_item(4.0, 10.8, words=words)])
        mapped, dropped = subtitle_generator.map_items_to_timeline(items, _KEEP)
        self.assertEqual(dropped, 0)
        self.assertAlmostEqual(mapped[1]["start"], 5.0, places=6)

    # 跨ぎ + 単語なし → 従来どおり 1 本で通す (R4 の安全側フォールバック)
    def test_across_cut_without_words_is_kept_as_one(self):
        items, added = self._split([_item(4.0, 11.0)])
        self.assertEqual(added, 0)
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(items[0]["start"], 4.0, places=6)

    # 跨いでいないセグメントは一切触らない (同じ辞書がそのまま返る)
    def test_inside_one_cut_is_untouched(self):
        source = _item(1.0, 3.0, words=[_word(1.0, 3.0, "あ")])
        items, added = self._split([source])
        self.assertEqual(added, 0)
        self.assertIs(items[0], source)

    # 分割で文字が失われない
    def test_text_is_not_lost(self):
        words = [_word(4.0, 4.4, "あい"), _word(4.4, 4.8, "うえ"),
                 _word(10.0, 10.4, "おか"), _word(10.4, 10.8, "きく")]
        items, _added = self._split([_item(4.0, 10.8, words=words)])
        self.assertEqual("".join(i["text"] for i in items), "あいうえおかきく")

    # 前のカットに残る片が min_piece_sec 未満 → その語は次のカットの字幕へ寄る
    # 寄るのは文字だけで、時刻は次のカットのまま (再び跨がない)
    def test_short_head_piece_moves_to_the_next_cut(self):
        words = [_word(4.9, 5.0, "ね"), _word(10.0, 10.8, "あと")]
        items, added = self._split([_item(4.9, 10.8, words=words)],
                                   min_piece_sec=0.3)
        self.assertEqual(added, 0)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "ねあと")
        self.assertAlmostEqual(items[0]["start"], 10.0, places=6)

    # min_piece_sec=0 なら寄せずにそのまま分ける
    def test_short_head_piece_is_kept_when_disabled(self):
        words = [_word(4.9, 5.0, "ね"), _word(10.0, 10.8, "あと")]
        items, added = self._split([_item(4.9, 10.8, words=words)],
                                   min_piece_sec=0.0)
        self.assertEqual(added, 1)
        self.assertEqual([i["text"] for i in items], ["ね", "あと"])

    # 整形関数 (フィラー除去＋改行) は組み直した本文へ掛かる
    def test_wrap_is_applied_to_the_rebuilt_text(self):
        words = [_word(4.0, 4.6, "えーまえ"), _word(10.0, 10.8, "あと")]
        items, _added = self._split(
            [_item(4.0, 10.8, words=words)],
            wrap=lambda t: subtitle_generator.strip_fillers(t, ["えー"]))
        self.assertEqual(items[0]["text"], "まえ")


class AlignToCutHeadTest(unittest.TestCase):

    def _align(self, items, tolerance=0.6, bounds=None):
        return subtitle_generator.align_items_to_cut_heads(
            items, _BOUNDS if bounds is None else bounds, tolerance)

    # 許容差内の遅れ → 開始がカット頭と一致する
    def test_small_gap_is_snapped(self):
        items, aligned = self._align([_item(5.2, 7.0)])
        self.assertEqual(aligned, 1)
        self.assertAlmostEqual(items[0]["start"], 5.0, places=6)

    # 許容差を超える遅れ → 動かさない (長いカットの途中の発話を吸わせない)
    def test_large_gap_is_kept(self):
        items, aligned = self._align([_item(6.5, 8.0)])
        self.assertEqual(aligned, 0)
        self.assertAlmostEqual(items[0]["start"], 6.5, places=6)

    # 同じカットの 2 本目以降は動かさない (1 本目と同時に出てしまうため)
    def test_second_item_in_the_same_cut_is_kept(self):
        items, aligned = self._align([_item(5.2, 6.0, "a"), _item(6.1, 6.5, "b")])
        self.assertEqual(aligned, 1)
        self.assertAlmostEqual(items[0]["start"], 5.0, places=6)
        self.assertAlmostEqual(items[1]["start"], 6.1, places=6)

    # 1 カットに 3 本 → 件数は変わらず、先頭だけが頭へ寄る (§3-3)
    def test_three_items_in_one_cut_keep_their_count(self):
        source = [_item(5.2, 6.0, "a"), _item(6.2, 7.0, "b"), _item(7.5, 8.0, "c")]
        items, aligned = self._align(source)
        self.assertEqual(len(items), 3)
        self.assertEqual(aligned, 1)
        self.assertEqual([i["text"] for i in items], ["a", "b", "c"])
        self.assertAlmostEqual(items[0]["start"], 5.0, places=6)
        self.assertAlmostEqual(items[1]["start"], 6.2, places=6)
        self.assertAlmostEqual(items[2]["start"], 7.5, places=6)

    # 許容差 0 は無効 (入力をそのまま返す)
    def test_zero_tolerance_does_nothing(self):
        source = [_item(5.2, 7.0)]
        items, aligned = self._align(source, tolerance=0.0)
        self.assertEqual(aligned, 0)
        self.assertIs(items, source)

    # ちょうど頭に乗っている字幕は数えない (変化なし)
    def test_already_aligned_is_not_counted(self):
        items, aligned = self._align([_item(5.0, 7.0)])
        self.assertEqual(aligned, 0)
        self.assertAlmostEqual(items[0]["start"], 5.0, places=6)

    # カット頭が直前のテロップ (カット跨ぎ) に覆われているなら吸わせない
    # (吸わせると重なり、R3 が直前のテロップを境界で打ち切る / §10.2 原因H)
    def test_covered_cut_head_is_not_taken(self):
        items, aligned = self._align([_item(4.0, 5.4, "a"), _item(5.6, 6.0, "b")])
        self.assertEqual(aligned, 0)
        self.assertAlmostEqual(items[1]["start"], 5.6, places=6)

    # 終端ちょうど (カットの外) は触らない
    def test_outside_the_cuts_is_kept(self):
        items, aligned = self._align([_item(10.0, 11.0)])
        self.assertEqual(aligned, 0)
        self.assertAlmostEqual(items[0]["start"], 10.0, places=6)


class DisplayTimingWithCutBoundsTest(unittest.TestCase):

    # 表示がカットの終端を越えない (カット頭で必ず切り替わる)
    def test_hold_is_clamped_at_the_cut_end(self):
        items = subtitle_generator.adjust_display_timing(
            [{"start": 4.0, "end": 4.5, "text": "a"}],
            max_hold_sec=2.0, min_duration_sec=0.0, cut_bounds=_BOUNDS)
        self.assertAlmostEqual(items[0]["end"], 5.0, places=6)

    # チラつき防止の下限もカット終端に抑えられる
    def test_min_duration_is_clamped_too(self):
        items = subtitle_generator.adjust_display_timing(
            [{"start": 4.8, "end": 4.9, "text": "a"}],
            max_hold_sec=2.0, min_duration_sec=1.0, cut_bounds=_BOUNDS)
        self.assertAlmostEqual(items[0]["end"], 5.0, places=6)

    # カット内の 2 本目以降は現行どおり「次の発話開始まで」で切り替わる
    def test_inside_a_cut_switches_at_the_next_item(self):
        items = subtitle_generator.adjust_display_timing(
            [{"start": 5.0, "end": 5.4, "text": "a"},
             {"start": 6.0, "end": 6.4, "text": "b"},
             {"start": 7.0, "end": 7.4, "text": "c"}],
            max_hold_sec=2.0, min_duration_sec=0.0, cut_bounds=_BOUNDS)
        self.assertEqual(len(items), 3)
        self.assertAlmostEqual(items[0]["end"], 6.0, places=6)
        self.assertAlmostEqual(items[1]["end"], 7.0, places=6)
        self.assertAlmostEqual(items[2]["end"], 9.4, places=6)

    # カットを跨ぐテロップは最初のカットの終端で切らない (§10.2 原因G)
    # 基準は「発話が終わったカット」なので、カット2 の中まで表示できる
    def test_crossing_item_is_not_cut_at_the_first_cut_end(self):
        items = subtitle_generator.adjust_display_timing(
            [{"start": 4.0, "end": 5.5, "text": "a"}],
            max_hold_sec=2.0, min_duration_sec=0.0, cut_bounds=_BOUNDS)
        self.assertAlmostEqual(items[0]["end"], 7.5, places=6)

    # 跨いだテロップも「発話が終わったカット」の終端は越えない
    def test_crossing_item_stops_at_the_speech_cut_end(self):
        items = subtitle_generator.adjust_display_timing(
            [{"start": 4.0, "end": 9.5, "text": "a"}],
            max_hold_sec=2.0, min_duration_sec=0.0, cut_bounds=_BOUNDS)
        self.assertAlmostEqual(items[0]["end"], 10.0, places=6)

    # cut_bounds=None は現行と完全に同一 (旧経路の回帰)
    def test_without_cut_bounds_is_unchanged(self):
        source = [{"start": 4.0, "end": 4.5, "text": "a"}]
        legacy = subtitle_generator.adjust_display_timing(source, 2.0, 0.0)
        self.assertAlmostEqual(legacy[0]["end"], 6.5, places=6)


class EndToEndOrderTest(unittest.TestCase):

    # recognize_for_timeline と同じ順序を通し、
    #   ・すべてのテロップが 1 カット内に収まる
    #   ・各カットの先頭テロップの開始がカット頭と一致する
    # ことを確かめる
    def test_pipeline_order(self):
        recognized = [
            # カット1 の中で 2 本 (本数は変わらないこと)
            _item(0.3, 1.0, "a", words=[_word(0.3, 1.0, "a")]),
            _item(2.0, 2.6, "b", words=[_word(2.0, 2.6, "b")]),
            # カット1 → カット2 を跨ぐ 1 本 (分割されること)
            _item(4.2, 10.9, "cd", words=[_word(4.2, 4.6, "c"),
                                          _word(10.2, 10.9, "d")]),
        ]
        items, added = subtitle_generator.split_items_by_keep_segments(
            recognized, _KEEP, min_piece_sec=0.0)
        self.assertEqual(added, 1)

        mapped, dropped = subtitle_generator.map_items_to_timeline(items, _KEEP)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(mapped), 4)

        bounds = TimeMap.from_segments(_KEEP).timeline_bounds()
        self.assertEqual(bounds, _BOUNDS)

        aligned_items, aligned = subtitle_generator.align_items_to_cut_heads(
            mapped, bounds, 0.6)
        # カット1 の先頭 (0.3 → 0.0) と カット2 の先頭 (5.2 → 5.0) の 2 件
        self.assertEqual(aligned, 2)

        final = subtitle_generator.adjust_display_timing(
            aligned_items, 2.0, 0.5, cut_bounds=bounds)

        # 各カットの先頭テロップの開始がカット頭と一致する
        self.assertAlmostEqual(final[0]["start"], 0.0, places=6)
        self.assertAlmostEqual(final[3]["start"], 5.0, places=6)
        # 2 本目以降は発話のタイミングのまま
        self.assertAlmostEqual(final[1]["start"], 2.0, places=6)
        # すべてのテロップが 1 カット内に収まる
        for entry in final:
            index = subtitle_generator._cut_index_at(bounds, entry["start"])
            self.assertIsNotNone(index)
            self.assertLessEqual(entry["end"], bounds[index][1] + 1e-6)


if __name__ == "__main__":
    unittest.main()
