# 経路 D (EditPlan → プロジェクト) の単体テスト (docs/AiEditor/resolve1.md §11.2 / §21 Phase 1 の 7 番)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・手書きの EditPlan を食わせるとプロジェクトと <project>.plan.json ができる
#   ・素材は plan の順・plan の区間で用意される (並べ替えのコマンドを使わない)
#   ・素材は <プロジェクト名>.media へ置く (一時フォルダだと開き直せない)
#   ・検証は素材を作る前に行う (使えない指示のために ffmpeg を回さない)
#   ・中止 (cancel_cb) は PipelineCancelled で、作りかけを残さない
#   ・縦ターゲットでは縦プロジェクトも作る (切り抜きは指定として持つ)
#   ・AI も ffmpeg も呼ばない (prepare と probe は差し替える)
import json
import os
import tempfile
import unittest
from unittest import mock

from src.ai import plan_schema as ps
from src.ai.provider import AiCompletion, AiProvider
from src.archive import timeline_builder as archive_timeline
from src.exceptions import (AiOfflineError, AiPlanError, InputError,
                            PipelineCancelled)
from src.modules import output_profile
from src.pipeline import ai_runner
from src.timeline import media_probe, project_io
from src.timeline.model import MEDIA_VIDEO, MediaRef


def _settings(output_dir):
    return {
        "general": {"output_directory": output_dir},
        "ffmpeg": {"output_width": 1920, "output_height": 1080, "output_fps": 60},
        "timeline": {"min_clip_sec": 0.05, "project_suffix": ".timeline.json",
                     "project_dir": output_dir,
                     "sound_tracks": {"enabled": True, "initial_tracks": 1}},
        "subtitle": {"max_line_length": 20, "subtitle_max_lines": 2},
        "ai": {"plan": {"youtube_long": {"min_sec": 0, "max_sec": 0, "max_scenes": 0},
                        "youtube_shorts": {"min_sec": 0, "max_sec": 0,
                                           "max_scenes": 0},
                        # AI に本文も追加も任せる側を記録する。
                        # key / none は tests/test_error_20261125c_telop.py
                        "subtitle_mode": "full"},
               # 既定ではシーン評価が切られ、レビューが有効になっている
               # (docs/error/20261125c/resolve.md §2.6)。このファイルは
               # **評価あり・レビューなし**の経路を記録するため明示する。
               "scene_eval": {"enabled": True},
               "review": {"enabled": False},
               "sounds": {"enabled": True, "presets": {}}},
    }


def _plan(scenes, target="youtube_long", subtitles=None):
    plan = ps.new_plan(target, {"kind": "vod", "video_id": "1"}, duration_sec=7200.0)
    plan["scenes"] = scenes
    plan["subtitles"] = subtitles or []

    return plan


def _scene(scene_id, start, end, order=1, **extra):
    scene = {"id": scene_id, "source_start": start, "source_end": end,
             "role": "body", "order": order}
    scene.update(extra)

    return scene


# run_ai 用の固定応答プロバイダー。役は cache_prefix_id で見分ける。
class _AiProvider(AiProvider):

    name = "fake"

    def __init__(self, fail=False):
        self._fail = bool(fail)
        self.requests = []

    def complete_json(self, request):
        self.requests.append(request)
        if self._fail:
            raise AiOfflineError("接続できません")

        return AiCompletion(result=self._result(request), model="claude-opus-5-5")

    def _result(self, request):
        if request.cache_prefix_id == "scene_eval-v1":
            indexes = [row["index"] for row in request.payload["candidates"]]

            return {"scenes": [
                {"candidate_index": index, "start": None, "end": None,
                 "scores": {key: 60 for key in ps.SCORE_KEYS},
                 "summary": "要約", "reason": "理由"} for index in indexes]}
        if request.cache_prefix_id == "plan-v1":
            scenes = request.payload["scenes"][:2]

            return {"target": request.payload["target"], "title": "題名",
                    "concept": "狙い", "duration_sec": 60.0,
                    "sections": [
                        {"role": "opening", "scene_ref": scene["scene_ref"],
                         "start": scene["start"], "end": scene["end"],
                         "reason": "r"} for scene in scenes],
                    "direction": {"subtitle": "full", "emphasis_scenes": [],
                                  "sound_effects": "light", "bgm": "none",
                                  "vertical_crop": False}}

        sections = request.payload["sections"]

        return {"plan_version": 1,
                "scenes": [
                    {"id": "sc{0}".format(position + 1),
                     "source_start": section["start"], "source_end": section["end"],
                     "role": section["role"], "order": position + 1, "reason": "r"}
                    for position, section in enumerate(sections)],
                "subtitles": [], "sounds": [], "editing": {"subtitle": True},
                "notes": ""}

    def count_tokens(self, request):
        return None

    def server_config(self):
        return None


# 共通の下ごしらえ (ffmpeg を呼ばずに経路 D を流す)。
class RunnerBase(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = self._dir.name
        self.input_path = os.path.join(self.root, "vod_123.mp4")
        with open(self.input_path, "wb") as handle:
            handle.write(b"0")
        self.settings = _settings(self.root)
        self.prepared_calls = []

        # ffmpeg を呼ばない。キャンバスの解決も素材を読まずに通す。
        self._patches = [
            mock.patch.object(ai_runner.ffmpeg_runner, "ensure_available",
                              lambda cfg: None),
            mock.patch.object(ai_runner.output_profile, "apply_canvas_to_settings",
                              lambda settings, input_path=None, canvas=None: settings),
            mock.patch.object(archive_timeline.media_probe, "probe", self._probe),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self._patches):
            patch.stop()
        self._dir.cleanup()

    # 素材の調べ物 (ffprobe) の代わり。
    def _probe(self, path, media_id, settings=None, cfg=None):
        return MediaRef(media_id, MEDIA_VIDEO, path, duration_sec=30.0,
                        width=1920, height=1080, fps=60.0, has_audio=True)

    # 素材の用意 (切り出し・正規化・文字起こし) の代わり。
    def _prepare(self, input_path, settings, clip_settings, clip, ffmpeg_cfg, workdir,
                 use_timeline=False, **kwargs):
        self.prepared_calls.append((clip["index"], clip["start"], clip["end"],
                                    workdir, use_timeline))
        path = os.path.join(workdir, "clip{0}.mp4".format(clip["index"]))
        os.makedirs(workdir, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"0")
        length = float(clip["end"]) - float(clip["start"])

        return {"index": clip["index"], "start": clip["start"], "end": clip["end"],
                "score": 0.0, "prepared_path": "", "profile": None, "eff_cfg": {},
                "items": [], "keep_segments": [(0.0, length)],
                "normalized_path": path, "normalized_duration": length,
                "media_role": "normalized", "gain_db": None,
                "marker": False, "marker_labels": []}

    def run_plan(self, plan, **kwargs):
        return ai_runner.run_plan(self.input_path, self.settings, plan,
                                  prepare=self._prepare, duration_sec=7200.0,
                                  **kwargs)


class AiRunnerTest(RunnerBase):

    def test_project_and_plan_are_written(self):
        plan = _plan([_scene("sc1", 100.0, 130.0, order=1),
                      _scene("sc2", 500.0, 520.0, order=2)])

        result = self.run_plan(plan)

        self.assertTrue(os.path.exists(result.project_path))
        self.assertTrue(os.path.exists(result.plan_path))
        self.assertTrue(result.plan_path.endswith(ps.PLAN_SUFFIX))
        with open(result.plan_path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(["sc1", "sc2"], [s["id"] for s in saved["scenes"]])

    # 素材は plan の順・plan の区間で用意される。
    def test_media_is_prepared_in_plan_order(self):
        plan = _plan([_scene("late", 500.0, 520.0, order=1),
                      _scene("early", 100.0, 130.0, order=2)])

        result = self.run_plan(plan)

        self.assertEqual([(0, 500.0, 520.0), (1, 100.0, 130.0)],
                         [(index, start, end)
                          for index, start, end, _, _ in self.prepared_calls])
        clips = result.timeline.base_clips(include_disabled=True)
        self.assertEqual([0.0, 20.0], [clip.timeline_start for clip in clips])
        self.assertEqual([20.0, 30.0], [clip.duration for clip in clips])

    # Timeline 経路で用意する (実カットしない)。素材はプロジェクトの隣。
    def test_media_dir_is_next_to_the_project(self):
        plan = _plan([_scene("sc1", 100.0, 130.0)])

        result = self.run_plan(plan)

        self.assertTrue(result.media_dir.endswith(".media"))
        self.assertEqual(os.path.dirname(os.path.abspath(result.project_path)),
                         os.path.dirname(os.path.abspath(result.media_dir)))
        self.assertTrue(all(use_timeline for _, _, _, _, use_timeline
                            in self.prepared_calls))

    # 検証は素材より先。VOD の尺を超える指示のために ffmpeg を回さない。
    def test_validation_runs_before_preparing(self):
        plan = _plan([_scene("sc1", 100.0, 130.0, order=1),
                      _scene("over", 8000.0, 8100.0, order=2)])

        result = self.run_plan(plan)

        self.assertEqual([0], [index for index, _, _, _, _ in self.prepared_calls])
        self.assertEqual(1, result.report.dropped)

    def test_no_usable_scene_is_fatal(self):
        plan = _plan([_scene("over", 8000.0, 8100.0)])

        with self.assertRaises(AiPlanError):
            self.run_plan(plan)

        self.assertEqual([], self.prepared_calls)

    def test_missing_input_is_fatal(self):
        with self.assertRaises(InputError):
            ai_runner.run_plan(os.path.join(self.root, "nope.mp4"), self.settings,
                               _plan([_scene("sc1", 1.0, 2.0)]),
                               prepare=self._prepare, duration_sec=100.0)

    # 中止は PipelineCancelled。プロジェクトは書かない。
    def test_cancel_stops_before_saving(self):
        plan = _plan([_scene("sc1", 100.0, 130.0)])

        with self.assertRaises(PipelineCancelled):
            self.run_plan(plan, cancel_cb=lambda: True)

        self.assertEqual([], [name for name in os.listdir(self.root)
                              if name.endswith(".timeline.json")])

    def test_progress_is_reported_in_order(self):
        labels = []
        plan = _plan([_scene("sc1", 100.0, 130.0)])

        self.run_plan(plan, progress_cb=lambda ratio, label: labels.append(
            (round(ratio, 2), label)))

        self.assertEqual(sorted(ratio for ratio, _ in labels),
                         [ratio for ratio, _ in labels], "進捗は戻らない")
        self.assertEqual(1.0, labels[-1][0])

    # 進捗の失敗で本処理を止めない。
    def test_broken_progress_callback_does_not_stop(self):
        def broken(ratio, label):
            raise RuntimeError("boom")

        result = self.run_plan(_plan([_scene("sc1", 100.0, 130.0)]),
                               progress_cb=broken)

        self.assertTrue(os.path.exists(result.project_path))

    # テロップの指示も適用される (plan_applier 経由)。
    def test_subtitles_are_applied(self):
        plan = _plan([_scene("sc1", 100.0, 130.0)],
                     subtitles=[{"scene_id": "sc1", "source_start": 105.0,
                                 "source_end": 107.0, "text": "AI のテロップ",
                                 "track_role": "own", "animation": "shake"}])

        result = self.run_plan(plan)
        clips = result.timeline.subtitle_clips(only_used=False)

        self.assertEqual(["AI のテロップ"], [clip.text for clip in clips])
        self.assertEqual(5.0, clips[0].timeline_start)
        self.assertEqual("shake", clips[0].animation)

    # 保存したプロジェクトはアーカイブ用として開ける (既存の経路が読める形)。
    def test_saved_project_is_an_archive_project(self):
        plan = _plan([_scene("sc1", 100.0, 130.0)])

        result = self.run_plan(plan)
        with open(result.project_path, encoding="utf-8") as handle:
            data = json.load(handle)

        self.assertEqual(project_io.KIND_ARCHIVE, project_io.project_kind(data))
        self.assertEqual(ps.PLAN_VERSION, 1)

    # 適用は履歴に乗る = 画面で Undo できる。
    def test_history_is_returned(self):
        plan = _plan([_scene("sc1", 105.0, 125.0)],
                     subtitles=[{"scene_id": "sc1", "source_start": 110.0,
                                 "source_end": 112.0, "text": "足した",
                                 "track_role": "own"}])

        result = self.run_plan(plan)

        self.assertTrue(result.stack.can_undo())
        self.assertTrue(result.stack.is_clean(), "保存した時点を保存点にする")


class VerticalTargetTest(RunnerBase):

    # 縦ターゲットでは縦プロジェクトも作る。切り抜きは指定として持つ (動画は作らない)。
    def test_vertical_project_is_created(self):
        plan = _plan([_scene("sc1", 100.0, 130.0, crop={"mode": "left",
                                                        "scale": 1.0})],
                     target="youtube_shorts")

        result = self.run_plan(plan, target="youtube_shorts")

        self.assertTrue(result.vertical_path)
        self.assertTrue(os.path.exists(result.vertical_path))
        with open(result.vertical_path, encoding="utf-8") as handle:
            data = json.load(handle)
        layout = data["source"]["crop"]
        self.assertEqual("single", layout["mode"])
        self.assertEqual([0, 0, 608, 1080], layout["frames"][0]["src"],
                         "left は素材の左端から 9:16 を取る")
        self.assertEqual(project_io.KIND_CLIP, project_io.project_kind(data))

    def test_center_is_the_default_window(self):
        plan = _plan([_scene("sc1", 100.0, 130.0)], target="tiktok")
        self.settings["ai"]["plan"]["tiktok"] = {"min_sec": 0, "max_sec": 0,
                                                 "max_scenes": 0}

        result = self.run_plan(plan, target="tiktok")

        with open(result.vertical_path, encoding="utf-8") as handle:
            layout = json.load(handle)["source"]["crop"]

        self.assertEqual(656, layout["frames"][0]["src"][0])

    def test_vertical_can_be_skipped(self):
        plan = _plan([_scene("sc1", 100.0, 130.0)], target="youtube_shorts")

        result = self.run_plan(plan, target="youtube_shorts", vertical=False)

        self.assertEqual("", result.vertical_path)

    # 縦が作れなくても横のプロジェクトは完成している (後から作り直せる)。
    def test_failure_keeps_the_horizontal_project(self):
        plan = _plan([_scene("sc1", 100.0, 130.0)], target="youtube_shorts")

        def broken_build(*args, **kwargs):
            raise ValueError("縦にできるクリップが選ばれていません")

        result = self.run_plan(plan, target="youtube_shorts",
                               vertical_build=broken_build)

        self.assertEqual("", result.vertical_path)
        self.assertTrue(os.path.exists(result.project_path))
        self.assertIn("A8", [n["check"] for n in result.report.notes])


if __name__ == "__main__":
    unittest.main()


# AI に決めさせる経路 (run_ai)。解析・文字起こし・素材の用意はすべて差し替える。
class RunAiTest(RunnerBase):

    def setUp(self):
        super().setUp()
        self.analyzed = []
        self.transcribed = []
        # analyze へ渡された採点スイープ (resolve2.md §6.2)
        self.scan_runners = []

    def _analysis(self, count=3):
        return {"duration": 7200.0, "curve": [],
                "clips": [{"index": i + 1, "start": 100.0 + i * 500,
                           "end": 130.0 + i * 500, "score": 8.0 - i,
                           "emotion": 5.0, "comment": 5.0} for i in range(count)],
                "audio_scan": None}

    # 本物の archive.pipeline.analyze と同じ形で受ける。
    # scan は AI の採点スイープ (docs/AiEditor/resolve2.md §6.2)。
    # 渡されたかどうかだけ控え、ここでは呼ばない (AI も ffmpeg も動かさない)。
    def _analyze(self, input_path, settings, progress_cb=None, comments=None,
                 markers=None, scan=None):
        self.analyzed.append(settings["archive"]["scoring"]["top_n"])
        self.scan_runners.append(scan)
        if progress_cb is not None:
            progress_cb(1.0, "採点完了")

        return self._analysis()

    def _transcribe(self, input_path, settings, candidates, workdir, **kwargs):
        self.transcribed.append([(c["start"], c["end"]) for c in candidates])

        return [{"start": 105.0, "end": 108.0, "text": "やばい"}]

    def _provider(self, **kwargs):
        return _AiProvider(**kwargs)

    def run_ai(self, provider=None, **kwargs):
        return ai_runner.run_ai(
            self.input_path, self.settings, provider=provider,
            analyze=self._analyze, transcribe=self._transcribe,
            prepare=self._prepare, **kwargs)

    def test_end_to_end_builds_a_project(self):
        provider = self._provider()

        result = self.run_ai(provider, targets=["youtube_long"])

        self.assertEqual(1, len(result.runs))
        self.assertTrue(os.path.exists(result.runs[0].project_path))
        self.assertTrue(os.path.exists(result.runs[0].plan_path))
        # edit_plan の鍵はテロップの扱い (subtitle_mode) で分かれる
        self.assertEqual(["scene_eval-v1", "plan-v1", "edit_plan-v1-full"],
                         [request.cache_prefix_id for request in provider.requests])

    # 候補の本数は AI 用 (既定 30) へ差し替える。設定ファイルは書き換えない (§7.1)。
    def test_candidate_count_is_the_ai_one(self):
        self.settings["archive"] = {"scoring": {"top_n": 10}}

        self.run_ai(self._provider(), targets=["youtube_long"])

        self.assertEqual([30], self.analyzed)
        self.assertEqual(10, self.settings["archive"]["scoring"]["top_n"])

    def test_transcription_covers_the_candidates(self):
        self.run_ai(self._provider(), targets=["youtube_long"])

        self.assertEqual([[(100.0, 130.0), (600.0, 630.0), (1100.0, 1130.0)]],
                         self.transcribed)

    # 評価は <project>.ai.json へ残る (§7.2)。
    def test_evaluation_is_saved(self):
        result = self.run_ai(self._provider(), targets=["youtube_long"])

        self.assertTrue(os.path.exists(result.eval_path))
        self.assertEqual(3, len(result.scenes))

    # ターゲットごとにプロジェクトを作る (§11.2)。
    def test_each_target_gets_its_own_project(self):
        result = self.run_ai(self._provider(),
                             targets=["youtube_long", "youtube_shorts"])

        self.assertEqual(2, len(result.runs))
        self.assertNotEqual(result.runs[0].project_path, result.runs[1].project_path)
        self.assertTrue(result.runs[1].vertical_path, "短尺は縦も作る")

    # 解析済みなら採点をやり直さない。
    def test_given_analysis_is_reused(self):
        self.run_ai(self._provider(), targets=["youtube_long"],
                    analysis=self._analysis())

        self.assertEqual([], self.analyzed)

    # AI が無くても既存の採点だけで完走する (経路 B 相当 / §11.2)。
    def test_without_a_provider_it_still_completes(self):
        result = self.run_ai(None, targets=["youtube_long"])

        self.assertEqual(1, len(result.runs))
        self.assertTrue(os.path.exists(result.runs[0].project_path))
        self.assertFalse(any(scene.evaluated for scene in result.scenes))

    # AI が落ちても完走する (縮退の件数は記録に残る)。
    def test_provider_failure_degrades(self):
        result = self.run_ai(self._provider(fail=True), targets=["youtube_long"])

        self.assertEqual(1, len(result.runs))
        self.assertTrue([n for n in result.report.notes
                         if n["check"] in ("E2", "P4")])

    def test_no_candidate_is_fatal(self):
        def empty(input_path, settings, progress_cb=None, comments=None,
                  markers=None, scan=None):
            return {"duration": 10.0, "curve": [], "clips": [],
                    "audio_scan": None, "moments": []}

        with self.assertRaises(AiPlanError):
            ai_runner.run_ai(self.input_path, self.settings,
                             provider=self._provider(), analyze=empty,
                             transcribe=self._transcribe, prepare=self._prepare,
                             targets=["youtube_long"])

    def test_cancel_stops_the_run(self):
        with self.assertRaises(PipelineCancelled):
            self.run_ai(self._provider(), targets=["youtube_long"],
                        cancel_cb=lambda: True)

    def test_unknown_target_is_fatal(self):
        with self.assertRaises(AiPlanError):
            self.run_ai(self._provider(), targets=["instagram"])

    def test_progress_reaches_the_end(self):
        seen = []

        self.run_ai(self._provider(), targets=["youtube_long"],
                    progress_cb=lambda ratio, label: seen.append(round(ratio, 2)))

        self.assertEqual(sorted(seen), seen, "進捗は戻らない")
        self.assertEqual(1.0, seen[-1])
