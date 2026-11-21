# 品質レビューと自動修正の単体テスト (docs/AiEditor/resolve1.md §12 / §20)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・役ごとに 1 回ずつ呼び、**前置きは役が変わっても同じ** (キャッシュへ乗る / §12.1)
#   ・viewer には評価の数値を渡さない (初見で読ませる)
#   ・qa には機械の測定結果だけを渡す (§12.2)
#   ・採る指摘は重大度の下限と件数で絞る (§12.4)
#   ・修正は決定的な操作だけ。当てられない指摘は当てずに記録へ残す
#   ・1 役が落ちても他の役は続く (§11.2 の縮退)
#   ・フレームは枚数・長辺を守り、**送信用に読んだら消す** (§12.5)
# プロバイダーへは繋がない。ffmpeg も呼ばない。
import os
import tempfile
import unittest

from src.ai import plan_schema as ps
from src.ai import reviewer, vision
from src.ai import scene_finder as sf
from src.ai.provider import AiCompletion, AiProvider
from src.exceptions import AiOfflineError

_ENABLED = {"ai": {"review": {"enabled": True, "max_rounds": 1,
                              "max_issues_per_round": 5, "min_severity": "major",
                              "roles": {"director": True, "critic": True,
                                        "viewer": True, "platform": True,
                                        "qa": False}},
                   "vision": {"enabled": False},
                   "plan": {"youtube_shorts": {"min_sec": 15, "max_sec": 60,
                                               "max_scenes": 5}}}}


def _plan(scenes=None, subtitles=None, sounds=None):
    plan = ps.new_plan("youtube_shorts", {"kind": "vod", "video_id": "1"},
                       duration_sec=3600.0)
    plan["scenes"] = scenes if scenes is not None else [
        {"id": "sc1", "source_start": 100.0, "source_end": 120.0, "role": "hook",
         "order": 1, "reason": ""},
        {"id": "sc2", "source_start": 500.0, "source_end": 530.0, "role": "body",
         "order": 2, "reason": ""}]
    plan["subtitles"] = subtitles if subtitles is not None else [
        {"scene_id": "sc1", "source_start": 101.0, "source_end": 103.0,
         "text": "やばい", "track_role": "own", "animation": "", "fade_in": False,
         "fade_out": False, "color": "", "align": "", "reason": ""}]
    plan["sounds"] = sounds or []

    return plan


def _issue(op="trim", severity="major", scene_id="sc1", priority=1, **fix):
    merged = {"op": op, "target": scene_id, "source_start": None, "source_end": None,
              "text": "", "animation": "", "preset": "", "order": []}
    merged.update(fix)

    return {"id": "i1", "role": "critic",
            "at": {"start": 100.0, "end": 120.0, "coord": "source"},
            "scene_id": scene_id, "kind": "pacing", "severity": severity,
            "detail": "d", "reason": "r", "fix": merged, "expected": "e",
            "priority": priority}


class _Provider(AiProvider):

    name = "fake"

    def __init__(self, issues=None, fail_roles=()):
        self._issues = issues if issues is not None else []
        self._fail_roles = set(fail_roles)
        self.requests = []

    def complete_json(self, request):
        self.requests.append(request)
        role = request.payload.get("role")
        if role in self._fail_roles:
            raise AiOfflineError("接続できません")

        return AiCompletion(result={"issues": self._issues}, model="claude-sonnet-5-5")

    def count_tokens(self, request):
        return None

    def server_config(self):
        return None


class RolesTest(unittest.TestCase):

    def test_one_call_per_enabled_role(self):
        provider = _Provider()

        reviewer.review(_plan(), None, [], provider, _ENABLED, "youtube_shorts")

        roles = [request.payload["role"] for request in provider.requests]
        self.assertEqual(["director", "critic", "viewer", "platform"], roles)

    # 前置きは役が変わっても同じ = キャッシュへ乗る (§6.4)。
    def test_prefix_is_shared(self):
        provider = _Provider()

        reviewer.review(_plan(), None, [], provider, _ENABLED, "youtube_shorts")

        self.assertEqual(1, len({request.system for request in provider.requests}))
        self.assertEqual({"review-v1"},
                         {request.cache_prefix_id for request in provider.requests})

    # critic だけ強いモデルを希望する (§6.3)。
    def test_critic_asks_for_a_stronger_model(self):
        provider = _Provider()

        reviewer.review(_plan(), None, [], provider, _ENABLED, "youtube_shorts")
        keys = {request.payload["role"]: request.model_key
                for request in provider.requests}

        self.assertEqual("review_critic", keys["critic"])
        self.assertEqual("review", keys["director"])
        self.assertEqual("review", provider.requests[0].role)

    # viewer には予備知識を渡さない (§12.1)。
    def test_viewer_gets_no_scores(self):
        scene = sf.RankedScene(1, 100.0, 120.0,
                               scores={key: 70.0 for key in ps.SCORE_KEYS})
        provider = _Provider()

        reviewer.review(_plan(), None, [scene], provider, _ENABLED, "youtube_shorts")
        payloads = {request.payload["role"]: request.payload
                    for request in provider.requests}

        self.assertNotIn("scene_scores", payloads["viewer"])
        self.assertIn("scene_scores", payloads["critic"])

    # qa には機械の測定結果だけを渡す (§12.2)。
    def test_qa_gets_the_measurements(self):
        # roles は ai_config が入れ子の既定で埋めるため、全役を明示する。
        settings = {"ai": {"review": {"enabled": True, "max_rounds": 1,
                                      "roles": {"director": False, "critic": False,
                                                "viewer": False, "platform": False,
                                                "qa": True}},
                           "vision": {"enabled": False}}}
        provider = _Provider()

        reviewer.review(_plan(), None, [], provider, settings, "youtube_shorts")

        self.assertEqual(["qa"], [r.payload["role"] for r in provider.requests])
        self.assertIn("measurements", provider.requests[0].payload)

    # 1 役が落ちても他の役は続ける。
    def test_one_failing_role_does_not_stop_the_rest(self):
        provider = _Provider(fail_roles=("director",))

        result = reviewer.review(_plan(), None, [], provider, _ENABLED,
                                 "youtube_shorts")

        self.assertEqual(4, len(provider.requests))
        self.assertIn("R2", [note["check"] for note in result.report.notes])

    def test_disabled_review_only_measures(self):
        provider = _Provider()

        result = reviewer.review(_plan(), None, [], provider, {}, "youtube_shorts")

        self.assertEqual([], provider.requests)
        self.assertTrue(result.checks or result.checks == [])

    def test_no_provider_only_measures(self):
        result = reviewer.review(_plan(), None, [], None, _ENABLED, "youtube_shorts")

        self.assertEqual([], result.issues)


class QaCheckTest(unittest.TestCase):

    def test_duration_over_the_limit(self):
        plan = _plan(scenes=[{"id": "sc1", "source_start": 0.0, "source_end": 90.0,
                              "role": "hook", "order": 1, "reason": ""}],
                     subtitles=[])

        checks = reviewer.qa_checks(plan, _ENABLED, "youtube_shorts")

        self.assertEqual(["duration"], [check["kind"] for check in checks])
        self.assertEqual("major", checks[0]["severity"])

    def test_overlapping_subtitles_are_measured(self):
        plan = _plan(subtitles=[
            {"scene_id": "sc1", "source_start": 101.0, "source_end": 105.0,
             "text": "前", "track_role": "own", "animation": "", "fade_in": False,
             "fade_out": False, "color": "", "align": "", "reason": ""},
            {"scene_id": "sc1", "source_start": 104.0, "source_end": 108.0,
             "text": "後", "track_role": "own", "animation": "", "fade_in": False,
             "fade_out": False, "color": "", "align": "", "reason": ""}])

        checks = reviewer.qa_checks(plan, _ENABLED, "youtube_shorts")

        self.assertIn("重なっている", "".join(check["detail"] for check in checks))

    def test_short_scene_is_measured(self):
        plan = _plan(scenes=[{"id": "sc1", "source_start": 100.0,
                              "source_end": 100.5, "role": "hook", "order": 1,
                              "reason": ""}], subtitles=[])

        checks = reviewer.qa_checks(plan, _ENABLED, "youtube_shorts")

        self.assertIn("scene", [check["kind"] for check in checks])


class SelectTest(unittest.TestCase):

    def test_minor_is_dropped_by_default(self):
        issues = [_issue(severity="minor"), _issue(severity="major")]

        chosen = reviewer.select_issues(issues, _ENABLED)

        self.assertEqual(["major"], [issue["severity"] for issue in chosen])

    def test_none_op_is_dropped(self):
        self.assertEqual([], reviewer.select_issues([_issue(op="none")], _ENABLED))

    def test_count_is_capped(self):
        settings = {"ai": {"review": {"max_issues_per_round": 2,
                                      "min_severity": "minor"}}}
        issues = [_issue(severity="major", priority=i) for i in range(1, 6)]

        self.assertEqual(2, len(reviewer.select_issues(issues, settings)))

    def test_priority_order(self):
        issues = [_issue(priority=3), _issue(priority=1)]

        chosen = reviewer.select_issues(issues, _ENABLED)

        self.assertEqual([1, 3], [issue["priority"] for issue in chosen])


class ApplyTest(unittest.TestCase):

    def test_trim_narrows_the_scene(self):
        plan = _plan()
        issue = _issue(op="trim", source_start=105.0, source_end=115.0)

        updated, applied = reviewer.apply_issues(plan, [issue], _ENABLED)

        self.assertEqual(1, len(applied))
        self.assertEqual(105.0, updated["scenes"][0]["source_start"])
        self.assertEqual(115.0, updated["scenes"][0]["source_end"])

    # 伸ばす指摘は当てない (素材が無い)。
    def test_trim_outside_is_not_applied(self):
        plan = _plan()
        issue = _issue(op="trim", source_start=90.0, source_end=130.0)

        updated, applied = reviewer.apply_issues(plan, [issue], _ENABLED)

        self.assertEqual([], applied)
        self.assertEqual(100.0, updated["scenes"][0]["source_start"])

    def test_drop_removes_the_scene(self):
        updated, applied = reviewer.apply_issues(_plan(), [_issue(op="drop")],
                                                 _ENABLED)

        self.assertEqual(["sc2"], [scene["id"] for scene in updated["scenes"]])
        self.assertEqual([1], [scene["order"] for scene in updated["scenes"]])
        self.assertEqual(1, len(applied))

    # 最後の 1 シーンは落とさない (何も残らなくなる)。
    def test_drop_keeps_the_last_scene(self):
        plan = _plan(scenes=[{"id": "sc1", "source_start": 100.0,
                              "source_end": 120.0, "role": "hook", "order": 1,
                              "reason": ""}], subtitles=[])

        _, applied = reviewer.apply_issues(plan, [_issue(op="drop")], _ENABLED)

        self.assertEqual([], applied)

    def test_reorder(self):
        issue = _issue(op="reorder", order=[2, 1])

        updated, applied = reviewer.apply_issues(_plan(), [issue], _ENABLED)

        self.assertEqual(["sc2", "sc1"], [scene["id"] for scene in updated["scenes"]])
        self.assertEqual(1, len(applied))

    def test_reorder_with_a_wrong_set_is_not_applied(self):
        issue = _issue(op="reorder", order=[1, 1])

        _, applied = reviewer.apply_issues(_plan(), [issue], _ENABLED)

        self.assertEqual([], applied)

    def test_edit_subtitle(self):
        issue = _issue(op="edit_subtitle", source_start=101.0, text="やばすぎ")

        updated, applied = reviewer.apply_issues(_plan(), [issue], _ENABLED)

        self.assertEqual("やばすぎ", updated["subtitles"][0]["text"])
        self.assertEqual(1, len(applied))

    def test_add_subtitle(self):
        issue = _issue(op="add_subtitle", source_start=110.0, source_end=112.0,
                       text="足した")

        updated, applied = reviewer.apply_issues(_plan(), [issue], _ENABLED)

        self.assertEqual(2, len(updated["subtitles"]))
        self.assertEqual(1, len(applied))

    def test_set_animation(self):
        issue = _issue(op="set_animation", source_start=101.0, animation="tsukkomi")

        updated, applied = reviewer.apply_issues(_plan(), [issue], _ENABLED)

        self.assertEqual("tsukkomi", updated["subtitles"][0]["animation"])

    def test_unknown_animation_is_not_applied(self):
        issue = _issue(op="set_animation", source_start=101.0, animation="word_pop")

        _, applied = reviewer.apply_issues(_plan(), [issue], _ENABLED)

        self.assertEqual([], applied)

    # コメント役割に動きは付かない (§9.4)。
    def test_animation_on_a_comment_is_not_applied(self):
        plan = _plan(subtitles=[
            {"scene_id": "sc1", "source_start": 101.0, "source_end": 103.0,
             "text": "コメント", "track_role": "comment", "animation": "",
             "fade_in": False, "fade_out": False, "color": "", "align": "",
             "reason": ""}])
        issue = _issue(op="set_animation", source_start=101.0, animation="shake")

        _, applied = reviewer.apply_issues(plan, [issue], _ENABLED)

        self.assertEqual([], applied)

    # 登録されていないプリセットは EditPlan へ入れない (PDF の提案に回る / §12.6)。
    def test_add_sound_without_a_preset_is_not_applied(self):
        issue = _issue(op="add_sound", source_start=105.0, preset="surprise")

        _, applied = reviewer.apply_issues(_plan(), [issue], _ENABLED)

        self.assertEqual([], applied)

    def test_add_sound_with_a_registered_preset(self):
        settings = {"ai": {"sounds": {"enabled": True,
                                      "presets": {"surprise": "C:/se/a.wav"}}}}
        issue = _issue(op="add_sound", source_start=105.0, preset="surprise")

        updated, applied = reviewer.apply_issues(_plan(), [issue], settings)

        self.assertEqual(1, len(applied))
        self.assertEqual(5.0, updated["sounds"][0]["offset_sec"])

    def test_unapplied_issue_is_recorded(self):
        from src.ai.plan_validator import PlanReport

        report = PlanReport()
        reviewer.apply_issues(_plan(), [_issue(op="trim")], _ENABLED, report)

        self.assertEqual(["R4"], [note["check"] for note in report.notes])


class RoundTest(unittest.TestCase):

    def test_rounds_stop_when_nothing_is_left(self):
        settings = {"ai": {"review": {"enabled": True, "max_rounds": 3,
                                      "min_severity": "major",
                                      "roles": {"director": False, "critic": True,
                                                "viewer": False, "platform": False,
                                                "qa": False}},
                           "vision": {"enabled": False}}}
        provider = _Provider(issues=[])

        result = reviewer.review(_plan(), None, [], provider, settings,
                                 "youtube_shorts")

        self.assertEqual(1, result.rounds, "指摘が無ければ 1 往復で止める")
        self.assertEqual(1, len(provider.requests))

    def test_max_rounds_is_respected(self):
        settings = {"ai": {"review": {"enabled": True, "max_rounds": 2,
                                      "min_severity": "major",
                                      "max_issues_per_round": 1,
                                      "roles": {"director": False, "critic": True,
                                                "viewer": False, "platform": False,
                                                "qa": False}},
                           "vision": {"enabled": False}}}
        provider = _Provider(issues=[{
            "id": "i1", "at": {"start": 100.0, "end": 120.0, "coord": "source"},
            "scene_id": "sc1", "kind": "pacing", "severity": "major",
            "detail": "d", "reason": "r",
            "fix": {"op": "trim", "target": "sc1", "source_start": 105.0,
                    "source_end": 115.0},
            "expected": "e", "priority": 1}])

        result = reviewer.review(_plan(), None, [], provider, settings,
                                 "youtube_shorts")

        self.assertEqual(2, result.rounds)
        self.assertEqual(2, len(provider.requests))

    # 未解決の critical は隠さない (§12.4)。
    def test_unresolved_critical_is_reported(self):
        settings = {"ai": {"review": {"enabled": True, "max_rounds": 1,
                                      "min_severity": "critical",
                                      "roles": {"director": False, "critic": True,
                                                "viewer": False, "platform": False,
                                                "qa": False}},
                           "vision": {"enabled": False}}}
        provider = _Provider(issues=[{
            "id": "i9", "at": {"start": 100.0, "end": 120.0, "coord": "source"},
            "scene_id": "sc1", "kind": "visual", "severity": "critical",
            "detail": "暗転している", "reason": "r",
            "fix": {"op": "none"}, "expected": "e", "priority": 1}])

        result = reviewer.review(_plan(), None, [], provider, settings,
                                 "youtube_shorts")

        self.assertEqual(["i9"], [issue["id"] for issue in result.unresolved])


class VisionTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.input_path = os.path.join(self._dir.name, "vod.mp4")
        with open(self.input_path, "wb") as handle:
            handle.write(b"0")
        self.taken = []

    def tearDown(self):
        self._dir.cleanup()

    def _extract(self, fail=False, size=16):
        def extract(input_path, at_sec, dest, max_edge_px, ffmpeg_cfg):
            self.taken.append((round(at_sec, 1), max_edge_px, dest))
            if fail:
                return False
            with open(dest, "wb") as handle:
                handle.write(b"x" * size)

            return True

        return extract

    def _settings(self, **vision_cfg):
        cfg = {"enabled": True, "frames_per_scene": 1, "max_frames": 12,
               "max_edge_px": 768, "points": ["peak", "head"]}
        cfg.update(vision_cfg)

        return {"ai": {"vision": cfg}}

    def test_one_frame_per_scene_by_default(self):
        images = vision.frames_for_plan(
            self.input_path, _plan(), self._settings(), workdir=self._dir.name,
            extract=self._extract())

        self.assertEqual(2, len(images))
        self.assertEqual("image/jpeg", images[0].media_type)
        self.assertTrue(images[0].data_base64)

    # peak があればそれを優先する (§12.5)。
    def test_peak_wins_when_available(self):
        scene = sf.RankedScene(1, 100.0, 120.0, peak=112.0)

        vision.frames_for_plan(self.input_path, _plan(), self._settings(),
                               scenes=[scene], workdir=self._dir.name,
                               extract=self._extract())

        self.assertEqual(112.0, self.taken[0][0])
        self.assertEqual(500.5, self.taken[1][0], "peak が無い側は先頭から")

    def test_frames_per_scene_and_max_frames(self):
        images = vision.frames_for_plan(
            self.input_path, _plan(),
            self._settings(frames_per_scene=2, max_frames=3,
                           points=["head", "middle"]),
            workdir=self._dir.name, extract=self._extract())

        self.assertEqual(3, len(images))

    def test_max_edge_is_passed(self):
        vision.frames_for_plan(self.input_path, _plan(),
                               self._settings(max_edge_px=512),
                               workdir=self._dir.name, extract=self._extract())

        self.assertEqual({512}, {edge for _, edge, _ in self.taken})

    # 送信用に読んだら消す (作業フォルダへ溜めない / §12.5)。
    def test_frames_are_deleted(self):
        vision.frames_for_plan(self.input_path, _plan(), self._settings(),
                               workdir=self._dir.name, extract=self._extract())

        self.assertEqual(["vod.mp4"], os.listdir(self._dir.name))

    def test_disabled_sends_nothing(self):
        images = vision.frames_for_plan(self.input_path, _plan(),
                                        self._settings(enabled=False),
                                        workdir=self._dir.name,
                                        extract=self._extract())

        self.assertEqual([], images)
        self.assertEqual([], self.taken)

    def test_missing_input_sends_nothing(self):
        images = vision.frames_for_plan("nope.mp4", _plan(), self._settings(),
                                        workdir=self._dir.name,
                                        extract=self._extract())

        self.assertEqual([], images)

    def test_failed_extraction_is_skipped(self):
        images = vision.frames_for_plan(self.input_path, _plan(), self._settings(),
                                        workdir=self._dir.name,
                                        extract=self._extract(fail=True))

        self.assertEqual([], images)


if __name__ == "__main__":
    unittest.main()
