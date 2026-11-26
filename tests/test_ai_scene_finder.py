# 面白シーン抽出の単体テスト (docs/AiEditor/resolve1.md §7 / §20)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・8 軸の評価が候補へ重なり、**区間は候補の範囲内へ収まる**
#   ・順位は設定の重みで決まる (AI に順位を作らせない / §7.3)
#   ・既存の採点も混ぜる (base_score_weight) = AI の誤判断で面白い場面を失わない
#   ・AI が失敗したバッチは既存の採点へ落ちて完走する (§11.2 の縮退)
#   ・provider が無くても順位が付く
#   ・重なる区間は統合され、点は高い側が残る
#   ・評価は <project>.ai.json へ残り、次回は使い回す (§7.2)
# プロバイダーへは繋がない (固定 JSON の MockProvider)。
import os
import tempfile
import unittest

from src.ai import context_builder as cb
from src.ai import plan_schema as ps
from src.ai import scene_finder as sf
from src.ai.provider import AiCompletion, AiProvider, MockProvider
from src.exceptions import AiOfflineError, AiPlanError

_AXES = ps.SCORE_KEYS


# AI のシーン評価は**既定で切られている** (採点スイープと役割が重なるため /
# docs/error/20261125c/resolve.md §2.6)。このファイルは AI 評価そのものを
# 記録するので、明示的に有効にして呼ぶ。切った側は DisabledTest が見る。
def _on(**scene_eval):
    scene_eval.setdefault("enabled", True)

    return {"ai": {"scene_eval": scene_eval}}


def _bundle(count=3, duration=3600.0):
    clips = [{"index": i + 1, "start": 100.0 + i * 400, "end": 130.0 + i * 400,
              "score": 5.0 + i, "emotion": 5.0, "comment": 5.0} for i in range(count)]

    return cb.build({"duration": duration, "curve": [], "clips": clips}, {},
                    source={"video_id": "123"})


def _scene_result(indexes, funny=70, start=None, end=None):
    scenes = []
    for index in indexes:
        scenes.append({"candidate_index": index,
                       "start": start if start is not None else None,
                       "end": end if end is not None else None,
                       "scores": {key: 50 for key in _AXES} | {"funny": funny},
                       "summary": "要約{0}".format(index), "reason": "理由"})

    return {"scenes": scenes}


# 役割ごとに違う応答を返すプロバイダー。
class _Provider(AiProvider):

    name = "fake"

    def __init__(self, results, error=None, fail_after=None):
        self._results = results
        self._error = error
        self._fail_after = fail_after
        self.requests = []

    def complete_json(self, request):
        self.requests.append(request)
        if self._error is not None and (self._fail_after is None
                                        or len(self.requests) > self._fail_after):
            raise self._error

        return AiCompletion(result=self._results, model="claude-sonnet-5-5")

    def count_tokens(self, request):
        return None

    def server_config(self):
        return None


class EvaluateTest(unittest.TestCase):

    def test_scores_are_attached_to_the_candidates(self):
        bundle = _bundle(2)
        provider = _Provider(_scene_result([1, 2]))

        scenes, report = sf.evaluate(bundle, provider, _on())

        self.assertEqual(2, len(scenes))
        self.assertTrue(all(scene.evaluated for scene in scenes))
        self.assertEqual(70.0, scenes[0].scores["funny"])
        self.assertEqual("要約1", {s.index: s.summary for s in scenes}[1])

    # 候補の範囲内へ詰めるのは許す。外へ伸ばすのは許さない (§8.1)。
    def test_range_is_kept_inside_the_candidate(self):
        bundle = _bundle(1)
        provider = _Provider(_scene_result([1], start=110.0, end=125.0))

        scenes, _ = sf.evaluate(bundle, provider, _on())

        self.assertEqual(110.0, scenes[0].start)
        self.assertEqual(125.0, scenes[0].end)

    def test_range_outside_is_clipped(self):
        bundle = _bundle(1)
        provider = _Provider(_scene_result([1], start=10.0, end=3000.0))

        scenes, report = sf.evaluate(bundle, provider, _on())

        self.assertEqual(100.0, scenes[0].start)
        self.assertEqual(130.0, scenes[0].end)
        self.assertEqual(["E2"], [n["check"] for n in report.notes])

    # 1 回の呼び出しで batch_size 件ずつ (§7.2)。
    def test_candidates_are_evaluated_in_batches(self):
        bundle = _bundle(5)
        provider = _Provider(_scene_result([1, 2]))

        sf.evaluate(bundle, provider, _on(batch_size=2))

        self.assertEqual(3, len(provider.requests))
        self.assertEqual("scene_eval", provider.requests[0].role)
        self.assertEqual("scene_eval-v1", provider.requests[0].cache_prefix_id)

    # 前置き (system) は呼び出しごとに変えない = キャッシュへ乗る (§6.4)。
    def test_the_prompt_prefix_is_stable(self):
        bundle = _bundle(4)
        provider = _Provider(_scene_result([1]))

        sf.evaluate(bundle, provider, _on(batch_size=1))
        prefixes = {request.system for request in provider.requests}

        self.assertEqual(1, len(prefixes))

    def test_schema_is_sent(self):
        bundle = _bundle(2)
        provider = _Provider(_scene_result([1, 2]))

        sf.evaluate(bundle, provider, _on())
        schema = provider.requests[0].schema

        self.assertEqual(set(_AXES),
                         set(schema["properties"]["scenes"]["items"]["properties"]
                             ["scores"]["properties"]))


class DegradeTest(unittest.TestCase):

    # AI が落ちても既存の採点で順位が付く (経路 B 相当 / §11.2)。
    def test_failed_batch_falls_back_to_the_base_score(self):
        bundle = _bundle(2)
        provider = _Provider({}, error=AiOfflineError("接続できません"))

        scenes, report = sf.evaluate(bundle, provider, _on())

        self.assertEqual(2, len(scenes))
        self.assertFalse(any(scene.evaluated for scene in scenes))
        self.assertEqual(["E2"], sorted({n["check"] for n in report.notes}))
        # 既存の採点の高い側が上に来る
        self.assertEqual(2, scenes[0].index)

    def test_only_the_failing_batch_is_lost(self):
        bundle = _bundle(2)
        provider = _Provider(_scene_result([1]),
                             error=AiPlanError("JSON ではありません"), fail_after=1)

        scenes, _ = sf.evaluate(bundle, provider, _on(batch_size=1))
        by_index = {scene.index: scene for scene in scenes}

        self.assertTrue(by_index[1].evaluated)
        self.assertFalse(by_index[2].evaluated)

    def test_no_provider_still_ranks(self):
        bundle = _bundle(3)

        scenes, report = sf.evaluate(bundle, None, _on())

        self.assertEqual([3, 2, 1], [scene.index for scene in scenes])
        self.assertEqual([], report.notes)

    def test_no_candidates_returns_nothing(self):
        bundle = cb.build({"duration": 10.0, "curve": [], "clips": []}, {})

        scenes, _ = sf.evaluate(bundle, MockProvider(), _on())

        self.assertEqual([], scenes)


class RankTest(unittest.TestCase):

    def _scene(self, index, start, end, funny=0.0, base=0.0, **scores):
        values = {key: 0.0 for key in _AXES}
        values["funny"] = funny
        values.update(scores)

        return sf.RankedScene(index=index, start=start, end=end, scores=values,
                              base_score=base, evaluated=True)

    # 用途ごとに重みが違う (§7.3 の表)。
    def test_weights_differ_by_use(self):
        scenes = [self._scene(1, 0.0, 10.0, funny=50.0, shorts_fit=100.0),
                  self._scene(2, 100.0, 110.0, funny=50.0, long_fit=100.0)]

        for_shorts = sf.rank(list(scenes), {}, use=sf.USE_SHORTS)
        for_long = sf.rank(list(scenes), {}, use=sf.USE_LONG)

        self.assertEqual(1, for_shorts[0].index)
        self.assertEqual(2, for_long[0].index)

    # 文脈依存度は引き算 (高いほど順位が下がる)。
    def test_context_dependency_lowers_the_rank(self):
        scenes = [self._scene(1, 0.0, 10.0, funny=60.0),
                  self._scene(2, 100.0, 110.0, funny=60.0, context_dependency=100.0)]

        ranked = sf.rank(scenes, {}, use=sf.USE_LONG)

        self.assertEqual(1, ranked[0].index)

    # 既存の採点も混ぜる = AI が低く付けても音量・コメントが高い区間は残る。
    def test_base_score_is_mixed_in(self):
        scenes = [self._scene(1, 0.0, 10.0, funny=10.0, base=100.0),
                  self._scene(2, 100.0, 110.0, funny=12.0, base=0.0)]

        ranked = sf.rank(scenes, {"ai": {"scene_eval": {"base_score_weight": 1.0}}},
                         use=sf.USE_LONG)

        self.assertEqual(1, ranked[0].index)

    # 重なる区間は統合し、点は高い側を残す。
    def test_overlaps_are_merged(self):
        scenes = [self._scene(1, 100.0, 130.0, funny=40.0),
                  self._scene(2, 125.0, 150.0, funny=80.0)]

        ranked = sf.rank(scenes, {}, use=sf.USE_LONG)

        self.assertEqual(1, len(ranked))
        self.assertEqual(100.0, ranked[0].start)
        self.assertEqual(150.0, ranked[0].end)
        self.assertEqual(80.0, ranked[0].scores["funny"])

    def test_separate_scenes_are_kept(self):
        scenes = [self._scene(1, 100.0, 130.0), self._scene(2, 500.0, 530.0)]

        self.assertEqual(2, len(sf.rank(scenes, {}, use=sf.USE_LONG)))

    # skip と判定されたシーンは構成へ渡さない。
    def test_top_scenes_drops_skipped(self):
        keep = self._scene(1, 0.0, 10.0, funny=90.0)
        skipped = self._scene(2, 100.0, 110.0, funny=10.0)
        skipped.recommended_use = ["skip"]

        chosen = sf.top_scenes([keep, skipped])

        self.assertEqual([1], [scene.index for scene in chosen])

    def test_top_scenes_limit(self):
        scenes = [self._scene(i, i * 100.0, i * 100.0 + 10.0) for i in range(1, 6)]

        self.assertEqual(2, len(sf.top_scenes(scenes, limit=2)))


class EvalCacheTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.project = os.path.join(self._dir.name, "video.timeline.json")

    def tearDown(self):
        self._dir.cleanup()

    # 評価は <project>.ai.json へ残す (再実行で呼び出しを減らす / §7.2)。
    def test_evaluation_is_saved_and_reused(self):
        bundle = _bundle(2)
        provider = _Provider(_scene_result([1, 2]))

        sf.evaluate(bundle, provider, _on(), project_path=self.project)

        self.assertTrue(os.path.exists(ps.eval_path_for(self.project)))

        again = _Provider(_scene_result([1, 2]))
        scenes, _ = sf.evaluate(_bundle(2), again, _on(), project_path=self.project)

        self.assertEqual([], again.requests, "保存済みの評価を使い、呼び直さない")
        self.assertTrue(all(scene.evaluated for scene in scenes))

    def test_reuse_can_be_turned_off(self):
        bundle = _bundle(1)
        sf.evaluate(bundle, _Provider(_scene_result([1])), _on(),
                    project_path=self.project)

        again = _Provider(_scene_result([1]))
        sf.evaluate(_bundle(1), again, _on(), project_path=self.project, reuse=False)

        self.assertEqual(1, len(again.requests))

    # 候補の作り方が変わった (時刻が大きく動いた) ら古い評価は使わない。
    def test_moved_candidates_are_not_reused(self):
        sf.evaluate(_bundle(1), _Provider(_scene_result([1])), _on(),
                    project_path=self.project)

        moved = cb.build({"duration": 3600.0, "curve": [],
                          "clips": [{"index": 1, "start": 2000.0, "end": 2030.0,
                                     "score": 5.0}]}, {})
        again = _Provider(_scene_result([1]))
        sf.evaluate(moved, again, _on(), project_path=self.project)

        self.assertEqual(1, len(again.requests))

    def test_unknown_version_is_ignored(self):
        path = ps.eval_path_for(self.project)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"eval_version": 99, "scenes": []}')

        self.assertEqual({}, sf.load_eval(self.project))

    def test_broken_file_is_ignored(self):
        path = ps.eval_path_for(self.project)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{")

        self.assertEqual({}, sf.load_eval(self.project))


class DisabledTest(unittest.TestCase):
    """ai.scene_eval.enabled = false (既定): AI を呼ばず既存の採点だけで順位を付ける
    (docs/error/20261125c/resolve.md §2.6)"""

    def test_the_default_is_off(self):
        from src.ai import config as ai_config
        from src.settings import settings_window

        self.assertFalse(ai_config.DEFAULT_AI["scene_eval"]["enabled"])
        self.assertFalse(
            settings_window.DEFAULT_SETTINGS["ai"]["scene_eval"]["enabled"])

    # 既定の設定では 1 回も呼ばない
    def test_no_call_is_made(self):
        provider = _Provider(_scene_result([1, 2, 3]))

        scenes, report = sf.evaluate(_bundle(3), provider, {})

        self.assertEqual([], provider.requests, "AI を呼ばない")
        self.assertEqual(3, len(scenes), "候補は落とさない")
        self.assertFalse(any(scene.evaluated for scene in scenes))
        self.assertEqual([], report.notes)

    # 既存の採点だけで順位が付く (全部が中立の軸 = base_score で並ぶ)
    def test_the_base_score_decides_the_order(self):
        bundle = _bundle(3)
        scenes = sf.rank(sf.evaluate(bundle, _Provider(_scene_result([1, 2, 3])), {})[0],
                         {"ai": {"scene_eval": {"base_score_weight": 1.0}}})
        totals = [scene.rank_score for scene in scenes]

        self.assertEqual(sorted(totals, reverse=True), totals)

    # 保存済みの評価も使わない (切ったのに前回の AI の点が効くと読めなくなる)
    def test_the_saved_evaluation_is_ignored(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            project = os.path.join(folder, "p.timeline.json")
            # まず有効にして評価を保存する
            sf.evaluate(_bundle(2), _Provider(_scene_result([1, 2])), _on(),
                        project_path=project)
            # 切った状態で読み直す
            scenes, _ = sf.evaluate(_bundle(2), _Provider(_scene_result([1, 2])), {},
                                    project_path=project)

        self.assertFalse(any(scene.evaluated for scene in scenes))


if __name__ == "__main__":
    unittest.main()
