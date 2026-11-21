# AI の利用量の記録と上限の単体テスト (docs/AiEditor/resolve1.md §17.2 / §17.3 / §20)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・usage の 4 項目と解決モデルが工程別に残る (ポイント単価を実測で決めるため / §22 T1)
#   ・入力側の合計は 4 項目の加算であり、引き算をしない
#   ・キャッシュの読みが 0 のまま続いたら警告する (前置きが揺れている / §6.4)
#   ・1 プロジェクトの呼び出し回数・入力トークンの上限で打ち切る (ai.cost.*)
#   ・プロンプト全文・応答全文・コメント本文を残さない
# ネットワークは使わない。
import logging
import unittest

from src.ai.provider import AiCompletion, AiRequest, AiUsage
from src.ai.usage_log import CODE_BUDGET_EXCEEDED, UsageTracker
from src.exceptions import AiProviderError


def _request(role="scene_eval", cache_prefix_id=None, images=None):
    return AiRequest(role=role, system="s", payload={"a": 1},
                     cache_prefix_id=cache_prefix_id, images=images or [])


def _completion(input_tokens=1000, output_tokens=200, cache_read=0, cache_create=0,
                model="claude-sonnet-5-5", charged=0, estimated=3):
    return AiCompletion(
        result={"ok": True}, model=model,
        usage=AiUsage(input_tokens, output_tokens, cache_create, cache_read),
        points_charged=charged, points_estimated=estimated)


class AiUsageRecordTest(unittest.TestCase):

    def test_records_the_four_usage_fields_and_the_resolved_model(self):
        tracker = UsageTracker({"ai": {}}, project_id="p1")

        tracker.record(_request(), _completion(input_tokens=1000, output_tokens=200,
                                               cache_create=500, cache_read=4000))

        summary = tracker.summary()
        self.assertEqual(1, summary["calls"])
        self.assertEqual(1000, summary["input_tokens"])
        self.assertEqual(200, summary["output_tokens"])
        self.assertEqual(500, summary["cache_creation_input_tokens"])
        self.assertEqual(4000, summary["cache_read_input_tokens"])

        # 入力側の合計は 3 項目の加算。引き算をすると負になり実費を取りこぼす。
        self.assertEqual(5500, summary["prompt_tokens"])

        role = summary["by_role"]["scene_eval"]
        self.assertEqual(["claude-sonnet-5-5"], role["models"], "解決後のモデルを残す")

    def test_splits_the_total_by_role(self):
        tracker = UsageTracker({"ai": {}})

        tracker.record(_request("scene_eval"), _completion(input_tokens=1000))
        tracker.record(_request("scene_eval"), _completion(input_tokens=2000))
        tracker.record(_request("plan"), _completion(input_tokens=5000,
                                                     model="claude-opus-5-5"))

        by_role = tracker.summary()["by_role"]
        self.assertEqual(2, by_role["scene_eval"]["calls"])
        self.assertEqual(3000, by_role["scene_eval"]["input_tokens"])
        self.assertEqual(1, by_role["plan"]["calls"])
        self.assertEqual(["claude-opus-5-5"], by_role["plan"]["models"])

    def test_counts_images_and_failures(self):
        tracker = UsageTracker({"ai": {}})

        tracker.record(_request(images=[object(), object()]), _completion())
        tracker.record_failure("review")

        summary = tracker.summary()
        self.assertEqual(2, summary["images"])
        self.assertEqual(2, summary["calls"], "失敗も回数として数える")
        self.assertEqual(1, summary["failures"])

    # 残すのは数値と識別子だけ (§17.2)。
    def test_summary_has_no_bodies(self):
        secret = "SENSITIVE-TRANSCRIPT"
        request = AiRequest(role="scene_eval", system=secret,
                            payload={"transcript": secret})
        completion = _completion()
        completion.result = {"reason": secret}

        tracker = UsageTracker({"ai": {}})
        tracker.record(request, completion)

        self.assertNotIn(secret, repr(tracker.summary()))


class AiUsageCacheWarningTest(unittest.TestCase):

    # 1 回目の 0 は前置きを作った回 (cache_creation) なので警告しない。
    def test_first_miss_is_not_warned(self):
        tracker = UsageTracker({"ai": {}})

        with self.assertNoLogs("src.ai.usage_log", level=logging.WARNING):
            tracker.record(_request(cache_prefix_id="vod-1"), _completion(cache_read=0))

    def test_repeated_miss_is_warned(self):
        tracker = UsageTracker({"ai": {}})
        tracker.record(_request(cache_prefix_id="vod-1"), _completion(cache_read=0))

        with self.assertLogs("src.ai.usage_log", level=logging.WARNING) as captured:
            tracker.record(_request(cache_prefix_id="vod-1"), _completion(cache_read=0))

        self.assertIn("vod-1", captured.output[0])

    def test_a_hit_resets_the_streak(self):
        tracker = UsageTracker({"ai": {}})
        tracker.record(_request(cache_prefix_id="vod-1"), _completion(cache_read=0))
        tracker.record(_request(cache_prefix_id="vod-1"), _completion(cache_read=4000))

        with self.assertNoLogs("src.ai.usage_log", level=logging.WARNING):
            tracker.record(_request(cache_prefix_id="vod-1"), _completion(cache_read=0))


class AiCostGuardTest(unittest.TestCase):

    def test_stops_at_the_call_limit(self):
        tracker = UsageTracker({"ai": {"cost": {"max_calls_per_project": 2}}})

        tracker.guard("scene_eval")
        tracker.record(_request(), _completion())
        tracker.guard("scene_eval")
        tracker.record(_request(), _completion())

        with self.assertRaises(AiProviderError) as captured:
            tracker.guard("scene_eval")

        self.assertEqual(CODE_BUDGET_EXCEEDED, captured.exception.code)

    def test_stops_at_the_input_token_limit(self):
        tracker = UsageTracker({"ai": {"cost": {"max_input_tokens_per_project": 5000}}})

        tracker.guard()
        tracker.record(_request(), _completion(input_tokens=4000, cache_read=2000))

        with self.assertRaises(AiProviderError):
            tracker.guard()

    # 0 は無制限 (打ち切らない)。
    def test_zero_means_no_limit(self):
        tracker = UsageTracker({"ai": {"cost": {"max_calls_per_project": 0,
                                                "max_input_tokens_per_project": 0}}})

        for _ in range(5):
            tracker.guard()
            tracker.record(_request(), _completion(input_tokens=100000))

        tracker.guard()


class AiUsageSummaryLogTest(unittest.TestCase):

    def test_summary_is_logged_in_one_line(self):
        tracker = UsageTracker({"ai": {}}, project_id="p1")
        tracker.record(_request(), _completion())

        with self.assertLogs("src.ai.usage_log", level=logging.INFO) as captured:
            tracker.log_summary()

        # 合計 1 行 + 工程 1 つぶんの内訳 1 行
        self.assertEqual(2, len(captured.output))
        self.assertIn("calls=1", captured.output[0])

    # 単価とモデル割り当ては工程ごとの実測が無いと決められない (§22 T1 / 回答 Q2)。
    # ログから表へ写せる形 (1 行 1 工程 / 4 項目 + 解決後のモデル) であることを確かめる。
    def test_breakdown_is_logged_per_role(self):
        tracker = UsageTracker({"ai": {}}, project_id="p1")
        tracker.record(_request(role="scene_eval"),
                       _completion(input_tokens=1000, output_tokens=200,
                                   cache_read=300, cache_create=400,
                                   model="claude-sonnet-5-5"))
        tracker.record(_request(role="plan"),
                       _completion(input_tokens=2000, output_tokens=500,
                                   model="claude-opus-5-5"))

        with self.assertLogs("src.ai.usage_log", level=logging.INFO) as captured:
            tracker.log_summary()

        rows = [line for line in captured.output if "AI 内訳" in line]
        self.assertEqual(2, len(rows))
        plan = [line for line in rows if "role=plan" in line][0]
        scene = [line for line in rows if "role=scene_eval" in line][0]
        self.assertIn("input=2000", plan)
        self.assertIn("output=500", plan)
        self.assertIn("claude-opus-5-5", plan)
        self.assertIn("cache_read=300", scene)
        self.assertIn("cache_create=400", scene)
        self.assertIn("claude-sonnet-5-5", scene)

    # 1 回も呼ばなかった場合 (縮退して AI を使わなかった) も例外にしない。
    def test_summary_without_calls(self):
        tracker = UsageTracker({"ai": {}})

        with self.assertLogs("src.ai.usage_log", level=logging.INFO) as captured:
            tracker.log_summary()

        self.assertEqual(1, len(captured.output))

    # 計測で本処理を止めない (テレメトリーが失敗しても例外にしない / §17.4)。
    def test_broken_telemetry_does_not_raise(self):
        class _Broken:

            def event(self, name, duration_ms=None, project_id=None):
                raise RuntimeError("boom")

        tracker = UsageTracker({"ai": {}}, project_id="p1", telemetry=_Broken())
        tracker.record(_request(), _completion())

        tracker.log_summary()


if __name__ == "__main__":
    unittest.main()
