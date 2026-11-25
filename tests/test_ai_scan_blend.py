# AI の山を採点へ混ぜる (docs/AiEditor/resolve2.md §6 / P3) の単体テスト
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・混ぜるのは total だけ。emotion / comment は機械の値のまま (§6.1)
#     採点グラフの内訳と best_window_for_range がこの 2 つを見ているため
#   ・ai_weight = 0 なら**完全に従来どおり** (経路 B を変えない)
#   ・既定は 0.3 (resolve2.md §14.2 の全長スイープの実測で決めた)
#   ・analyze(scan=None) は今までと同じ挙動 (回帰)
#   ・スイープが失敗しても採点は止めない (§9)
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


class BlendTest(unittest.TestCase):

    def setUp(self):
        self.curve = [_window(0.0, 180.0, 80.0), _window(45.0, 225.0, 60.0),
                      _window(400.0, 580.0, 20.0)]

    # 重なる窓だけ混ざる。重ならない窓は機械の値のまま
    def test_only_overlapping_windows_change(self):
        moments = [_moment(100.0, 150.0, 40.0)]
        blended = scoring.blend_ai_moments(self.curve, moments, 0.5)
        # 0-180 と 45-225 は重なる / 400-580 は重ならない
        self.assertEqual(blended[0]["total"], 0.5 * 40.0 + 0.5 * 80.0)
        self.assertEqual(blended[1]["total"], 0.5 * 40.0 + 0.5 * 60.0)
        self.assertEqual(blended[2]["total"], 0.5 * 0.0 + 0.5 * 20.0)

    # emotion / comment は触らない (採点グラフの内訳とセクション追加が見る)
    def test_emotion_and_comment_are_untouched(self):
        blended = scoring.blend_ai_moments(
            self.curve, [_moment(0.0, 600.0, 90.0)], 0.7)
        for before, after in zip(self.curve, blended):
            self.assertEqual(after["emotion"], before["emotion"])
            self.assertEqual(after["comment"], before["comment"])

    # ai_score と moment_index を足す (消さない)
    def test_adds_the_ai_columns(self):
        moments = [_moment(0.0, 50.0, 30.0), _moment(100.0, 150.0, 70.0)]
        blended = scoring.blend_ai_moments(self.curve, moments, 0.5)
        # 窓 0-180 は両方に重なる → 強いほう (70) を採る
        self.assertEqual(blended[0]["ai_score"], 70.0)
        self.assertEqual(blended[0]["moment_index"], 1)
        # 重ならない窓は 0 / None
        self.assertEqual(blended[2]["ai_score"], 0.0)
        self.assertIsNone(blended[2]["moment_index"])

    # ai_weight = 0 なら従来どおり (混ぜない指定)
    def test_zero_weight_keeps_the_curve(self):
        blended = scoring.blend_ai_moments(
            self.curve, [_moment(0.0, 600.0, 90.0)], 0.0)
        self.assertEqual(blended, self.curve)

    # ai_weight = 1 なら AI の点がそのまま出る
    def test_full_weight_uses_the_ai_score(self):
        blended = scoring.blend_ai_moments(
            self.curve, [_moment(0.0, 180.0, 90.0)], 1.0)
        self.assertEqual(blended[0]["total"], 90.0)

    # 範囲外の比率は 0..1 へ丸める
    def test_weight_is_clamped(self):
        high = scoring.blend_ai_moments(self.curve, [_moment(0.0, 180.0, 90.0)], 5.0)
        self.assertEqual(high[0]["total"], 90.0)
        low = scoring.blend_ai_moments(self.curve, [_moment(0.0, 180.0, 90.0)], -1.0)
        self.assertEqual(low, self.curve)

    # 山が無ければそのまま
    def test_without_moments(self):
        self.assertEqual(scoring.blend_ai_moments(self.curve, [], 0.7), self.curve)
        self.assertEqual(scoring.blend_ai_moments(self.curve, None, 0.7), self.curve)

    # 元の列を書き換えない (呼び出し側の控えを壊さない)
    def test_does_not_mutate_the_input(self):
        before = [dict(row) for row in self.curve]
        scoring.blend_ai_moments(self.curve, [_moment(0.0, 180.0, 90.0)], 0.7)
        self.assertEqual(self.curve, before)


class AiScanWeightTest(unittest.TestCase):

    def test_reads_the_setting(self):
        settings = {"ai": {"scan": {"ai_weight": 0.7}}}
        self.assertEqual(archive_pipeline.ai_scan_weight(settings), 0.7)

    # 既定は 0.3 (resolve2.md §14.2)。設定の読み込みは浅いマージのため、
    # 古い設定ファイルには鍵が無い。そのときも意図した既定で動く必要がある
    def test_defaults_to_the_shipped_weight(self):
        self.assertEqual(archive_pipeline.ai_scan_weight({}), 0.3)
        self.assertEqual(archive_pipeline.ai_scan_weight(None), 0.3)
        self.assertEqual(archive_pipeline.ai_scan_weight({"ai": {}}), 0.3)
        self.assertEqual(archive_pipeline.ai_scan_weight({"ai": {"scan": {}}}), 0.3)

    # 0 を**明示**したときは混ぜない (欠落と区別する)
    def test_explicit_zero_is_honored(self):
        self.assertEqual(
            archive_pipeline.ai_scan_weight({"ai": {"scan": {"ai_weight": 0}}}), 0.0)

    def test_clamps_and_survives_junk(self):
        self.assertEqual(
            archive_pipeline.ai_scan_weight({"ai": {"scan": {"ai_weight": 9}}}), 1.0)
        self.assertEqual(
            archive_pipeline.ai_scan_weight({"ai": {"scan": {"ai_weight": "x"}}}), 0.3)


class ShippedDefaultTest(unittest.TestCase):
    """同梱の既定値が 3 か所で揃っていること (黙って戻るのを防ぐ)"""

    def test_the_three_defaults_agree(self):
        import json
        import os

        from src.ai import config as ai_config
        from src.settings import settings_window

        self.assertEqual(ai_config.DEFAULT_AI["scan"]["ai_weight"], 0.3)
        self.assertEqual(
            settings_window.DEFAULT_SETTINGS["ai"]["scan"]["ai_weight"], 0.3)

        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "src", "settings", "setting.json")
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["ai"]["scan"]["ai_weight"], 0.3)

    # archive/pipeline の落としどころも同じ値であること
    def test_the_fallback_agrees(self):
        from src.ai import config as ai_config

        self.assertEqual(archive_pipeline._DEFAULT_AI_WEIGHT,
                         ai_config.DEFAULT_AI["scan"]["ai_weight"])


class RunScanTest(unittest.TestCase):
    """スイープが失敗しても採点は止めない (§9)"""

    def test_none_means_no_scan(self):
        self.assertEqual(
            archive_pipeline._run_scan(None, "v.mp4", {}, [], [], 100.0), [])

    def test_failure_is_swallowed(self):
        def boom(*args):
            raise RuntimeError("AI が落ちた")

        self.assertEqual(
            archive_pipeline._run_scan(boom, "v.mp4", {}, [], [], 100.0), [])

    def test_result_is_passed_through(self):
        moments = [_moment(0.0, 10.0, 50.0)]
        self.assertEqual(
            archive_pipeline._run_scan(lambda *a: moments, "v.mp4", {}, [], [], 100.0),
            moments)

    # 渡される引数の形 (pipeline → ai/ の向きを作らないための契約)
    def test_call_signature(self):
        seen = {}

        def capture(input_path, settings, cells, comments, duration):
            seen.update(input_path=input_path, settings=settings, cells=cells,
                        comments=comments, duration=duration)

            return []

        archive_pipeline._run_scan(capture, "v.mp4", {"a": 1}, ["cell"], ["c"], 42.0)
        self.assertEqual(seen["input_path"], "v.mp4")
        self.assertEqual(seen["duration"], 42.0)
        self.assertEqual(seen["cells"], ["cell"])


if __name__ == "__main__":
    unittest.main()
