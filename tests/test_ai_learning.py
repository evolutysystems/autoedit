# 編集の統計とプロファイルの単体テスト (docs/AiEditor/resolve1.md §13 / §20)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・既存のプロジェクト JSON から統計が出る (**AI 不要**の段 / §13.2 の ①)
#   ・同じ入力なら同じ結果 (再現性)
#   ・プロファイルは stats (実測) と hypotheses (推定) を分けて持つ (§13.4)
#   ・`min_evidence` 未満の仮説と `rejected` の仮説はプロンプトへ入れない
#   ・更新履歴 (history) を必ず積む (§13.3)
#   ・置き場は %LOCALAPPDATA% 配下。**プロジェクトの隣には置かない** (§13.4)
#   ・古い差分は keep_days で掃除する。**プロファイルは捨てない**
# プロバイダーへは繋がない。ゴミ箱 API も呼ばない (差し替える)。
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from src.ai import edit_analyzer, profile, store
from src.ai.provider import AiCompletion, AiProvider
from src.exceptions import AiOfflineError


def _project(clips=None, subtitles=None, sounds=None, audio=None, duration=60.0,
             updated="2026-11-01T12:00:00"):
    return {
        "schema_version": 1,
        "updated_at": updated,
        "timeline": {"fps": 60, "duration_sec": duration},
        "source": {"input_path": "vod.mp4"},
        "tracks": [
            {"id": "V1", "kind": "video", "clips": clips if clips is not None else [
                {"id": "c1", "media_id": "m1", "timeline_start": 0.0,
                 "duration": 10.0, "source_in": 0.0, "source_out": 10.0,
                 "transform": {"x": 0.0, "y": 0.0, "scale": 1.0}},
                {"id": "c2", "media_id": "m1", "timeline_start": 10.0,
                 "duration": 5.0, "source_in": 12.0, "source_out": 17.0,
                 "transform": {"x": 0.0, "y": 0.0, "scale": 1.5}}]},
            {"id": "A1", "kind": "audio", "clips": audio if audio is not None else [
                {"id": "a1", "link_clip": "c1", "gain_db": -3.0, "muted": False}]},
            {"id": "S1", "kind": "subtitle",
             "clips": subtitles if subtitles is not None else [
                 {"id": "s1", "timeline_start": 1.0, "duration": 2.0,
                  "text": "やばいって", "animation": "tsukkomi", "fade_in": True,
                  "color": "#FF0000", "font_size": 60, "align": "center",
                  "transform": {"x": None, "y": None}},
                 {"id": "s2", "timeline_start": 4.0, "duration": 2.0,
                  "text": "そんなわけ", "animation": "", "fade_in": False,
                  "color": "", "font_size": None, "align": "",
                  "transform": {"x": None, "y": None}}]},
            {"id": "SE1", "kind": "sound", "clips": sounds if sounds is not None else [
                {"id": "se1", "media_id": "m2", "timeline_start": 3.0,
                 "duration": 1.0, "source_in": 0.0, "source_out": 1.0,
                 "gain_db": -6.0, "muted": False}]},
        ],
    }


class AnalyzeTest(unittest.TestCase):

    def test_measures_one_project(self):
        measure = edit_analyzer.analyze_project(_project())

        self.assertEqual([10.0, 5.0], measure["clip_sec"])
        # 素材内の隙間 = カットで取り除いた長さ
        self.assertEqual([2.0], measure["cut_gap_sec"])
        self.assertEqual([5, 5], measure["subtitle_chars"])
        self.assertEqual(["tsukkomi", ""], measure["animations"])
        self.assertEqual(1, measure["zoomed"])
        self.assertEqual(1, measure["sound_count"])

    def test_unknown_schema_is_skipped(self):
        data = _project()
        data["schema_version"] = 99

        self.assertIsNone(edit_analyzer.analyze_project(data))

    def test_missing_file_is_skipped(self):
        self.assertIsNone(edit_analyzer.analyze_project("nope.timeline.json"))

    def test_summary_over_several_projects(self):
        analysis = edit_analyzer.analyze_paths(
            [_project(), _project(duration=120.0)],
            loader=edit_analyzer.analyze_project)
        stats = analysis["stats"]

        self.assertEqual(2, analysis["sample"]["projects"])
        self.assertEqual(4, analysis["sample"]["clips"])
        self.assertEqual(4, analysis["sample"]["subtitles"])
        self.assertEqual(0.5, stats["animation_rate"])
        self.assertEqual([["tsukkomi", 0.5]], stats["animation_top"])
        self.assertEqual(0.5, stats["zoom_rate"])
        self.assertEqual(5.0, stats["clip_sec"]["p25"])
        self.assertEqual("2026-11-01", analysis["sample"]["from"])

    # 同じ入力なら同じ結果 (再現性がある = AI を呼ばない段の価値)。
    def test_repeatable(self):
        first = edit_analyzer.analyze_paths([_project()],
                                            loader=edit_analyzer.analyze_project)
        second = edit_analyzer.analyze_paths([_project()],
                                             loader=edit_analyzer.analyze_project)

        self.assertEqual(first, second)

    def test_broken_project_does_not_stop_the_rest(self):
        def loader(value):
            if value == "broken":
                raise ValueError("boom")

            return edit_analyzer.analyze_project(value)

        analysis = edit_analyzer.analyze_paths(["broken", _project()], loader=loader)

        self.assertEqual(1, analysis["sample"]["projects"])

    def test_empty_input(self):
        analysis = edit_analyzer.analyze_paths([])

        self.assertEqual(0, analysis["sample"]["projects"])
        self.assertEqual(0.0, analysis["stats"]["animation_rate"])

    def test_reads_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "a.timeline.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(_project(), handle)

            measure = edit_analyzer.analyze_project(path)

            self.assertEqual([10.0, 5.0], measure["clip_sec"])


class _Provider(AiProvider):

    name = "fake"

    def __init__(self, hypotheses=None, fail=False):
        self._hypotheses = hypotheses or []
        self._fail = fail
        self.requests = []

    def complete_json(self, request):
        self.requests.append(request)
        if self._fail:
            raise AiOfflineError("接続できません")

        return AiCompletion(result={"hypotheses": self._hypotheses},
                            model="claude-sonnet-5-5")

    def count_tokens(self, request):
        return None

    def server_config(self):
        return None


class ProfileTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def _analysis(self):
        return edit_analyzer.analyze_paths([_project()],
                                           loader=edit_analyzer.analyze_project)

    def test_build_has_stats_and_history(self):
        built = profile.build(self._analysis(), name="ゲーム実況 / 長尺",
                              target="youtube_long")

        self.assertEqual(profile.PROFILE_VERSION, built["profile_version"])
        self.assertEqual("youtube_long", built["scope"]["target"])
        self.assertTrue(built["stats"])
        self.assertEqual([], built["hypotheses"])
        self.assertEqual("created", built["history"][0]["change"])
        self.assertTrue(built["avoid"], "機械由来の制約を持つ")

    def test_update_stats_appends_history(self):
        built = profile.build(self._analysis(), target="youtube_long")

        profile.update_stats(built, self._analysis())

        self.assertEqual(["created", "stats"],
                         [entry["change"] for entry in built["history"]])

    def test_hypotheses_come_from_the_ai(self):
        built = profile.build(self._analysis(), target="youtube_long")
        provider = _Provider(hypotheses=[
            {"text": "オチの直前は間を詰める傾向がある", "confidence": "medium",
             "evidence": 99}])
        diffs = [{"kind": "cut_shorten"}] * 12

        _, added = profile.add_hypotheses(built, provider, {}, diffs=diffs)

        self.assertEqual(1, len(added))
        self.assertEqual("unconfirmed", added[0]["status"])
        # 件数は AI の自己申告を信用しない (差分の実数を上限にする)
        self.assertEqual(12, added[0]["evidence"])
        self.assertEqual(["created", "hypotheses"],
                         [entry["change"] for entry in built["history"]])

    def test_duplicate_hypotheses_are_ignored(self):
        built = profile.build(self._analysis(), target="youtube_long")
        provider = _Provider(hypotheses=[{"text": "同じ文", "confidence": "low"},
                                         {"text": "同じ文", "confidence": "high"}])

        _, added = profile.add_hypotheses(built, provider, {}, diffs=[{"kind": "x"}])

        self.assertEqual(1, len(added))

    def test_ai_failure_keeps_the_profile(self):
        built = profile.build(self._analysis(), target="youtube_long")

        updated, added = profile.add_hypotheses(built, _Provider(fail=True), {})

        self.assertEqual([], added)
        self.assertTrue(updated["stats"])

    def test_without_a_provider_nothing_is_asked(self):
        built = profile.build(self._analysis(), target="youtube_long")

        _, added = profile.add_hypotheses(built, None, {})

        self.assertEqual([], added)

    # min_evidence 未満の仮説はプロンプトへ入れない (§13.4)。
    def test_weak_hypotheses_are_not_sent(self):
        built = profile.build(self._analysis(), target="youtube_long")
        built["hypotheses"] = [
            {"id": "h1", "text": "弱い", "confidence": "low", "evidence": 3,
             "status": "unconfirmed"},
            {"id": "h2", "text": "強い", "confidence": "high", "evidence": 20,
             "status": "unconfirmed"}]

        payload = profile.for_prompt(built, {"ai": {"learning": {"min_evidence": 10}}})

        self.assertEqual(["強い"], [item["text"] for item in payload["tendencies"]])
        self.assertIn("参考", payload["note"])

    def test_rejected_hypotheses_are_not_sent(self):
        built = profile.build(self._analysis(), target="youtube_long")
        built["hypotheses"] = [
            {"id": "h1", "text": "却下された", "confidence": "high", "evidence": 50,
             "status": "rejected"}]

        payload = profile.for_prompt(built, {})

        self.assertEqual([], payload["tendencies"])

    # 利用者が採否を決められる (§13.3)。
    def test_user_can_set_the_status(self):
        built = profile.build(self._analysis(), target="youtube_long")
        built["hypotheses"] = [{"id": "h1", "text": "t", "confidence": "low",
                                "evidence": 20, "status": "unconfirmed"}]

        self.assertTrue(profile.set_status(built, "h1", "rejected", note="違う"))
        self.assertEqual("rejected", built["hypotheses"][0]["status"])
        self.assertEqual("違う", built["hypotheses"][0]["user_note"])
        self.assertEqual("status", built["history"][-1]["change"])

    def test_unknown_status_is_refused(self):
        built = profile.build(self._analysis(), target="youtube_long")
        built["hypotheses"] = [{"id": "h1", "text": "t", "confidence": "low",
                                "evidence": 1, "status": "unconfirmed"}]

        self.assertFalse(profile.set_status(built, "h1", "bogus"))

    # 置き場は %LOCALAPPDATA% 配下。プロジェクトの隣には置かない (§13.4)。
    def test_save_and_load(self):
        built = profile.build(self._analysis(), name="長尺", target="youtube_long")

        path = profile.save(built, root=self.root)

        self.assertTrue(path.startswith(self.root))
        self.assertIn("profiles", path)
        self.assertEqual(built["slug"], profile.load(built["slug"],
                                                     root=self.root)["slug"])

    def test_active_profile_comes_from_the_settings(self):
        built = profile.build(self._analysis(), name="長尺", target="youtube_long")
        profile.save(built, root=self.root)
        settings = {"ai": {"profile": {"active": {"youtube_long": built["slug"]}}}}

        loaded = profile.active(settings, "youtube_long", root=self.root)

        self.assertEqual(built["slug"], loaded["slug"])
        self.assertIsNone(profile.active({}, "youtube_long", root=self.root),
                          "未設定なら使わない")

    def test_for_prompt_of_none(self):
        self.assertIsNone(profile.for_prompt(None, {}))


class StoreTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def test_paths_are_under_localappdata(self):
        self.assertIn("Stretheus", store.root_dir())
        self.assertTrue(store.profile_path("a").endswith(".json"))

    def test_slugify(self):
        self.assertEqual("ゲーム実況_長尺", store.slugify("ゲーム実況 / 長尺"))
        self.assertEqual("", store.slugify("///"))

    def test_json_round_trip(self):
        path = os.path.join(self.root, "a", "b.json")

        store.save_json({"a": 1}, path)

        self.assertEqual({"a": 1}, store.load_json(path))

    def test_broken_json_is_none(self):
        path = os.path.join(self.root, "broken.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{")

        self.assertIsNone(store.load_json(path))

    # 古い差分だけを掃除する。プロファイルは捨てない (§13.4)。
    def test_cleanup_removes_only_old_diffs(self):
        old = os.path.join(store.diffs_dir(self.root), "old.json")
        fresh = os.path.join(store.diffs_dir(self.root), "fresh.json")
        store.save_json({"a": 1}, old)
        store.save_json({"a": 1}, fresh)
        kept_profile = store.profile_path("keep", self.root)
        store.save_json({"a": 1}, kept_profile)
        long_ago = time.time() - 400 * 86400
        os.utime(old, (long_ago, long_ago))

        with mock.patch.object(store.trash, "is_available", lambda: False):
            removed, kept = store.cleanup({}, root=self.root)

        self.assertEqual(1, removed)
        self.assertEqual(1, kept)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(kept_profile), "プロファイルは捨てない")

    def test_cleanup_without_keep_days(self):
        self.assertEqual((0, 0), store.cleanup(
            {"ai": {"learning": {"keep_days": 0}}}, root=self.root))

    def test_list_profiles(self):
        store.save_json({"a": 1}, store.profile_path("one", self.root))
        store.save_json({"a": 1}, store.profile_path("two", self.root))

        self.assertEqual(["one", "two"],
                         [slug for slug, _ in store.list_profiles(self.root)])


if __name__ == "__main__":
    unittest.main()


class RefreshTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = self._dir.name
        self.paths = [_project(), _project(duration=120.0)]

    def tearDown(self):
        self._dir.cleanup()

    # 統計だけで完結する (AI 不要の段 / §13.2 の ①)。
    def test_refresh_without_a_provider(self):
        built, added = profile.refresh({}, self.paths, root=self.root,
                                       name="長尺", target="youtube_long")

        self.assertEqual(2, built["sample"]["projects"])
        self.assertEqual([], added)
        self.assertTrue(os.path.exists(store.profile_path(built["slug"], self.root)))

    # 2 回目は作り直さず、統計を入れ替えて履歴を積む。
    def test_refresh_updates_the_existing_profile(self):
        first, _ = profile.refresh({}, self.paths, root=self.root, name="長尺",
                                   target="youtube_long")

        again, _ = profile.refresh({}, self.paths, root=self.root, name="長尺",
                                   target="youtube_long")

        self.assertEqual(["created", "stats"],
                         [entry["change"] for entry in again["history"]])
        self.assertEqual(first["slug"], again["slug"])

    # 差分が溜まっていれば仮説の件数の上限になる (§13.4)。
    def test_refresh_uses_the_recorded_diffs(self):
        store.save_json({"diffs": [{"kind": "cut_shorten"}] * 7},
                        os.path.join(store.diffs_dir(self.root), "20261121-1_a.json"))
        provider = _Provider(hypotheses=[{"text": "導入を短くする傾向がある",
                                          "confidence": "medium", "evidence": 99}])

        _, added = profile.refresh({}, self.paths, provider=provider, root=self.root,
                                   target="youtube_long")

        self.assertEqual(7, added[0]["evidence"])

    def test_learning_off_does_nothing(self):
        built, added = profile.refresh({"ai": {"learning": {"enabled": False}}},
                                       self.paths, root=self.root)

        self.assertIsNone(built)
        self.assertEqual([], added)

    def test_no_measurable_project(self):
        built, _ = profile.refresh({}, [], root=self.root)

        self.assertIsNone(built)
