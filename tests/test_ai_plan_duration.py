# 尺の基準を「出力尺」に揃え、候補を尺で積み上げる (docs/AiEditor/resolve3.md §9)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・ai.plan.<target>.min_sec / max_sec は**出力尺** (無音カット後)。
#     AI へ渡す前・検証する前に残存率で素材尺へ割り戻す
#   ・候補は**点の高い順に積み、目標素材尺に達したら終了**する (利用者の指示 2026-11-26)
#   ・尺の下限未達は major。minor だと min_severity=major で弾かれて誰も直さない
#   ・目標が無い呼び出し (経路 B) は従来どおり top_n 件で打ち切る
# ffmpeg も AI も呼ばない。
import json
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.ai import config as ai_config
from src.ai import reviewer
from src.archive import pipeline as archive_pipeline
from src.archive import scoring


def _window(start, end, total, emotion=50.0, comment=10.0):
    return {"start": start, "end": end, "total": total,
            "emotion": emotion, "comment": comment}


def _cell(start, end, silence_ratio=0.0):
    return {"start": start, "end": end, "silence_ratio": silence_ratio}


class SourceSecTest(unittest.TestCase):
    """出力尺 → 素材尺の換算"""

    def test_converts_with_the_kept_ratio(self):
        self.assertEqual(1680.0, ai_config.source_sec(840, 0.5))
        self.assertEqual(1200.0, ai_config.source_sec(600, 0.5))

    def test_ratio_one_does_not_convert(self):
        self.assertEqual(840.0, ai_config.source_sec(840, 1.0))
        self.assertEqual(840.0, ai_config.source_sec(840))

    # 0 や未設定は 0 のまま (上限なし・下限なしの意味を保つ)
    def test_zero_stays_zero(self):
        self.assertEqual(0.0, ai_config.source_sec(0, 0.5))
        self.assertEqual(0.0, ai_config.source_sec(None, 0.5))

    # 極端な残存率で割って無限大にしない
    def test_ratio_is_clamped(self):
        self.assertEqual(840.0 / ai_config.MIN_KEPT_RATIO,
                         ai_config.source_sec(840, 0.0001))
        self.assertEqual(840.0, ai_config.source_sec(840, 5.0), "1.0 を超えない")

    def test_survives_junk(self):
        self.assertEqual(0.0, ai_config.source_sec("x", 0.5))
        self.assertEqual(840.0, ai_config.source_sec(840, "x"))


class KeptRatioTest(unittest.TestCase):
    """セルの silence_ratio から残存率を見積もる"""

    def test_no_silence_keeps_everything(self):
        cells = [_cell(0, 45), _cell(45, 90)]
        self.assertEqual(1.0, archive_pipeline.kept_ratio(cells))

    def test_half_silence(self):
        cells = [_cell(0, 45, 0.5), _cell(45, 90, 0.5)]
        self.assertEqual(0.5, archive_pipeline.kept_ratio(cells))

    # セルの長さで重みが付く (長いセルの無音ほど効く)
    def test_is_weighted_by_cell_length(self):
        cells = [_cell(0, 90, 1.0), _cell(90, 135, 0.0)]
        self.assertAlmostEqual(1.0 / 3.0, archive_pipeline.kept_ratio(cells), places=4)

    def test_empty_means_no_conversion(self):
        self.assertEqual(1.0, archive_pipeline.kept_ratio([]))
        self.assertEqual(1.0, archive_pipeline.kept_ratio(None))

    # 全部無音でも下限で止める (0 除算と極端な換算を防ぐ)
    def test_lower_bound(self):
        cells = [_cell(0, 45, 1.0)]
        self.assertEqual(archive_pipeline._MIN_KEPT_RATIO,
                         archive_pipeline.kept_ratio(cells))


class CandidateTargetTest(unittest.TestCase):
    """集める素材尺の目標 = 最長プリセットの出力目標 ÷ 残存率 × 余裕"""

    def _settings(self, headroom=2.0, **plan):
        plan.setdefault("targets", ["youtube_long", "youtube_shorts"])
        plan.setdefault("youtube_long", {"min_sec": 840})
        plan.setdefault("youtube_shorts", {"min_sec": 15})

        return {"ai": {"plan": plan},
                "archive": {"scoring": {"target_headroom": headroom}}}

    def test_uses_the_longest_preset(self):
        self.assertEqual(3360.0,
                         archive_pipeline.candidate_target_sec(self._settings(), 0.5))

    def test_headroom_applies(self):
        self.assertEqual(1680.0,
                         archive_pipeline.candidate_target_sec(
                             self._settings(headroom=1.0), 0.5))

    # 余裕は 1 未満にしない (目標より少なく集めては意味が無い)
    def test_headroom_below_one_is_ignored(self):
        self.assertEqual(1680.0,
                         archive_pipeline.candidate_target_sec(
                             self._settings(headroom=0.1), 0.5))

    # 尺指定が無ければ 0 = 従来どおり top_n で打ち切る
    def test_without_min_sec_there_is_no_target(self):
        settings = {"ai": {"plan": {"targets": ["youtube_long"],
                                    "youtube_long": {"max_sec": 900}}}}
        self.assertEqual(0.0, archive_pipeline.candidate_target_sec(settings, 0.5))
        self.assertEqual(0.0, archive_pipeline.candidate_target_sec({}, 0.5))
        self.assertEqual(0.0, archive_pipeline.candidate_target_sec(None, 0.5))

    def test_survives_junk(self):
        settings = {"ai": {"plan": {"targets": ["youtube_long", 7, "missing"],
                                    "youtube_long": {"min_sec": "x"}}}}
        self.assertEqual(0.0, archive_pipeline.candidate_target_sec(settings, 0.5))


class SelectByDurationTest(unittest.TestCase):
    """候補の積み上げ: 点の高い順に積み、目標に達したら終了する"""

    def setUp(self):
        # 180 秒の窓を 5 本。点は降順、互いに重ならない
        self.scored = [_window(i * 200.0, i * 200.0 + 180.0, 90.0 - i * 10.0)
                       for i in range(5)]

    def test_stops_at_the_target(self):
        clips = scoring.select_top_events(self.scored, 10, 0.0, 2000.0,
                                          target_total_sec=400.0)
        # 180 + 180 + 180 = 540 ≧ 400 で終了
        self.assertEqual(3, len(clips))
        self.assertEqual([0.0, 200.0, 400.0], [c["start"] for c in clips])

    # 目標があるときは top_n で切らない (件数ではなく尺で決める)
    def test_top_n_does_not_cap_the_target(self):
        clips = scoring.select_top_events(self.scored, 1, 0.0, 2000.0,
                                          target_total_sec=400.0)
        self.assertEqual(3, len(clips))

    # 点の高い順に積む (時間順ではない)
    def test_picks_by_score(self):
        scored = [_window(0.0, 180.0, 10.0), _window(200.0, 380.0, 90.0)]
        clips = scoring.select_top_events(scored, 10, 0.0, 2000.0,
                                          target_total_sec=100.0)
        self.assertEqual([200.0], [c["start"] for c in clips])

    # 素材が尽きたら目標未満で終わる (無い物は選べない)
    def test_runs_out_of_material(self):
        clips = scoring.select_top_events(self.scored, 10, 0.0, 2000.0,
                                          target_total_sec=10000.0)
        self.assertEqual(5, len(clips))

    # 目標が無ければ従来どおり top_n 件 (経路 B の回帰)
    def test_without_target_top_n_applies(self):
        clips = scoring.select_top_events(self.scored, 2, 0.0, 2000.0)
        self.assertEqual(2, len(clips))
        self.assertEqual(2, len(scoring.select_top_events(
            self.scored, 2, 0.0, 2000.0, target_total_sec=0.0)))


class QaDurationTest(unittest.TestCase):
    """尺の機械検査: 下限未達は major。基準は素材尺へ換算する"""

    def _settings(self):
        return {"ai": {"plan": {"youtube_long": {"min_sec": 840, "max_sec": 1200,
                                                 "max_scenes": 12}}}}

    def _plan(self, total_sec):
        return {"scenes": [{"id": "s1", "source_start": 0.0,
                            "source_end": float(total_sec)}],
                "subtitles": [], "sounds": []}

    # 素材 192 秒 (2026-11-26 の実測) は下限 1680 秒に届かない → major
    def test_short_plan_is_major(self):
        checks = reviewer.qa_checks(self._plan(192.3), self._settings(),
                                    "youtube_long", 0.5)
        duration = [c for c in checks if c["kind"] == "duration"]

        self.assertTrue(duration, "尺の指摘が出る")
        self.assertEqual("major", duration[0]["severity"])
        self.assertIn("1680", duration[0]["detail"])

    # 換算後の下限を満たしていれば指摘しない
    def test_long_enough_plan_has_no_check(self):
        checks = reviewer.qa_checks(self._plan(1700.0), self._settings(),
                                    "youtube_long", 0.5)
        self.assertEqual([], [c for c in checks if c["kind"] == "duration"])

    # 上限も換算される (1200 秒 → 素材 2400 秒)
    def test_upper_bound_is_converted(self):
        checks = reviewer.qa_checks(self._plan(2500.0), self._settings(),
                                    "youtube_long", 0.5)
        duration = [c for c in checks if c["kind"] == "duration"]

        self.assertEqual("major", duration[0]["severity"])
        self.assertIn("2400", duration[0]["detail"])


class ShippedDefaultTest(unittest.TestCase):
    """同梱の既定値が 3 か所で揃っていること"""

    def setUp(self):
        from src.settings import settings_window

        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "src", "settings", "setting.json")
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.sources = (ai_config.DEFAULT_AI,
                        settings_window.DEFAULT_SETTINGS["ai"],
                        saved["ai"])
        self.scoring_sources = (
            settings_window.DEFAULT_SETTINGS["archive"]["scoring"],
            saved["archive"]["scoring"])

    # 14 分 = 840 秒 (出力尺 / 利用者の指示 2026-11-26)
    def test_youtube_long_targets_fourteen_minutes(self):
        for source in self.sources:
            values = source["plan"]["youtube_long"]
            self.assertEqual(840, values["min_sec"])
            self.assertEqual(1200, values["max_sec"])

    def test_headroom_agrees(self):
        for source in self.scoring_sources:
            self.assertEqual(2.0, source["target_headroom"])

    def test_the_fallback_agrees(self):
        self.assertEqual(archive_pipeline._DEFAULT_TARGET_HEADROOM, 2.0)
        self.assertEqual(archive_pipeline._MIN_KEPT_RATIO,
                         ai_config.MIN_KEPT_RATIO)


if __name__ == "__main__":
    unittest.main()
