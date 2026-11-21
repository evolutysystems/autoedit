# 企画・構成と編集指示の単体テスト (docs/AiEditor/resolve1.md §8 / §9 / §20)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・長尺と短尺は**別々に呼ぶ** (同じ VideoPlan を使い回さない / §8.1)
#   ・section の区間はシーンの範囲へ収める (AI が時刻を発明しても素材を外さない)
#   ・EditPlan の project は**こちらで埋める** (モデルに素材の情報を作らせない / §9.1)
#   ・構成の呼び出しが失敗したら手元で組んで完走する (§11.2 の縮退)
#   ・編集指示の呼び出しが失敗したら「シーンの並びだけ」の EditPlan へ落ちる
#     (テロップは既存の自動生成がそのまま残る / §19 の 6)
#   ・尺と本数の上限は設定が持つ (AI にハードコードさせない / §8.1)
# プロバイダーへは繋がない。
import unittest

from src.ai import context_builder as cb
from src.ai import plan_schema as ps
from src.ai import planner
from src.ai import scene_finder as sf
from src.ai.provider import AiCompletion, AiProvider
from src.exceptions import AiOfflineError

_AXES = ps.SCORE_KEYS


def _bundle(duration=3600.0):
    bundle = cb.build({"duration": duration, "curve": [], "clips": []}, {},
                      source={"video_id": "123", "title": "配信"})
    bundle.transcript = [{"start": 105.0, "end": 108.0, "text": "やばい"},
                         {"start": 505.0, "end": 509.0, "text": "まじか"}]

    return bundle


def _scene(index, start, end, funny=70.0, summary=""):
    return sf.RankedScene(index=index, start=start, end=end,
                          scores={key: 50.0 for key in _AXES} | {"funny": funny},
                          summary=summary or "要約{0}".format(index),
                          reason="理由", base_score=5.0, evaluated=True)


def _scenes():
    return [_scene(1, 100.0, 130.0), _scene(2, 500.0, 540.0),
            _scene(3, 900.0, 930.0)]


_VIDEO_PLAN = {
    "target": "youtube_long", "title": "題名", "concept": "狙い",
    "duration_sec": 70.0,
    "sections": [{"role": "opening", "scene_ref": 1, "start": 105.0, "end": 125.0,
                  "reason": "山を先に見せる"},
                 {"role": "body", "scene_ref": 2, "start": 500.0, "end": 540.0,
                  "reason": "積む"}],
    "direction": {"subtitle": "full", "emphasis_scenes": [1],
                  "sound_effects": "light", "bgm": "none", "vertical_crop": False},
}

_EDIT_PLAN = {
    "plan_version": 1,
    "project": {"target": "youtube_long",
                "source": {"kind": "local", "video_id": "でっちあげ", "path": "C:/x.mp4"},
                "canvas": {"orientation": "portrait"}, "duration_sec": 1.0},
    "scenes": [{"id": "a", "source_start": 105.0, "source_end": 125.0,
                "role": "opening", "order": 1, "reason": "x"},
               {"id": "b", "source_start": 500.0, "source_end": 540.0,
                "role": "body", "order": 2, "reason": "y"}],
    "subtitles": [{"scene_id": "a", "source_start": 105.0, "source_end": 108.0,
                   "text": "やばい", "track_role": "own", "animation": "tsukkomi"}],
    "sounds": [], "editing": {"subtitle": True}, "notes": "メモ",
}


# 役ごとに違う応答を返す。cache_prefix_id で見分ける。
class _Provider(AiProvider):

    name = "fake"

    def __init__(self, video_plan=None, edit_plan=None, error_on=None):
        self._video_plan = video_plan if video_plan is not None else _VIDEO_PLAN
        self._edit_plan = edit_plan if edit_plan is not None else _EDIT_PLAN
        self._error_on = error_on or ()
        self.calls = []

    def complete_json(self, request):
        self.calls.append(request)
        kind = "plan" if request.cache_prefix_id == "plan-v1" else "edit"
        if kind in self._error_on:
            raise AiOfflineError("接続できません")
        result = self._video_plan if kind == "plan" else self._edit_plan

        return AiCompletion(result=result, model="claude-opus-5-5")

    def count_tokens(self, request):
        return None

    def server_config(self):
        return None


class BuildTest(unittest.TestCase):

    def test_two_calls_one_per_stage(self):
        provider = _Provider()

        plan, report = planner.build(_bundle(), _scenes(), provider, {},
                                     "youtube_long")

        self.assertEqual(["plan-v1", "edit_plan-v1"],
                         [call.cache_prefix_id for call in provider.calls])
        self.assertEqual("plan", provider.calls[0].role)
        self.assertEqual(2, len(plan["scenes"]))
        self.assertEqual([], report.notes)

    # project は AI の値を使わない (素材の情報を作らせない / §9.1)。
    def test_project_is_filled_locally(self):
        plan, _ = planner.build(_bundle(), _scenes(), _Provider(), {},
                                "youtube_long")

        self.assertEqual("youtube_long", plan["project"]["target"])
        self.assertEqual("123", plan["project"]["source"]["video_id"])
        self.assertEqual("vod", plan["project"]["source"]["kind"])
        self.assertEqual("", plan["project"]["source"]["path"])
        self.assertEqual("landscape", plan["project"]["canvas"]["orientation"])
        self.assertEqual(3600.0, plan["project"]["duration_sec"])
        self.assertEqual("claude-opus-5-5", plan["generated"]["model"])

    def test_subtitles_and_notes_are_kept(self):
        plan, _ = planner.build(_bundle(), _scenes(), _Provider(), {},
                                "youtube_long")

        self.assertEqual("やばい", plan["subtitles"][0]["text"])
        self.assertEqual("メモ", plan["notes"])

    # 長尺と短尺は別々に呼ぶ (同じ構成を使い回さない)。
    def test_each_target_is_asked_separately(self):
        provider = _Provider()
        bundle = _bundle()

        planner.build(bundle, _scenes(), provider, {}, "youtube_long")
        planner.build(bundle, _scenes(), provider, {}, "youtube_shorts")

        targets = [call.payload.get("target") for call in provider.calls]
        self.assertEqual(["youtube_long", "youtube_long",
                          "youtube_shorts", "youtube_shorts"], targets)

    def test_no_usable_scene_returns_none(self):
        plan, _ = planner.build(_bundle(), [], _Provider(), {}, "youtube_long")

        self.assertIsNone(plan)

    # 評価したシーンの情報と、その区間の文字起こしを渡す。
    def test_payload_carries_the_scenes_and_transcript(self):
        provider = _Provider()

        planner.build(_bundle(), _scenes(), provider, {}, "youtube_long")
        payload = provider.calls[0].payload

        self.assertEqual([1, 2, 3], [row["scene_ref"] for row in payload["scenes"]])
        self.assertEqual([{"start": 105.0, "end": 108.0, "text": "やばい"}],
                         payload["scenes"][0]["transcript"])
        self.assertEqual({"min_sec": 180.0, "max_sec": 900.0, "max_scenes": 12},
                         payload["limits"])


class SectionFitTest(unittest.TestCase):

    def _with_sections(self, sections):
        video_plan = dict(_VIDEO_PLAN)
        video_plan["sections"] = sections

        return _Provider(video_plan=video_plan)

    # AI が範囲外の時刻を書いてもシーンの中へ収める。
    def test_section_is_clipped_to_the_scene(self):
        provider = self._with_sections([
            {"role": "opening", "scene_ref": 1, "start": 50.0, "end": 200.0,
             "reason": "r"}])

        planner.build(_bundle(), _scenes(), provider, {}, "youtube_long")
        payload = provider.calls[1].payload

        self.assertEqual(100.0, payload["sections"][0]["start"])
        self.assertEqual(130.0, payload["sections"][0]["end"])

    # 存在しないシーンを指したら、時刻の近いシーンへ寄せる。
    def test_unknown_scene_ref_falls_back_to_the_nearest(self):
        provider = self._with_sections([
            {"role": "body", "scene_ref": 99, "start": 505.0, "end": 530.0,
             "reason": "r"}])

        _, report = planner.build(_bundle(), _scenes(), provider, {}, "youtube_long")
        payload = provider.calls[1].payload

        self.assertEqual(2, payload["sections"][0]["scene_ref"])
        self.assertIn("P4", [n["check"] for n in report.notes])

    # section が全部落ちたら手元で組み直す (空の構成を下流へ流さない)。
    def test_empty_sections_fall_back(self):
        provider = self._with_sections([])

        plan, report = planner.build(_bundle(), _scenes(), provider, {},
                                     "youtube_long")

        self.assertTrue(plan["scenes"])
        self.assertTrue(provider.calls[1].payload["sections"])


class FallbackTest(unittest.TestCase):

    # 構成の呼び出しが落ちても手元で組んで完走する。
    def test_video_plan_failure_degrades(self):
        provider = _Provider(error_on=("plan",))

        plan, report = planner.build(_bundle(), _scenes(), provider, {},
                                     "youtube_long")

        self.assertTrue(plan["scenes"])
        self.assertIn("P4", [n["check"] for n in report.notes])
        self.assertIn("local", [n["detail"] for n in report.notes])

    # 編集指示が落ちたら「シーンの並びだけ」へ落ちる。テロップは書かない。
    def test_edit_plan_failure_keeps_the_scenes_only(self):
        provider = _Provider(error_on=("edit",))

        plan, _ = planner.build(_bundle(), _scenes(), provider, {}, "youtube_long")

        self.assertEqual(2, len(plan["scenes"]))
        self.assertEqual([], plan["subtitles"],
                         "本文は文字起こしが正。AI 無しで足さない")
        self.assertEqual([], plan["sounds"])

    def test_without_a_provider_everything_is_local(self):
        plan, report = planner.build(_bundle(), _scenes(), None, {}, "youtube_long")

        self.assertTrue(plan["scenes"])
        self.assertEqual(2, len([n for n in report.notes if n["check"] == "P4"]))

    # 空の応答 (scenes が無い) も縮退へ落とす。
    def test_empty_edit_plan_degrades(self):
        provider = _Provider(edit_plan={"plan_version": 1, "scenes": []})

        plan, report = planner.build(_bundle(), _scenes(), provider, {},
                                     "youtube_long")

        self.assertTrue(plan["scenes"])
        self.assertIn("local", [n["detail"] for n in report.notes])

    # 尺と本数の上限は設定が持つ。
    def test_fallback_respects_the_limits(self):
        settings = {"ai": {"plan": {"youtube_shorts": {"min_sec": 15, "max_sec": 60,
                                                       "max_scenes": 2}}}}
        scenes = [_scene(1, 100.0, 130.0), _scene(2, 500.0, 530.0),
                  _scene(3, 900.0, 930.0)]

        video_plan = planner.fallback_video_plan(scenes, settings, "youtube_shorts")

        self.assertEqual(2, len(video_plan["sections"]))
        self.assertEqual(60.0, video_plan["duration_sec"])

    def test_fallback_keeps_the_time_order(self):
        scenes = [_scene(2, 500.0, 530.0, funny=90.0), _scene(1, 100.0, 130.0)]

        video_plan = planner.fallback_video_plan(scenes, {}, "youtube_long")

        self.assertEqual([100.0, 500.0],
                         [section["start"] for section in video_plan["sections"]])
        self.assertEqual(["opening", "ending"],
                         [section["role"] for section in video_plan["sections"]])

    def test_fallback_uses_at_least_one_scene(self):
        settings = {"ai": {"plan": {"youtube_shorts": {"min_sec": 15, "max_sec": 5,
                                                       "max_scenes": 5}}}}

        video_plan = planner.fallback_video_plan([_scene(1, 100.0, 130.0)], settings,
                                                 "youtube_shorts")

        self.assertEqual(1, len(video_plan["sections"]))


class UseOfTargetTest(unittest.TestCase):

    def test_targets_map_to_the_weight_sets(self):
        self.assertEqual(sf.USE_LONG, planner.use_of("youtube_long"))
        self.assertEqual(sf.USE_SHORTS, planner.use_of("youtube_shorts"))
        self.assertEqual(sf.USE_SHORTS, planner.use_of("tiktok"))
        self.assertEqual(sf.USE_LONG, planner.use_of("unknown"))


if __name__ == "__main__":
    unittest.main()
