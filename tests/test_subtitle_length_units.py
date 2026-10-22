# 文字数で決めるテロップ単位 (src/modules/subtitle_generator) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver8/resolve6.md §14-1):
#   ・下限文字数に達していない片はカットを跨いで 1 本になること
#     (「カットごとに必ず 1 本」で裁断されていた §10.2 原因F の回帰テスト)
#   ・下限達成後はカット境界・文末で切れること (カット頭からテロップが始まる)
#   ・上限超過はカットの途中でも切れること (1 クリップに複数のテロップ)
#   ・単語が取れない認識結果はそのまま 1 本で通ること (R4)
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


class RegroupByLengthTest(unittest.TestCase):

    def _regroup(self, items, keep=None, **kwargs):
        kwargs.setdefault("min_chars", 5)
        kwargs.setdefault("max_chars", 40)
        return subtitle_generator.regroup_items_by_length(
            items, _KEEP if keep is None else keep, **kwargs)

    # 下限未満の片はカットを跨いで 1 本になる (今回の不具合の回帰テスト)
    def test_short_pieces_across_cuts_become_one(self):
        words = [_word(4.0, 4.6, "まえ"), _word(10.0, 10.8, "あと")]
        items, count = self._regroup([_item(4.0, 10.8, words=words)])
        self.assertEqual(count, 1)
        self.assertEqual(items[0]["text"], "まえあと")
        self.assertAlmostEqual(items[0]["start"], 4.0, places=6)
        self.assertAlmostEqual(items[0]["end"], 10.8, places=6)

    # 下限達成後はカット境界で切れる (次のテロップがカット頭から始まる)
    def test_break_happens_at_the_cut_when_enough_chars(self):
        words = [_word(3.0, 4.0, "あいうえお"), _word(10.0, 10.8, "かきく")]
        items, count = self._regroup([_item(3.0, 10.8, words=words)])
        self.assertEqual(count, 2)
        self.assertEqual([i["text"] for i in items], ["あいうえお", "かきく"])
        self.assertAlmostEqual(items[0]["end"], 4.0, places=6)
        self.assertAlmostEqual(items[1]["start"], 10.0, places=6)

    # 上限超過はカットの途中でも切れる (1 クリップに複数のテロップ)
    def test_over_max_breaks_inside_one_cut(self):
        words = [_word(0.0, 0.5, "あい"), _word(0.5, 1.0, "うえ"),
                 _word(1.0, 1.5, "おか")]
        items, count = self._regroup([_item(0.0, 1.5, words=words)],
                                     min_chars=2, max_chars=4)
        self.assertEqual(count, 2)
        self.assertEqual([i["text"] for i in items], ["あいうえ", "おか"])

    # 上限は下限より強い (下限未達でも上限超過なら切る)
    def test_max_wins_over_min(self):
        words = [_word(0.0, 0.5, "あい"), _word(0.5, 1.0, "うえ"),
                 _word(1.0, 1.5, "おか")]
        items, count = self._regroup([_item(0.0, 1.5, words=words)],
                                     min_chars=10, max_chars=4)
        self.assertEqual(count, 2)
        self.assertEqual([i["text"] for i in items], ["あいうえ", "おか"])

    # 文末記号で切れるのは下限達成後だけ
    def test_sentence_end_breaks_after_min(self):
        words = [_word(0.0, 0.8, "あいうえお。"), _word(1.0, 1.6, "かきく")]
        items, count = self._regroup([_item(0.0, 1.6, words=words)])
        self.assertEqual(count, 2)
        self.assertEqual([i["text"] for i in items], ["あいうえお。", "かきく"])

    # 下限未達の文末では切らない (「はい。」が独立したテロップにならない)
    def test_sentence_end_under_min_is_kept(self):
        words = [_word(0.0, 0.4, "あい。"), _word(0.6, 1.2, "かきく")]
        items, count = self._regroup([_item(0.0, 1.2, words=words)])
        self.assertEqual(count, 1)
        self.assertEqual(items[0]["text"], "あい。かきく")

    # break_on_sentence_end=False なら文末では切らない
    def test_sentence_break_can_be_disabled(self):
        words = [_word(0.0, 0.8, "あいうえお。"), _word(1.0, 1.6, "かきく")]
        items, count = self._regroup([_item(0.0, 1.6, words=words)],
                                     break_on_sentence_end=False)
        self.assertEqual(count, 1)
        self.assertEqual(items[0]["text"], "あいうえお。かきく")

    # 取り除かれた無音が長いときは下限未達でも切る (文脈が切れている)
    def test_long_removed_gap_breaks(self):
        words = [_word(4.0, 4.6, "まえ"), _word(10.0, 10.8, "あと")]
        items, count = self._regroup([_item(4.0, 10.8, words=words)],
                                     max_cut_gap_sec=1.0)
        self.assertEqual(count, 2)
        self.assertEqual([i["text"] for i in items], ["まえ", "あと"])

    # max_cut_gap_sec=0 は無制限 (無音の長さでは切らない)
    def test_zero_gap_limit_is_unlimited(self):
        words = [_word(4.0, 4.6, "まえ"), _word(10.0, 10.8, "あと")]
        items, count = self._regroup([_item(4.0, 10.8, words=words)],
                                     max_cut_gap_sec=0.0)
        self.assertEqual(count, 1)

    # 単語が取れない認識結果はそのまま 1 本で通す (R4 の安全側フォールバック)
    def test_items_without_words_pass_through(self):
        plain = _item(2.0, 3.0, "ことば")
        items, count = self._regroup(
            [_item(0.0, 1.0, words=[_word(0.0, 1.0, "あいうえお")]), plain])
        self.assertEqual(count, 2)
        self.assertIs(items[1], plain)

    # カットされた無音の中の単語は載せない (幻聴字幕を拾わない)
    def test_words_in_removed_silence_are_dropped(self):
        words = [_word(4.0, 4.5, "あいうえお"), _word(6.0, 7.0, "ごみ"),
                 _word(10.0, 10.5, "かきく")]
        items, count = self._regroup([_item(4.0, 10.5, words=words)])
        self.assertEqual(count, 2)
        self.assertEqual([i["text"] for i in items], ["あいうえお", "かきく"])

    # まとめ直しで文字が失われない
    def test_text_is_not_lost(self):
        words = [_word(4.0, 4.4, "あい"), _word(4.4, 4.8, "うえ"),
                 _word(10.0, 10.4, "おか"), _word(10.4, 10.8, "きく")]
        items, _count = self._regroup([_item(4.0, 10.8, words=words)])
        self.assertEqual("".join(i["text"] for i in items), "あいうえおかきく")

    # 整形関数 (フィラー除去＋改行) は組み直した本文へ掛かる
    def test_wrap_is_applied_to_the_rebuilt_text(self):
        words = [_word(4.0, 4.6, "えーまえ"), _word(10.0, 10.8, "あと")]
        items, _count = self._regroup(
            [_item(4.0, 10.8, words=words)],
            wrap=lambda t: subtitle_generator.strip_fillers(t, ["えー"]))
        self.assertEqual(items[0]["text"], "まえあと")

    # start/end は keep_segments の端ではなく単語の実時間になる
    def test_times_come_from_words(self):
        words = [_word(4.2, 4.6, "まえ"), _word(10.2, 10.9, "あと")]
        items, _count = self._regroup([_item(4.2, 10.9, words=words)])
        self.assertAlmostEqual(items[0]["start"], 4.2, places=6)
        self.assertAlmostEqual(items[0]["end"], 10.9, places=6)

    # 下限達成後は認識セグメントの境界で切れる (別の文を 1 本へ混ぜない / B5)
    def test_segment_boundary_breaks_after_min(self):
        items, count = self._regroup([
            _item(0.0, 1.0, "あいうえお", words=[_word(0.0, 1.0, "あいうえお")]),
            _item(1.6, 2.4, "かきくけこ", words=[_word(1.6, 2.4, "かきくけこ")]),
        ])
        self.assertEqual(count, 2)
        self.assertEqual([i["text"] for i in items], ["あいうえお", "かきくけこ"])

    # 認識セグメントの境界だけでは切れない (短い断片は次と繋がる)
    def test_segment_boundary_alone_does_not_break(self):
        items, count = self._regroup([
            _item(0.0, 0.5, "はい。", words=[_word(0.0, 0.5, "はい。")]),
            _item(0.8, 1.6, "それでは", words=[_word(0.8, 1.6, "それでは")]),
        ])
        self.assertEqual(count, 1)
        self.assertEqual(items[0]["text"], "はい。それでは")
        self.assertAlmostEqual(items[0]["start"], 0.0, places=6)
        self.assertAlmostEqual(items[0]["end"], 1.6, places=6)


class WordAttachTest(unittest.TestCase):
    """カットの外へこぼれた単語を拾う (resolve6 第4部 R6)"""

    def _regroup(self, items, **kwargs):
        kwargs.setdefault("min_chars", 5)
        kwargs.setdefault("max_chars", 40)
        return subtitle_generator.regroup_items_by_length(items, _KEEP, **kwargs)

    # 「かんたんです」の語頭「か」がカット頭 (10.0) の手前へこぼれた想定
    def _spilled_head(self):
        words = [_word(9.88, 9.98, "か"), _word(10.0, 10.3, "んたん"),
                 _word(10.3, 10.8, "です")]
        return [_item(9.88, 10.8, "かんたんです", words=words)]

    # カット頭の手前にこぼれた語が落ちない (今回の症状の回帰テスト)
    def test_word_before_the_cut_head_is_kept(self):
        items, count = self._regroup(self._spilled_head())
        self.assertEqual(count, 1)
        self.assertEqual(items[0]["text"], "かんたんです")

    # 寄せた語で始まるテロップの開始はカット頭と一致する
    def test_attached_word_starts_at_the_cut_head(self):
        items, _count = self._regroup(self._spilled_head())
        self.assertAlmostEqual(items[0]["start"], 10.0, places=6)
        mapped, dropped = subtitle_generator.map_items_to_timeline(items, _KEEP)
        self.assertEqual(dropped, 0)
        self.assertAlmostEqual(mapped[0]["start"], 5.0, places=6)

    # 語尾のこぼれ (カット尻の直後) も拾い、終了はカット終端で止まる
    def test_word_after_the_cut_tail_is_kept(self):
        words = [_word(4.5, 4.9, "あいう"), _word(5.05, 5.2, "す")]
        items, count = self._regroup([_item(4.5, 5.2, "あいうす", words=words)])
        self.assertEqual(count, 1)
        self.assertEqual(items[0]["text"], "あいうす")
        self.assertAlmostEqual(items[0]["end"], 5.0, places=6)

    # 無音の真ん中の語は従来どおり落ちる (幻聴字幕の抑制を壊さない)
    def test_far_word_in_silence_is_still_dropped(self):
        words = [_word(4.0, 4.5, "あいうえお"), _word(7.0, 7.3, "幻"),
                 _word(10.0, 10.5, "かきく")]
        items, _count = self._regroup([_item(4.0, 10.5, words=words)])
        self.assertNotIn("幻", "".join(i["text"] for i in items))

    # 許容差 0 なら従来どおり落とす (切り分け用)
    def test_zero_tolerance_keeps_the_old_behavior(self):
        items, count = self._regroup(self._spilled_head(),
                                     attach_tolerance_sec=0.0)
        self.assertEqual(count, 1)
        self.assertEqual(items[0]["text"], "んたんです")

    # 寄せた語だけのテロップでも写像で消えない (尺 0 にならない)
    def test_attached_word_is_not_lost_by_mapping(self):
        items, count = self._regroup(
            [_item(9.88, 9.98, "か", words=[_word(9.88, 9.98, "か")])])
        self.assertEqual(count, 1)
        self.assertGreater(items[0]["end"] - items[0]["start"], 0.0)
        mapped, dropped = subtitle_generator.map_items_to_timeline(items, _KEEP)
        self.assertEqual(dropped, 0)
        self.assertEqual(mapped[0]["text"], "か")

    # 走査位置が目的のカットを通り過ぎていても拾える (保険 / §18-2)
    def test_out_of_order_word_is_not_dropped(self):
        index, word, _cursor = subtitle_generator._resolve_word_cut(
            _word(4.0, 4.5, "い"), _KEEP, cursor=1)
        self.assertEqual(index, 0)
        self.assertAlmostEqual(word["start"], 4.0, places=6)

    # 順序が前後した単語でも文字が落ちない (再構成を通した確認)
    def test_out_of_order_text_is_not_lost(self):
        items, _count = self._regroup([
            _item(10.2, 10.5, "あ", words=[_word(10.2, 10.5, "あ")]),
            _item(4.0, 4.5, "い", words=[_word(4.0, 4.5, "い")]),
        ])
        self.assertEqual("".join(i["text"] for i in items), "あい")


class PipelineOrderTest(unittest.TestCase):

    # recognize_for_timeline と同じ順序 (再構成 → 写像 → 整列 → 整形) を通し、
    #   ・1 カットに複数のテロップが載る
    #   ・カットを跨ぐテロップが 1 本のまま残る
    #   ・カット頭から始まるテロップの開始がカット頭と一致する
    # ことを確かめる
    def test_pipeline_order_keeps_cut_head_and_crossing(self):
        recognized = [
            # カット1 の中の 2 本 (上限 5 文字で切れる)
            _item(0.3, 2.8, "あいうえおかきくけこ",
                  words=[_word(0.3, 1.2, "あいうえお"), _word(2.0, 2.8, "かきくけこ")]),
            # カット1 → カット2 を跨ぐ 1 本 (下限 5 文字に届かないため繋がる)
            _item(4.2, 10.9, "さしす",
                  words=[_word(4.2, 4.6, "さ"), _word(10.2, 10.9, "しす")]),
        ]
        items, count = subtitle_generator.regroup_items_by_length(
            recognized, _KEEP, min_chars=5, max_chars=5)
        self.assertEqual(count, 3)

        mapped, dropped = subtitle_generator.map_items_to_timeline(items, _KEEP)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(mapped), 3)

        bounds = TimeMap.from_segments(_KEEP).timeline_bounds()
        self.assertEqual(bounds, _BOUNDS)

        aligned_items, aligned = subtitle_generator.align_items_to_cut_heads(
            mapped, bounds, 0.6)
        # カット1 の先頭 (0.3 → 0.0) の 1 件。カット2 の頭は跨ぎテロップが覆う
        self.assertEqual(aligned, 1)

        final = subtitle_generator.adjust_display_timing(
            aligned_items, 2.0, 0.5, cut_bounds=bounds)
        self.assertEqual(len(final), 3)

        # カット頭と一致する
        self.assertAlmostEqual(final[0]["start"], 0.0, places=6)
        # 2 本目は発話のタイミングのまま (1 カットに複数のテロップ)
        self.assertAlmostEqual(final[1]["start"], 2.0, places=6)
        # 3 本目はカット境界 (5.0) を跨いだまま 1 本で残る
        self.assertLess(final[2]["start"], 5.0)
        self.assertGreater(final[2]["end"], 5.0)


class SubtitleUnitTest(unittest.TestCase):

    # 明示指定はそのまま使う
    def test_explicit_values(self):
        for value in ("length", "cut", "segment"):
            self.assertEqual(
                subtitle_generator.subtitle_unit({"subtitle_unit": value}), value)

    # 不正値は既定 (length) へ倒す
    def test_invalid_value_falls_back_to_length(self):
        self.assertEqual(
            subtitle_generator.subtitle_unit({"subtitle_unit": "???"}), "length")

    # 旧 setting.json (キー無し) は split_across_cut から決める
    def test_legacy_settings(self):
        self.assertEqual(subtitle_generator.subtitle_unit({}), "length")
        self.assertEqual(
            subtitle_generator.subtitle_unit({"split_across_cut": True}), "length")
        self.assertEqual(
            subtitle_generator.subtitle_unit({"split_across_cut": False}), "segment")


if __name__ == "__main__":
    unittest.main()
