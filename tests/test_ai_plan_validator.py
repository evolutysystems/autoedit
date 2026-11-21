# EditPlan の検証の単体テスト (docs/AiEditor/resolve1.md §10 / §20)
# 実行: python -m unittest discover -s tests
#
# 要件 (V1〜V14 の各違反が「落とす / 丸める / 切る / 例外」のどれになるか):
#   ・V6  start >= end                     → その要素を落とす
#   ・V7  VOD の尺の外                     → 収まる範囲へ切る / 全部外なら落とす
#   ・V8  素材が手元に無い                 → 取り直す。取れなければ落とす
#   ・V9  最短尺を下回る                   → 落とす
#   ・V10 合計尺・本数の上限               → 後ろの body から落とし、山と入りは残す
#   ・V11 テロップがシーンの外             → はみ出しを切る / 完全に外なら落とす
#   ・V12 テロップ同士の重なり             → 後ろをずらす (コメント役割は別トラック)
#   ・V4  テロップの文字数の上限           → 詰める
#   ・V13 SE の本数の上限                  → 超過分を落とす
#   ・V14 プリセットが無い / ファイルが無い → 落とす
#   ・使えるシーンが 1 つも残らない        → AiPlanError
#   ・手書きの EditPlan は素材を問わず通る (Phase 0 の 4 番)
# ネットワークもプロバイダーも使わない。ファイルの実在確認は差し替える。
import logging
import unittest

from src.ai import plan_schema as ps
from src.ai import plan_validator as pv
from src.exceptions import AiPlanError

_LONG = {"ai": {"plan": {"youtube_long": {"min_sec": 0, "max_sec": 0,
                                          "max_scenes": 0}}}}


def _scene(scene_id, start, end, role="body", **extra):
    scene = {"id": scene_id, "source_start": start, "source_end": end, "role": role}
    scene.update(extra)

    return scene


def _plan(scenes, target="youtube_long", duration_sec=3600.0, **overrides):
    plan = ps.new_plan(target, {"kind": "vod", "video_id": "1"},
                       duration_sec=duration_sec)
    plan["scenes"] = scenes
    plan.update(overrides)

    return plan


def _checks(report):
    return [n["check"] for n in report.notes]


class SceneRangeTest(unittest.TestCase):

    # 手書きの EditPlan は素材の有無を問わずそのまま通る (§21 Phase 0 の 4 番)。
    def test_hand_written_plan_passes(self):
        plan, report = pv.validate(
            _plan([_scene("sc1", 10.0, 25.0, "opening")]), settings=_LONG)

        self.assertEqual(1, len(plan["scenes"]))
        self.assertEqual([], report.notes)
        self.assertEqual("", report.message())

    def test_reversed_range_is_dropped(self):
        with self.assertRaises(AiPlanError):
            pv.validate(_plan([_scene("sc1", 25.0, 10.0)]), settings=_LONG)

    def test_range_beyond_the_vod_is_cut(self):
        plan, report = pv.validate(
            _plan([_scene("sc1", 3590.0, 3700.0)]), duration_sec=3600.0,
            settings=_LONG)

        self.assertEqual(3600.0, plan["scenes"][0]["source_end"])
        self.assertIn("V7", _checks(report))

    def test_range_outside_the_vod_is_dropped(self):
        plan, report = pv.validate(
            _plan([_scene("sc1", 4000.0, 4100.0), _scene("sc2", 10.0, 20.0)]),
            duration_sec=3600.0, settings=_LONG)

        self.assertEqual(["sc2"], [s["id"] for s in plan["scenes"]])
        self.assertEqual(1, report.dropped)

    # 尺が 0 のときは V7 を飛ばす (尺が分からないまま切ると素材を削る)。
    def test_unknown_duration_skips_the_range_check(self):
        plan, _ = pv.validate(_plan([_scene("sc1", 4000.0, 4100.0)], duration_sec=0.0),
                              settings=_LONG)

        self.assertEqual(4100.0, plan["scenes"][0]["source_end"])

    def test_too_short_scene_is_dropped(self):
        plan, report = pv.validate(
            _plan([_scene("sc1", 10.0, 10.02), _scene("sc2", 20.0, 30.0)]),
            settings=_LONG)

        self.assertEqual(["sc2"], [s["id"] for s in plan["scenes"]])
        self.assertIn("V9", _checks(report))

    def test_min_clip_sec_comes_from_the_settings(self):
        settings = dict(_LONG)
        settings["timeline"] = {"min_clip_sec": 5.0}

        plan, report = pv.validate(
            _plan([_scene("sc1", 10.0, 12.0), _scene("sc2", 20.0, 30.0)]),
            settings=settings)

        self.assertEqual(["sc2"], [s["id"] for s in plan["scenes"]])

    def test_no_usable_scene_is_fatal(self):
        with self.assertRaises(AiPlanError):
            pv.validate(_plan([_scene("sc1", 4000.0, 4100.0)]), duration_sec=3600.0,
                        settings=_LONG)

    def test_order_is_renumbered_after_drops(self):
        plan, _ = pv.validate(
            _plan([_scene("a", 10.0, 20.0, order=1),
                   _scene("b", 4000.0, 4100.0, order=2),
                   _scene("c", 30.0, 40.0, order=3)]),
            duration_sec=3600.0, settings=_LONG)

        self.assertEqual([1, 2], [s["order"] for s in plan["scenes"]])
        self.assertEqual(["a", "c"], [s["id"] for s in plan["scenes"]])


class MaterialsTest(unittest.TestCase):

    def test_scene_without_media_is_dropped(self):
        materials = pv.Materials(ranges=[(0.0, 100.0)])

        plan, report = pv.validate(
            _plan([_scene("sc1", 10.0, 20.0), _scene("sc2", 500.0, 520.0)]),
            settings=_LONG, materials=materials)

        self.assertEqual(["sc1"], [s["id"] for s in plan["scenes"]])
        self.assertIn("V8", _checks(report))

    def test_missing_range_is_refetched(self):
        fetched = []

        def ensure(start, end):
            fetched.append((start, end))

            return True

        materials = pv.Materials(ranges=[(0.0, 100.0)], ensure=ensure)

        plan, report = pv.validate(_plan([_scene("sc1", 500.0, 520.0)]),
                                   settings=_LONG, materials=materials)

        self.assertEqual([(500.0, 520.0)], fetched)
        self.assertEqual(1, len(plan["scenes"]))
        self.assertIn("refetch", [n["action"] for n in report.notes])

    def test_failed_refetch_drops_the_scene(self):
        materials = pv.Materials(ranges=[(0.0, 100.0)],
                                 ensure=lambda start, end: False)

        plan, _ = pv.validate(
            _plan([_scene("sc1", 10.0, 20.0), _scene("sc2", 500.0, 520.0)]),
            settings=_LONG, materials=materials)

        self.assertEqual(["sc1"], [s["id"] for s in plan["scenes"]])

    # 取り直しが例外を投げても経路を止めない (落として続ける)。
    def test_refetch_error_is_contained(self):
        def ensure(start, end):
            raise RuntimeError("boom")

        materials = pv.Materials(ranges=[(0.0, 100.0)], ensure=ensure)

        plan, _ = pv.validate(
            _plan([_scene("sc1", 10.0, 20.0), _scene("sc2", 500.0, 520.0)]),
            settings=_LONG, materials=materials)

        self.assertEqual(["sc1"], [s["id"] for s in plan["scenes"]])


class DurationLimitTest(unittest.TestCase):

    def _shorts(self, max_sec=60, max_scenes=0):
        return {"ai": {"plan": {"youtube_shorts": {"min_sec": 0, "max_sec": max_sec,
                                                   "max_scenes": max_scenes}}}}

    # 後ろの body から削り、hook と punchline は残す。
    def test_body_is_dropped_before_the_hook(self):
        scenes = [_scene("hook", 0.0, 20.0, "hook"),
                  _scene("b1", 100.0, 130.0),
                  _scene("b2", 200.0, 230.0),
                  _scene("end", 300.0, 320.0, "punchline")]

        plan, report = pv.validate(_plan(scenes, target="youtube_shorts"),
                                   target="youtube_shorts",
                                   settings=self._shorts(max_sec=60))

        self.assertEqual(["hook", "end"], [s["id"] for s in plan["scenes"]])
        self.assertEqual(2, report.dropped)
        self.assertIn("V10", _checks(report))

    # 落とせる役割が無くなっても超えるときは最後の終わりを詰める。
    def test_the_last_scene_is_trimmed_when_nothing_can_be_dropped(self):
        scenes = [_scene("hook", 0.0, 30.0, "hook"),
                  _scene("end", 100.0, 150.0, "punchline")]

        plan, report = pv.validate(_plan(scenes, target="youtube_shorts"),
                                   target="youtube_shorts",
                                   settings=self._shorts(max_sec=60))

        self.assertEqual(2, len(plan["scenes"]))
        self.assertEqual(60.0, sum(s["source_end"] - s["source_start"]
                                   for s in plan["scenes"]))
        self.assertIn("cut", [n["action"] for n in report.notes])

    def test_scene_count_limit(self):
        scenes = [_scene("s{0}".format(i), i * 100.0, i * 100.0 + 10.0)
                  for i in range(6)]

        plan, report = pv.validate(_plan(scenes, target="youtube_shorts"),
                                   target="youtube_shorts",
                                   settings=self._shorts(max_sec=0, max_scenes=3))

        self.assertEqual(3, len(plan["scenes"]))
        self.assertEqual(["s0", "s1", "s2"], [s["id"] for s in plan["scenes"]])

    # 0 は上限なし。
    def test_zero_means_no_limit(self):
        scenes = [_scene("s{0}".format(i), i * 100.0, i * 100.0 + 60.0)
                  for i in range(8)]

        plan, _ = pv.validate(_plan(scenes, target="youtube_shorts"),
                              target="youtube_shorts",
                              settings=self._shorts(max_sec=0, max_scenes=0))

        self.assertEqual(8, len(plan["scenes"]))


class SubtitleTest(unittest.TestCase):

    def _with_subtitles(self, subtitles, scenes=None, settings=None):
        scenes = scenes or [_scene("sc1", 100.0, 130.0)]
        merged = dict(_LONG)
        merged.update(settings or {})

        return pv.validate(_plan(scenes, subtitles=subtitles), settings=merged)

    def test_subtitle_is_cut_to_the_scene(self):
        plan, report = self._with_subtitles([
            {"scene_id": "sc1", "source_start": 95.0, "source_end": 105.0,
             "text": "はみ出し"}])

        subtitle = plan["subtitles"][0]
        self.assertEqual(100.0, subtitle["source_start"])
        self.assertIn("V11", _checks(report))

    def test_subtitle_outside_the_scene_is_dropped(self):
        plan, report = self._with_subtitles([
            {"scene_id": "sc1", "source_start": 10.0, "source_end": 20.0,
             "text": "別の場所"}])

        self.assertEqual([], plan["subtitles"])
        self.assertEqual(1, report.dropped)

    def test_subtitle_of_a_dropped_scene_is_dropped(self):
        plan, _ = self._with_subtitles(
            [{"scene_id": "sc2", "source_start": 4005.0, "source_end": 4006.0,
              "text": "落ちるシーンの分"}],
            scenes=[_scene("sc1", 100.0, 130.0), _scene("sc2", 4000.0, 4010.0)])

        self.assertEqual([], plan["subtitles"])
        self.assertEqual(1, len(plan["scenes"]))

    def test_overlapping_subtitles_are_shifted(self):
        plan, report = self._with_subtitles([
            {"scene_id": "sc1", "source_start": 101.0, "source_end": 104.0,
             "text": "前"},
            {"scene_id": "sc1", "source_start": 103.0, "source_end": 106.0,
             "text": "後"}])

        starts = [s["source_start"] for s in plan["subtitles"]]
        self.assertEqual([101.0, 104.0], starts)
        self.assertIn("shift", [n["action"] for n in report.notes])

    # コメント役割は専用トラック (S2) へ置き分かれるため重なってよい。
    def test_comment_role_does_not_collide_with_the_streamer(self):
        plan, report = self._with_subtitles([
            {"scene_id": "sc1", "source_start": 101.0, "source_end": 104.0,
             "text": "本人", "track_role": "own"},
            {"scene_id": "sc1", "source_start": 101.0, "source_end": 104.0,
             "text": "コメント", "track_role": "comment"}])

        self.assertEqual([101.0, 101.0],
                         [s["source_start"] for s in plan["subtitles"]])
        self.assertEqual([], report.notes)

    def test_subtitle_without_room_is_dropped(self):
        plan, report = self._with_subtitles([
            {"scene_id": "sc1", "source_start": 100.0, "source_end": 129.98,
             "text": "ほぼ全部"},
            {"scene_id": "sc1", "source_start": 129.5, "source_end": 130.0,
             "text": "ずらすと入らない"}])

        self.assertEqual(1, len(plan["subtitles"]))
        self.assertIn("V12", _checks(report))

    # 上限文字数 = max_line_length × subtitle_max_lines
    def test_long_text_is_shortened(self):
        plan, report = self._with_subtitles(
            [{"scene_id": "sc1", "source_start": 101.0, "source_end": 104.0,
              "text": "あ" * 80}],
            settings={"subtitle": {"max_line_length": 20, "subtitle_max_lines": 2}})

        self.assertEqual(40, len(plan["subtitles"][0]["text"]))
        self.assertIn("V4", _checks(report))

    # 手動改行 (\N) は文字数に数えない。
    def test_manual_breaks_are_not_counted(self):
        text = "あ" * 20 + "\\N" + "い" * 20
        plan, report = self._with_subtitles(
            [{"scene_id": "sc1", "source_start": 101.0, "source_end": 104.0,
              "text": text}],
            settings={"subtitle": {"max_line_length": 20, "subtitle_max_lines": 2}})

        self.assertEqual(text, plan["subtitles"][0]["text"])
        self.assertNotIn("V4", _checks(report))


class SoundTest(unittest.TestCase):

    def _settings(self, presets=None, enabled=True, max_mix_inputs=32):
        return {
            "ai": {"plan": {"youtube_long": {"min_sec": 0, "max_sec": 0,
                                             "max_scenes": 0}},
                   "sounds": {"enabled": enabled,
                              "presets": presets if presets is not None
                              else {"surprise": "C:/se/surprise.wav"}}},
            "timeline": {"sound_tracks": {"max_mix_inputs": max_mix_inputs}},
        }

    def _validate(self, sounds, settings=None, exists=None):
        return pv.validate(
            _plan([_scene("sc1", 100.0, 130.0)], sounds=sounds),
            settings=settings or self._settings(),
            exists=exists if exists is not None else (lambda path: True))

    def test_registered_preset_is_kept_with_its_path(self):
        plan, report = self._validate(
            [{"scene_id": "sc1", "offset_sec": 0.4, "preset": "surprise",
              "gain_db": -3.0}])

        self.assertEqual("C:/se/surprise.wav", plan["sounds"][0]["path"])
        self.assertEqual(-3.0, plan["sounds"][0]["gain_db"])
        self.assertEqual([], report.notes)

    def test_unknown_preset_is_dropped(self):
        plan, report = self._validate(
            [{"scene_id": "sc1", "offset_sec": 0.4, "preset": "nope"}])

        self.assertEqual([], plan["sounds"])
        self.assertIn("V14", _checks(report))

    def test_missing_file_is_dropped(self):
        plan, report = self._validate(
            [{"scene_id": "sc1", "offset_sec": 0.4, "preset": "surprise"}],
            exists=lambda path: False)

        self.assertEqual([], plan["sounds"])
        self.assertIn("V14", _checks(report))

    # 既定ではプリセットが空であり、SE は何も載らない (回答 Q9 / T6)。
    def test_no_presets_means_no_sounds(self):
        plan, _ = self._validate(
            [{"scene_id": "sc1", "offset_sec": 0.4, "preset": "surprise"}],
            settings=self._settings(presets={}))

        self.assertEqual([], plan["sounds"])

    def test_disabled_sounds_are_not_applied(self):
        plan, _ = self._validate(
            [{"scene_id": "sc1", "offset_sec": 0.4, "preset": "surprise"}],
            settings=self._settings(enabled=False))

        self.assertEqual([], plan["sounds"])

    # SE はシーン先頭からの相対秒 (§9.2)。シーンの外に鳴らす指示は落とす。
    def test_offset_beyond_the_scene_is_dropped(self):
        plan, report = self._validate(
            [{"scene_id": "sc1", "offset_sec": 90.0, "preset": "surprise"}])

        self.assertEqual([], plan["sounds"])
        self.assertIn("V6", _checks(report))

    def test_mix_input_limit(self):
        sounds = [{"scene_id": "sc1", "offset_sec": float(i), "preset": "surprise"}
                  for i in range(5)]

        plan, report = self._validate(
            sounds, settings=self._settings(max_mix_inputs=2))

        self.assertEqual(2, len(plan["sounds"]))
        self.assertIn("V13", _checks(report))


class ReportTest(unittest.TestCase):

    def test_message_counts_only_the_drops(self):
        report = pv.PlanReport()
        report.add("V7", "scenes[0]", ps.ACTION_CUT, "10.0")
        report.add("V9", "scenes[1]", ps.ACTION_DROP, "0.01")

        self.assertEqual(1, report.dropped)
        self.assertEqual(2, report.changed)
        self.assertIn("1", report.message())

    def test_no_message_when_nothing_was_dropped(self):
        report = pv.PlanReport()
        report.add("V7", "scenes[0]", ps.ACTION_CUT, "10.0")

        self.assertEqual("", report.message())

    # ログは 1 行。テロップ本文は入れない (§17.2)。
    def test_log_is_one_line_without_bodies(self):
        secret = "SENSITIVE-SUBTITLE"
        plan = _plan([_scene("sc1", 100.0, 130.0)],
                     subtitles=[{"scene_id": "sc1", "source_start": 10.0,
                                 "source_end": 20.0, "text": secret}])

        with self.assertLogs("src.ai.plan_validator", level=logging.INFO) as captured:
            pv.validate(plan, settings=_LONG)

        self.assertEqual(1, len(captured.output))
        self.assertNotIn(secret, captured.output[0])

    def test_nothing_is_logged_for_a_clean_plan(self):
        with self.assertNoLogs("src.ai.plan_validator", level=logging.INFO):
            pv.validate(_plan([_scene("sc1", 100.0, 130.0)]), settings=_LONG)


if __name__ == "__main__":
    unittest.main()
