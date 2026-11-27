# 採点を AI の点で行う (docs/AiEditor/resolve3.md) の単体テスト
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・ai_only は窓の total を AI の点だけで置き換える。重なりの深さで差を付ける (§3.2)
#   ・emotion / comment は機械の値のまま。方式 A の点は machine_total へ残す (§0-1)
#   ・**方式 A の採点そのものは消さない。** moments が空・スイープ失敗・machine モードでは
#     total が方式 A のままであること (= 縮退 / §3.5)
#   ・同点は machine_total の降順で解く (§3.2)
#   ・既定は ai_only / ai_weight 0.5 / scene_eval 有効 (§4)
# ffmpeg も AI も呼ばない。
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import pipeline as archive_pipeline
from src.archive import scoring


def _window(start, end, total, emotion=50.0, comment=10.0):
    return {"start": start, "end": end, "total": total,
            "emotion": emotion, "comment": comment}


def _moment(start, end, score):
    return {"start": start, "end": end, "scan_score": score}


class ScoreByAiMomentsTest(unittest.TestCase):

    def setUp(self):
        self.curve = [_window(0.0, 180.0, 80.0), _window(45.0, 225.0, 60.0),
                      _window(400.0, 580.0, 20.0)]

    # 山を丸ごと含む窓は満点 / 重ならない窓は 0
    def test_full_overlap_takes_the_whole_score(self):
        scored = scoring.score_by_ai_moments(self.curve, [_moment(100.0, 150.0, 40.0)])
        self.assertEqual(scored[0]["total"], 40.0)
        self.assertEqual(scored[1]["total"], 40.0)
        self.assertEqual(scored[2]["total"], 0.0)

    # 端をかすめる窓は重なり率に比例する (ai_weight=1.0 のような「全部同点」を避ける)
    def test_partial_overlap_is_proportional(self):
        scored = scoring.score_by_ai_moments(self.curve, [_moment(150.0, 400.0, 60.0)])
        # 窓 0-180: 重なり 30s / 山 250s・窓 180s → 30/180 = 0.1667
        self.assertEqual(scored[0]["total"], 10.0)
        # 窓 45-225: 重なり 75s → 75/180 = 0.4167
        self.assertEqual(scored[1]["total"], 25.0)
        # 窓 400-580: 接しているだけで重なっていない
        self.assertEqual(scored[2]["total"], 0.0)

    # 同点にならない = 順位が付く (resolve2.md §14.2 で 1.0 が不可とされた理由の解消)
    def test_overlapping_windows_are_not_tied(self):
        scored = scoring.score_by_ai_moments(self.curve, [_moment(150.0, 400.0, 60.0)])
        self.assertNotEqual(scored[0]["total"], scored[1]["total"])

    # emotion / comment は触らない (採点グラフの内訳とセクション追加が見る)
    def test_emotion_and_comment_are_untouched(self):
        scored = scoring.score_by_ai_moments(self.curve, [_moment(0.0, 600.0, 90.0)])
        for before, after in zip(self.curve, scored):
            self.assertEqual(after["emotion"], before["emotion"])
            self.assertEqual(after["comment"], before["comment"])

    # 方式 A の点を machine_total へ残す (消さない / 同点の解きに使う)
    def test_keeps_the_machine_total(self):
        scored = scoring.score_by_ai_moments(self.curve, [_moment(0.0, 600.0, 90.0)])
        self.assertEqual([row["machine_total"] for row in scored], [80.0, 60.0, 20.0])

    # 重なる山が複数なら点の高いほうを採る
    def test_strongest_moment_wins(self):
        moments = [_moment(0.0, 180.0, 30.0), _moment(0.0, 180.0, 70.0)]
        scored = scoring.score_by_ai_moments(self.curve, moments)
        self.assertEqual(scored[0]["ai_score"], 70.0)
        self.assertEqual(scored[0]["moment_index"], 1)

    # 山が無ければ方式 A のまま (縮退)
    def test_without_moments(self):
        self.assertEqual(scoring.score_by_ai_moments(self.curve, []), self.curve)
        self.assertEqual(scoring.score_by_ai_moments(self.curve, None), self.curve)

    # 元の列を書き換えない (呼び出し側の控えを壊さない)
    def test_does_not_mutate_the_input(self):
        before = [dict(row) for row in self.curve]
        scoring.score_by_ai_moments(self.curve, [_moment(0.0, 180.0, 90.0)])
        self.assertEqual(self.curve, before)


class TieBreakTest(unittest.TestCase):
    """0 点同士は方式 A の点で解く (ai_only では 0 点の窓が大量に出る / §3.2)"""

    def test_machine_total_breaks_the_tie(self):
        scored = [dict(_window(0.0, 180.0, 0.0), machine_total=20.0),
                  dict(_window(400.0, 580.0, 0.0), machine_total=80.0)]
        clips = scoring.select_top_events(scored, 1, 0.0, 1000.0)
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0]["start"], 400.0)

    # AI の点が付いている窓が優先される (同点解きが主キーを覆さない)
    def test_ai_score_still_wins(self):
        scored = [dict(_window(0.0, 180.0, 5.0), machine_total=10.0),
                  dict(_window(400.0, 580.0, 0.0), machine_total=90.0)]
        clips = scoring.select_top_events(scored, 1, 0.0, 1000.0)
        self.assertEqual(clips[0]["start"], 0.0)

    # machine_total を持たない従来の呼び出しでは並びが変わらない (回帰)
    def test_without_machine_total_the_order_is_unchanged(self):
        scored = [_window(0.0, 180.0, 10.0), _window(400.0, 580.0, 90.0)]
        clips = scoring.select_top_events(scored, 1, 0.0, 1000.0)
        self.assertEqual(clips[0]["start"], 400.0)


class ScoringModeTest(unittest.TestCase):

    # 既定は ai_only (利用者の指示 2026-11-26 / §4)
    def test_defaults_to_ai_only(self):
        for settings in ({}, None, {"ai": {}}, {"ai": {"scan": {}}}):
            self.assertEqual(archive_pipeline.scoring_mode(settings),
                             archive_pipeline.MODE_AI_ONLY)

    def test_reads_the_setting(self):
        self.assertEqual(
            archive_pipeline.scoring_mode({"ai": {"scan": {"scoring_mode": "blend"}}}),
            archive_pipeline.MODE_BLEND)
        self.assertEqual(
            archive_pipeline.scoring_mode({"ai": {"scan": {"scoring_mode": "machine"}}}),
            archive_pipeline.MODE_MACHINE)

    # 知らない値・空は既定へ落とす (黙って壊れた採点にしない)
    def test_junk_falls_back_to_the_default(self):
        for value in ("", "AI", "ai-only", 7, None):
            self.assertEqual(
                archive_pipeline.scoring_mode({"ai": {"scan": {"scoring_mode": value}}}),
                archive_pipeline.MODE_AI_ONLY)

    # 大文字・前後の空白は許す
    def test_is_lenient_about_case(self):
        self.assertEqual(
            archive_pipeline.scoring_mode({"ai": {"scan": {"scoring_mode": " Blend "}}}),
            archive_pipeline.MODE_BLEND)


def _cells(count=8, cell_sec=45.0):
    rows = []
    for index in range(count):
        rows.append({
            "start": index * cell_sec, "end": (index + 1) * cell_sec,
            # 窓ごとに違う点が出るよう音量に差を付ける
            "mean_db": -40.0 + index, "max_db": -10.0 + index,
            "silence_ratio": 0.0, "w_count": 0, "comment_count": 0,
        })

    return rows


def _settings(mode=None, weight=None):
    settings = {"archive": {"scoring": {"window_sec": 180, "slide_sec": 45,
                                        "top_n": 10}},
                "ai": {"scan": {}}}
    if mode is not None:
        settings["ai"]["scan"]["scoring_mode"] = mode
    if weight is not None:
        settings["ai"]["scan"]["ai_weight"] = weight

    return settings


class AnalyzeModeTest(unittest.TestCase):
    """analyze() のモード分岐。ffmpeg は呼ばない (features を差し替える)"""

    def setUp(self):
        self.cells = _cells()
        self.duration = 45.0 * len(self.cells)
        self.moment = _moment(45.0, 135.0, 70.0)
        self.addCleanup(setattr, archive_pipeline.features, "extract_cells",
                        archive_pipeline.features.extract_cells)
        self.addCleanup(setattr, archive_pipeline.ffmpeg_runner, "ensure_available",
                        archive_pipeline.ffmpeg_runner.ensure_available)
        archive_pipeline.features.extract_cells = (
            lambda path, settings, cell_sec, progress_cb=None:
            (self.duration, [dict(row) for row in self.cells], None))
        archive_pipeline.ffmpeg_runner.ensure_available = lambda cfg: None

    def _curve(self, settings, scan=None):
        return archive_pipeline.analyze("vod.mp4", settings, scan=scan)["curve"]

    # 窓 (180s / 45s スライド) が組めていること。土台の確認
    def test_windows_are_built(self):
        base = self._curve(_settings())
        self.assertEqual([(row["start"], row["end"]) for row in base],
                         [(0.0, 180.0), (45.0, 225.0), (90.0, 270.0),
                          (135.0, 315.0), (180.0, 360.0)])

    # ai_only: total が AI の点に置き換わり、方式 A の点は machine_total に残る
    def test_ai_only_replaces_the_total(self):
        base = self._curve(_settings())
        curve = self._curve(_settings(mode="ai_only"), scan=lambda *a: [self.moment])
        self.assertEqual(len(curve), len(base))
        for row, before in zip(curve, base):
            self.assertEqual(row["machine_total"], before["total"])
            self.assertEqual(row["emotion"], before["emotion"])
        # 山 45-135 (90s) を丸ごと含む窓は満点、半分だけの窓は半分、外は 0
        self.assertEqual([row["total"] for row in curve],
                         [70.0, 70.0, 35.0, 0.0, 0.0])

    # machine: 山があっても total を変えない (従来の挙動を残す)
    def test_machine_mode_keeps_method_a(self):
        base = self._curve(_settings())
        curve = self._curve(_settings(mode="machine"), scan=lambda *a: [self.moment])
        self.assertEqual([row["total"] for row in curve],
                         [row["total"] for row in base])
        self.assertNotIn("ai_score", curve[0])

    # blend: ai_weight で混ざる (resolve2.md §6.1 の挙動を残す)
    def test_blend_mode_mixes(self):
        base = self._curve(_settings())
        curve = self._curve(_settings(mode="blend", weight=0.5),
                            scan=lambda *a: [self.moment])
        self.assertAlmostEqual(curve[0]["total"],
                               0.5 * 70.0 + 0.5 * base[0]["total"], places=1)

    # 山が 0 件なら ai_only でも方式 A のまま (縮退)
    def test_no_moments_falls_back_to_method_a(self):
        base = self._curve(_settings())
        curve = self._curve(_settings(mode="ai_only"), scan=lambda *a: [])
        self.assertEqual([row["total"] for row in curve],
                         [row["total"] for row in base])

    # スイープが落ちても採点は止まらず方式 A で続く (§3.5 / resolve2.md §9)
    def test_scan_failure_falls_back_to_method_a(self):
        def boom(*args):
            raise RuntimeError("AI が落ちた")

        base = self._curve(_settings())
        curve = self._curve(_settings(mode="ai_only"), scan=boom)
        self.assertEqual([row["total"] for row in curve],
                         [row["total"] for row in base])

    # scan を渡さない呼び出し (経路 B) は従来と完全に同じ
    def test_without_scan_nothing_changes(self):
        curve = self._curve(_settings(mode="ai_only"))
        self.assertNotIn("ai_score", curve[0])
        self.assertNotIn("machine_total", curve[0])


class ShippedDefaultTest(unittest.TestCase):
    """同梱の既定値が 3 か所で揃っていること (黙って戻るのを防ぐ)"""

    def setUp(self):
        import json

        from src.ai import config as ai_config
        from src.settings import settings_window

        self.ai_default = ai_config.DEFAULT_AI
        self.window_default = settings_window.DEFAULT_SETTINGS["ai"]
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "src", "settings", "setting.json")
        with open(path, encoding="utf-8") as handle:
            self.saved = json.load(handle)["ai"]

    def test_scoring_mode_agrees(self):
        for source in (self.ai_default, self.window_default, self.saved):
            self.assertEqual(source["scan"]["scoring_mode"],
                             archive_pipeline.MODE_AI_ONLY)

    # ②' を流れへ戻した (利用者の指示 2026-11-26 / §0-4)
    def test_scene_eval_is_enabled(self):
        for source in (self.ai_default, self.window_default, self.saved):
            self.assertTrue(source["scene_eval"]["enabled"])

    # archive/pipeline の落としどころも同じ値であること
    def test_the_fallback_agrees(self):
        self.assertEqual(archive_pipeline._DEFAULT_SCORING_MODE,
                         self.ai_default["scan"]["scoring_mode"])


class ConsentVersionTest(unittest.TestCase):
    """全長の静止画を送るため文面の版を上げた (§0-5 / resolve2.md §10)"""

    def test_version_was_raised(self):
        from src.ai import consent

        self.assertGreaterEqual(consent.CURRENT_VERSION, 2)

    # スイープが有効なら文面にその行が出る (挙動と文面を食い違わせない)
    def test_text_mentions_the_sweep(self):
        from src.ai import consent

        text = consent.text({"ai": {"scan": {"enabled": True,
                                             "frame_interval_sec": 30,
                                             "frame_edge_px": 512}}})
        self.assertIn("配信全体", text)

    def test_text_omits_the_sweep_when_disabled(self):
        from src.ai import consent

        text = consent.text({"ai": {"scan": {"enabled": False}}})
        self.assertNotIn("配信全体", text)


if __name__ == "__main__":
    unittest.main()
