# テロップが音声と合わない 3 件の回帰テスト
# (docs/error/20261125c/error.md / resolve.md)
# 実行: python -m unittest discover -s tests
#
# 実測した症状:
#   ① AI のテロップ 51 本が既存テロップの枠を乗っ取り、0.30 秒の枠へ 12 文字が入った
#   ② AI へ渡す文字起こしが 99 件 → 1 件 (149.1 秒 / 1,538 文字) に潰れていた
#
# ③ (テロップ単位が語の途中で切れる) は利用者の判断で見送り。
# 既存の文字起こし処理は安定しているため触らない (resolve.md §3)。
#
# ffmpeg も AI も呼ばない。
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.ai import plan_applier, plan_schema, prompts
from src.ai.context_builder import _merge_transcript

# 既存の足場を使い回す (アーカイブ Timeline を手で組む部分)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_ai_plan_applier import (ApplierTestBase, _Scene,  # noqa: E402
                                  _plan, _scene)


# ---- ① AI のテロップを正しい枠へ置く -----------------------------------

class SpanOfTest(unittest.TestCase):
    """_span_of: 無音カットで割れた片をまとめる (§1.1)"""

    # T1': 隣接した片は 1 つの枠になる
    def test_adjacent_pieces_become_one_span(self):
        span = plan_applier._span_of([(1.0, 2.0), (2.0, 3.0), (3.0, 4.5)], 0.05)
        self.assertEqual(span, (1.0, 4.5))

    # T2: 許容差を超える隙間があれば**最も長い一続き**を採る
    def test_the_longest_run_wins_when_there_is_a_gap(self):
        span = plan_applier._span_of(
            [(1.0, 1.4), (5.0, 6.0), (6.0, 7.5)], 0.05)
        self.assertEqual(span, (5.0, 7.5))

    def test_a_single_piece_is_returned_as_is(self):
        self.assertEqual(plan_applier._span_of([(2.0, 2.3)], 0.05), (2.0, 2.3))

    # 許容差ちょうどの隙間は繋ぐ
    def test_a_gap_within_the_tolerance_is_joined(self):
        self.assertEqual(
            plan_applier._span_of([(1.0, 2.0), (2.04, 3.0)], 0.05), (1.0, 3.0))


class UnplaceableTest(unittest.TestCase):
    """_unplaceable: 読めない枠へは書かない (§1.3)"""

    def setUp(self):
        self.cfg = {"min_display_sec": 0.4, "max_chars_per_sec": 20}

    # T6: 枠が短すぎる
    def test_too_short(self):
        self.assertEqual(plan_applier._unplaceable("あ", 0.3, self.cfg),
                         "too short")

    # T5: 文字が詰まりすぎ (実測の 0.30 秒 / 12 文字はまずここで落ちる)
    def test_too_dense(self):
        self.assertEqual(
            plan_applier._unplaceable("フロントウィングないやん", 0.5, self.cfg),
            "too dense")

    def test_readable_is_allowed(self):
        self.assertIsNone(
            plan_applier._unplaceable("フロントウィングないやん", 2.0, self.cfg))

    # 改行は文字数に数えない (表示の行数は別の仕組みが見る)
    def test_newlines_do_not_count(self):
        self.assertIsNone(
            plan_applier._unplaceable("あいうえお\nかきくけこ", 0.6, self.cfg))

    # 上限 0 = 無効
    def test_zero_limit_disables_the_density_check(self):
        cfg = {"min_display_sec": 0.0, "max_chars_per_sec": 0}
        self.assertIsNone(plan_applier._unplaceable("あ" * 100, 0.1, cfg))


class SubtitlePlacementTest(ApplierTestBase):
    """apply_plan まで通した置き方 (§1.1 / §1.2)"""

    # T1: 無音カットで割れた素材でも、枠は片の全体になる
    #
    # 素材 0-2 と 5-7 を残す = Timeline 0-2 と 2-4。
    # AI が素材 101-106 (= 1-6) を指したら、残っているのは 1-2 と 5-6 であり
    # Timeline では 1-2 と 2-3 = **隣接している**。直す前は先頭の 1 秒しか見ず、
    # 実測ではこれが 0.30 秒の破片になっていた。
    def test_the_span_covers_every_piece(self):
        timeline = self.build([_Scene(100.0, [(0.0, 2.0), (5.0, 7.0)])])
        plan = _plan([_scene("sc1", 100.0, 107.0)],
                     subtitles=[{"scene_id": "sc1", "source_start": 101.0,
                                 "source_end": 106.0, "text": "まさか",
                                 "track_role": "own"}])

        applied, _ = self.apply(timeline, plan)
        clips = applied.subtitle_clips(only_used=False)

        self.assertEqual(1, len(clips))
        self.assertAlmostEqual(1.0, clips[0].timeline_start, places=6)
        self.assertAlmostEqual(2.0, clips[0].duration, places=6,
                              msg="片の全体 (1-3) を覆う")

    # T3: 重なりが浅い既存テロップは再利用しない (枠を乗っ取らせない)
    #
    # 既存 1.0-1.2 (0.2 秒) に対して AI の枠は 1.0-3.0。
    # 覆っている比は 0.2 / 2.0 = 0.1 で reuse_min_ratio 0.6 に届かない。
    def test_a_shallow_overlap_is_not_reused(self):
        timeline = self.build([_Scene(100.0, [(0.0, 2.0), (5.0, 7.0)],
                                      texts=[("フ", 1.0, 0.2)])])
        plan = _plan([_scene("sc1", 100.0, 107.0)],
                     subtitles=[{"scene_id": "sc1", "source_start": 101.0,
                                 "source_end": 106.0,
                                 "text": "フロントウィングないやん",
                                 "track_role": "own"}])

        applied, _ = self.apply(timeline, plan)
        clips = applied.subtitle_clips(only_used=False)
        texts = sorted(clip.text for clip in clips)

        self.assertEqual(2, len(clips), "足す。元の枠は残す")
        self.assertEqual(["フ", "フロントウィングないやん"], texts)
        original = [clip for clip in clips if clip.text == "フ"][0]
        self.assertAlmostEqual(0.2, original.duration, places=6,
                              msg="元のテロップは触らない")
        added = [clip for clip in clips if clip.text != "フ"][0]
        self.assertAlmostEqual(1.2, added.timeline_start, places=6,
                              msg="既存を避けて空いている所へ寄せる")
        self.assertAlmostEqual(1.8, added.duration, places=6)
    def test_a_deep_overlap_is_still_reused(self):
        timeline = self.build([_Scene(100.0, [(0.0, 2.0), (5.0, 7.0)],
                                      texts=[("もとの文字起こし", 1.0, 2.0)])])
        plan = _plan([_scene("sc1", 100.0, 107.0)],
                     subtitles=[{"scene_id": "sc1", "source_start": 101.0,
                                 "source_end": 106.0, "text": "まさか",
                                 "track_role": "own", "animation": "shake"}])

        applied, _ = self.apply(timeline, plan)
        clips = applied.subtitle_clips(only_used=False)

        self.assertEqual(1, len(clips), "足さずに直す")
        self.assertEqual("shake", clips[0].animation)

    # T5: 読めない枠には置かず、理由をレポートへ残す
    def test_a_dense_subtitle_is_dropped_with_a_reason(self):
        timeline = self.build([_Scene(100.0, [(0.0, 0.5)])])
        plan = _plan([_scene("sc1", 100.0, 100.5)],
                     subtitles=[{"scene_id": "sc1", "source_start": 100.0,
                                 "source_end": 100.5,
                                 "text": "フロントウィングないやん",
                                 "track_role": "own"}])

        applied, report = self.apply(timeline, plan)

        self.assertEqual(0, len(applied.subtitle_clips(only_used=False)))
        reasons = [note.get("detail") or note.get("reason")
                   for note in report.notes if note["check"] == "A5"]
        self.assertIn("too dense", reasons)






class FreeSpanTest(ApplierTestBase):
    """_free_span: 同じ役割のテロップが埋めていない所 (§1.2)"""

    def _timeline_with(self, texts):
        return self.build([_Scene(100.0, [(0.0, 10.0)], texts=texts)])

    def test_the_longest_gap_wins(self):
        timeline = self._timeline_with([("あ", 1.0, 0.2), ("い", 4.0, 0.2)])
        span = plan_applier._free_span(timeline, "streamer", 0.0, 6.0)
        # 空きは 0.0-1.0 / 1.2-4.0 / 4.2-6.0 の 3 つ。最も長い 1.2-4.0 を採る
        self.assertEqual((1.2, 4.0), span)

    def test_a_free_range_is_returned_whole(self):
        timeline = self._timeline_with([])
        self.assertEqual((1.0, 3.0),
                         plan_applier._free_span(timeline, "streamer", 1.0, 3.0))

    # 埋まり切っていれば None (呼び出し側は "no room" で落とす)
    def test_a_full_range_is_none(self):
        timeline = self._timeline_with([("あ", 0.0, 5.0)])
        self.assertIsNone(plan_applier._free_span(timeline, "streamer", 1.0, 3.0))

    # 役割が違えばトラックが違うので障害物にしない
    def test_another_role_is_not_an_obstacle(self):
        timeline = self._timeline_with([("あ", 1.0, 2.0)])
        self.assertEqual((1.0, 3.0),
                         plan_applier._free_span(timeline, "comment", 1.0, 3.0))


class NoRoomTest(ApplierTestBase):
    """空きが無いときは落とす (文字起こしを壊さない)"""

    def test_a_filled_range_is_dropped(self):
        timeline = self.build([_Scene(100.0, [(0.0, 10.0)],
                                      texts=[("びっしり埋まった文字起こし", 0.0, 9.0)])])
        plan = _plan([_scene("sc1", 100.0, 110.0)],
                     subtitles=[{"scene_id": "sc1", "source_start": 102.0,
                                 "source_end": 103.0, "text": "まさか",
                                 "track_role": "own"}])

        applied, report = self.apply(timeline, plan)
        clips = applied.subtitle_clips(only_used=False)

        self.assertEqual(1, len(clips))
        self.assertEqual("びっしり埋まった文字起こし", clips[0].text,
                         "既存の本文を書き換えない")
        reasons = [note.get("detail") for note in report.notes
                   if note["check"] == "A5"]
        self.assertIn("no room", reasons)

    # T4: 重なりが深ければ従来どおり再利用する (回帰)
class SubtitleAtTest(ApplierTestBase):
    """_subtitle_at の重なり比 (§1.2)"""

    def _timeline_with(self, start, duration):
        return self.build([_Scene(100.0, [(0.0, 10.0)],
                                  texts=[("既存", start, duration)])])

    def test_ratio_zero_keeps_the_old_behaviour(self):
        timeline = self._timeline_with(1.0, 0.2)
        found = plan_applier._subtitle_at(timeline, 1.0, 3.0, 0.0)
        self.assertIsNotNone(found, "比を見ないなら少しの重なりでも拾う")

    def test_a_short_existing_clip_is_refused(self):
        timeline = self._timeline_with(1.0, 0.2)
        self.assertIsNone(plan_applier._subtitle_at(timeline, 1.0, 3.0, 0.6))

    # 長い既存テロップが短い AI テロップを飲むのも防ぐ
    def test_a_long_existing_clip_is_refused(self):
        timeline = self._timeline_with(0.0, 9.0)
        self.assertIsNone(plan_applier._subtitle_at(timeline, 1.0, 1.5, 0.6))

    def test_a_matching_clip_is_accepted(self):
        timeline = self._timeline_with(1.0, 2.0)
        self.assertIsNotNone(plan_applier._subtitle_at(timeline, 1.0, 3.0, 0.6))


# ---- ② 文字起こしに上限を付ける ----------------------------------------

def _item(start, end, text):
    return {"start": start, "end": end, "text": text}


class MergeTranscriptTest(unittest.TestCase):
    """_merge_transcript の上限 (§2.2)"""

    def setUp(self):
        # 1 秒ずつ隙間なく続く発話 = 従来は全部 1 件に潰れる
        self.items = [_item(float(i), float(i) + 0.9, "あいうえ")
                      for i in range(20)]

    # T9: 上限 0 なら従来どおり無制限 (回帰)
    def test_no_limit_merges_everything(self):
        merged = _merge_transcript(self.items, 1.0)
        self.assertEqual(1, len(merged))
        self.assertAlmostEqual(0.0, merged[0]["start"], places=6)
        self.assertAlmostEqual(19.9, merged[0]["end"], places=6)

    # T7: 長さで切れる
    def test_max_sec_splits(self):
        merged = _merge_transcript(self.items, 1.0, max_sec=6.0)
        self.assertGreater(len(merged), 1)
        for row in merged:
            self.assertLessEqual(row["end"] - row["start"], 6.0 + 1e-6)

    # T8: 文字数で切れる
    def test_max_chars_splits(self):
        merged = _merge_transcript(self.items, 1.0, max_chars=40)
        self.assertGreater(len(merged), 1)
        for row in merged:
            self.assertLessEqual(len(row["text"]), 40)

    # T10: 実測の形 (99 件の連続した発話) が 1 件にならない
    def test_the_measured_case_does_not_collapse(self):
        items = [_item(i * 1.5, i * 1.5 + 1.4, "しゃべっている") for i in range(99)]
        merged = _merge_transcript(items, 1.0, max_sec=6.0, max_chars=40)
        self.assertGreater(len(merged), 20,
                           "99 件が 1 件に潰れていたのが症状")
        self.assertLessEqual(max(r["end"] - r["start"] for r in merged), 6.0 + 1e-6)

    # 隙間が gap を超えるところは今までどおり切れる
    def test_the_gap_still_splits(self):
        items = [_item(0.0, 1.0, "まえ"), _item(5.0, 6.0, "あと")]
        self.assertEqual(2, len(_merge_transcript(items, 1.0, 6.0, 40)))

    # 1 件だけで上限を超えていても落とさない (分割はできない)
    def test_a_single_long_item_survives(self):
        items = [_item(0.0, 30.0, "あ" * 100)]
        merged = _merge_transcript(items, 1.0, max_sec=6.0, max_chars=40)
        self.assertEqual(1, len(merged))
        self.assertEqual("あ" * 100, merged[0]["text"])


# ---- ④ テロップをどこまで AI に触らせるか (subtitle_mode) ---------------
#
# 「AI が盛り上がっている箇所を切り抜いて、文字起こしは既存の処理でやる」
# という構成に合わせ、既定を "key" (見た目だけ) にした。
# ai.plan.subtitle_mode は語彙だけあってどこからも読まれていなかった。

class SubtitleModeTest(unittest.TestCase):
    """設定の語の正規化"""

    def test_known_modes_pass_through(self):
        for mode in ("full", "key", "none"):
            self.assertEqual(mode, plan_schema.subtitle_mode(mode))

    # 知らない語・空は既定 (key) へ落ちる
    def test_unknown_falls_back_to_key(self):
        self.assertEqual("key", plan_schema.subtitle_mode(""))
        self.assertEqual("key", plan_schema.subtitle_mode(None))
        self.assertEqual("key", plan_schema.subtitle_mode("ぜんぶ"))

    def test_the_shipped_default_is_key(self):
        from src.ai import config as ai_config
        from src.settings import settings_window

        self.assertEqual("key", ai_config.DEFAULT_AI["plan"]["subtitle_mode"])
        self.assertEqual(
            "key", settings_window.DEFAULT_SETTINGS["ai"]["plan"]["subtitle_mode"])

    # 前置きはモードごとに違う (キャッシュの前方一致の一部でもある)
    def test_the_prompt_rule_differs_per_mode(self):
        rules = {mode: prompts.subtitle_rule(mode)
                 for mode in ("full", "key", "none")}
        self.assertEqual(3, len(set(rules.values())))
        self.assertIn("そのまま使う", rules["full"])
        self.assertIn("変更されない", rules["key"])
        self.assertEqual(rules["key"], prompts.subtitle_rule("しらない語"))


class _ModeRunner(ApplierTestBase):
    """subtitle_mode を変えて apply_plan を通す足場 (テストは持たない)"""

    def _settings(self, mode):
        import copy

        from test_ai_plan_applier import _SETTINGS

        settings = copy.deepcopy(_SETTINGS)
        settings["ai"]["plan"]["subtitle_mode"] = mode

        return settings

    def _run(self, mode, texts, subtitles):
        import copy

        import src.ai.plan_validator as pv
        from src.ai import plan_applier as pa

        settings = self._settings(mode)
        timeline = self.build([_Scene(100.0, [(0.0, 10.0)], texts=texts)])
        plan = _plan([_scene("sc1", 100.0, 110.0)], subtitles=subtitles)
        normalized, _ = pv.validate(copy.deepcopy(plan), settings=settings,
                                    exists=lambda path: True)
        applied, report = pa.apply_plan(timeline, normalized, settings=settings,
                                        probe=lambda path: None)

        return applied, report


class ModeKeyTest(_ModeRunner):
    """key: 本文と時刻は文字起こしのまま。見た目だけ付ける"""

    # 本文は書き換えない。動きだけ付く
    def test_the_text_is_kept_and_the_look_is_applied(self):
        applied, report = self._run(
            "key", [("もとの文字起こし", 1.0, 2.0)],
            [{"scene_id": "sc1", "source_start": 101.0, "source_end": 103.0,
              "text": "AI が書き直した本文", "track_role": "own",
              "animation": "shake", "fade_in": True}])
        clips = applied.subtitle_clips(only_used=False)

        self.assertEqual(1, len(clips))
        self.assertEqual("もとの文字起こし", clips[0].text, "本文は文字起こしのまま")
        self.assertEqual("shake", clips[0].animation, "見た目は付く")
        self.assertTrue(clips[0].fade_in)
        details = [n.get("detail") for n in report.notes if n["check"] == "A5"]
        self.assertIn("looks", details)

    # 重なる既存テロップが無ければ**足さない**
    def test_nothing_is_added(self):
        applied, report = self._run(
            "key", [],
            [{"scene_id": "sc1", "source_start": 105.0, "source_end": 107.0,
              "text": "AI が足そうとした一言", "track_role": "own"}])

        self.assertEqual(0, len(applied.subtitle_clips(only_used=False)))
        details = [n.get("detail") for n in report.notes if n["check"] == "A5"]
        self.assertIn("subtitle_mode key", details)

    # 役割は変えない (変えるとトラックが移り、文字起こしが S1 から出てしまう)
    def test_the_role_is_not_changed(self):
        applied, _ = self._run(
            "key", [("もとの文字起こし", 1.0, 2.0)],
            [{"scene_id": "sc1", "source_start": 101.0, "source_end": 103.0,
              "text": "もとの文字起こし", "track_role": "comment"}])
        clips = applied.subtitle_clips(only_used=False)

        self.assertEqual(1, len(clips))
        self.assertNotEqual("comment", clips[0].role)


class ModeNoneTest(_ModeRunner):
    """none: テロップには一切触らない"""

    def test_everything_is_dropped(self):
        applied, report = self._run(
            "none", [("もとの文字起こし", 1.0, 2.0)],
            [{"scene_id": "sc1", "source_start": 101.0, "source_end": 103.0,
              "text": "AI の本文", "track_role": "own", "animation": "shake"}])
        clips = applied.subtitle_clips(only_used=False)

        self.assertEqual(1, len(clips))
        self.assertEqual("もとの文字起こし", clips[0].text)
        self.assertEqual("", clips[0].animation, "見た目も付けない")
        details = [n.get("detail") for n in report.notes if n["check"] == "A5"]
        self.assertIn("subtitle_mode none", details)


if __name__ == "__main__":
    unittest.main()
