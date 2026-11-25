# クリップ最小尺の下限の一本化と、V15 の誤判定の回収 (docs/error/20261124)
# 実行: python -m unittest discover -s tests
#
# 要件 (docs/error/20261124/resolve.md §6):
#   ・編集点 → クリップの経路では timeline.min_clip_sec を下限にする (R1)
#     作る側 (silence_cutter / 各 builder) と検証側 (project_io) の下限が一致すること
#   ・適用前から不正だったクリップで AI 編集を失敗にしない (R2)
#     plan が本当に壊した場合は従来どおり巻き戻すこと
# ffmpeg も ffprobe も呼ばない (probe は差し替える)。
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.ai import plan_applier, plan_schema as ps, plan_validator as pv
from src.archive import timeline_builder
from src.exceptions import AiPlanError
from src.modules import silence_cutter
from src.timeline import builder as timeline_builder_mod
from src.timeline import project_io
from src.timeline.commands import CommandStack
from src.timeline.model import MEDIA_VIDEO, MediaRef

# 既存のテストヘルパを使い回す (Timeline の組み方を 2 通り持たないため)
from test_ai_plan_applier import _SETTINGS, _Scene, _fake_probe, _timeline

# 下限 (0.05) より長いが 0.01 より短くはない = 今回事故になった尺
_SHORT_SEC = 0.031

_CLIP_SETTINGS = {
    "general": {"opening_enabled": False, "ending_enabled": False},
    "ffmpeg": {"output_width": 1920, "output_height": 1080, "output_fps": 60},
    "timeline": {"min_clip_sec": 0.05},
}


def _probe(path, media_id, settings=None, cfg=None):
    return MediaRef(media_id, MEDIA_VIDEO, path, duration_sec=60.0,
                    width=1920, height=1080, fps=60, has_audio=True)


# ---- R1 作る側 -----------------------------------------------------------

class KeepSegmentsTest(unittest.TestCase):

    # 無音 [5,6) を挟んで 0.031 秒の有音区間ができる並び
    def _ranges(self):
        return [(0.0, 5.0), (5.0 + _SHORT_SEC, 10.0)]

    # T1: 既定は従来どおり 0.01 秒で切る (既存の呼び出しの挙動は変えない)
    def test_default_limit_is_unchanged(self):
        segments = silence_cutter.build_keep_segments(self._ranges(), 20.0)

        self.assertEqual(2, len(segments))
        self.assertAlmostEqual(_SHORT_SEC, segments[0][1] - segments[0][0], places=6)

    # T2: 下限を渡せば 0.031 秒の区間は落ちる
    def test_min_segment_sec_drops_the_short_one(self):
        segments = silence_cutter.build_keep_segments(
            self._ranges(), 20.0, min_segment_sec=0.05)

        self.assertEqual(1, len(segments))
        self.assertAlmostEqual(10.0, segments[0][0], places=6)

    # 下限の取り出し (設定が無ければ既定 0.05 / 0.01 より下へは行かない)
    def test_timeline_min_clip_sec(self):
        self.assertAlmostEqual(
            0.05, silence_cutter.timeline_min_clip_sec({}), places=6)
        self.assertAlmostEqual(
            0.2, silence_cutter.timeline_min_clip_sec(
                {"timeline": {"min_clip_sec": 0.2}}), places=6)
        self.assertAlmostEqual(
            0.01, silence_cutter.timeline_min_clip_sec(
                {"timeline": {"min_clip_sec": 0.0}}), places=6)


class DetectEditPointsTest(unittest.TestCase):

    class _Context:

        def __init__(self, settings):
            self.settings = settings

        def current_video_path(self):
            return "in.mp4"

    # T3: detect_edit_points は timeline.min_clip_sec を下限として使う
    def test_detect_edit_points_uses_min_clip_sec(self):
        context = self._Context({
            "silence_cut": {"enabled": True},
            "volume_analysis": {"last_cut_db": -30, "cut_min_silence_sec": 0.6},
            "ffmpeg": {},
            "timeline": {"min_clip_sec": 0.05},
        })
        ranges = [(0.0, 5.0), (5.0 + _SHORT_SEC, 10.0)]

        with mock.patch.object(silence_cutter.ffmpeg_runner, "probe_duration",
                               return_value=20.0), \
                mock.patch.object(silence_cutter, "detect_silence",
                                  return_value=ranges):
            keep, _meta = silence_cutter.detect_edit_points(context)

        self.assertEqual(1, len(keep), "0.031 秒の区間は編集点にしない")
        self.assertAlmostEqual(10.0, keep[0][0], places=6)


class ArchiveBuilderTest(unittest.TestCase):

    def setUp(self):
        self._real = timeline_builder.media_probe.probe
        timeline_builder.media_probe.probe = _probe

    def tearDown(self):
        timeline_builder.media_probe.probe = self._real

    def _prepared(self, keep):
        return [{
            "index": 1, "start": 100.0, "end": 130.0, "score": 90.0,
            "normalized_path": "clip1.mp4", "normalized_duration": 30.0,
            "keep_segments": keep,
            "items": [],
            "profile": {"width": 1920, "height": 1080, "orientation": "landscape"},
            "eff_cfg": {"enabled": True},
        }]

    # T4: detect_edit_points が下限を掛けた編集点なら、構築したものは検証を
    #     素通りする (作る側と検証側の下限が一致している)
    def test_built_timeline_loses_nothing_at_validate(self):
        keep = [(0.0, 5.0), (10.0, 15.0)]

        timeline = timeline_builder.build_archive_timeline(
            self._prepared(keep), _CLIP_SETTINGS, source_path="vod.mp4")

        before = {clip.id for track in timeline.tracks for clip in track.clips}
        project_io.validate(timeline, 0.05)
        after = {clip.id for track in timeline.tracks for clip in track.clips}

        self.assertEqual(before, after)

    # T5: builder は下限で区間を落とさない (落とすと字幕の軸がずれる / §4 R4)。
    #     古いプロジェクトの短い区間はクリップになり、検証側で消される。
    def test_builder_does_not_apply_the_floor(self):
        keep = [(0.0, 5.0), (6.0, 6.0 + _SHORT_SEC), (10.0, 15.0)]

        timeline = timeline_builder.build_archive_timeline(
            self._prepared(keep), _CLIP_SETTINGS, source_path="vod.mp4")

        clips = timeline.base_video_track().clips
        self.assertEqual(3, len(clips), "軸を詰めないため区間はそのまま載る")
        self.assertAlmostEqual(10.0 + _SHORT_SEC, timeline.duration_sec(), places=6)


class TimelineBuilderTest(unittest.TestCase):

    def setUp(self):
        self._real = timeline_builder_mod.media_probe.probe
        timeline_builder_mod.media_probe.probe = _probe

    def tearDown(self):
        timeline_builder_mod.media_probe.probe = self._real

    # T6: 1 本編集の builder も下限で区間を落とさない (§4 R4)。
    #     keep_segments は _append_subtitles の TimeMap にも渡るため。
    def test_builder_does_not_apply_the_floor(self):
        keep = [(0.0, 5.0), (6.0, 6.0 + _SHORT_SEC), (10.0, 15.0)]

        timeline = timeline_builder_mod.build(
            "in.mp4", "body.mp4", keep, [], _CLIP_SETTINGS)

        clips = timeline.base_video_track().clips
        self.assertEqual(3, len(clips))
        self.assertAlmostEqual(10.0 + _SHORT_SEC, timeline.duration_sec(), places=6)


# ---- R2 検証側 -----------------------------------------------------------

class InvalidClipIdsTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._dir.cleanup()

    # T7: 下限未満のクリップとそのリンク音声を返し、Timeline は変えない
    def test_reports_without_changing_the_timeline(self):
        timeline = _timeline(
            [_Scene(100.0, [(0.0, 10.0), (10.0, 10.0 + _SHORT_SEC)])],
            self._dir.name)
        video = timeline.base_video_track()
        short = video.clips[1]
        audio = timeline.audio_track_for(video.id)
        linked = next(c for c in audio.clips if c.link_clip == short.id)

        before = {clip.id for track in timeline.tracks for clip in track.clips}
        reported = project_io.invalid_clip_ids(timeline, 0.05)
        after = {clip.id for track in timeline.tracks for clip in track.clips}

        self.assertEqual({short.id, linked.id}, reported)
        self.assertEqual(before, after, "調べるだけで Timeline は変えない")

    # validate が実際に落とすものと一致している (規則が 2 つに分かれていない)
    def test_matches_what_validate_drops(self):
        timeline = _timeline(
            [_Scene(100.0, [(0.0, 10.0), (10.0, 10.0 + _SHORT_SEC)])],
            self._dir.name)

        reported = project_io.invalid_clip_ids(timeline, 0.05)
        before = {clip.id for track in timeline.tracks for clip in track.clips}
        project_io.validate(timeline, 0.05)
        after = {clip.id for track in timeline.tracks for clip in track.clips}

        self.assertEqual(reported, before - after)


class PreExistingInvalidTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._dir.cleanup()

    def _apply(self, timeline, plan, **kwargs):
        normalized, _ = pv.validate(plan, settings=_SETTINGS,
                                    exists=lambda path: True)

        return plan_applier.apply_plan(
            timeline, normalized, settings=_SETTINGS, probe=_fake_probe(),
            **kwargs)

    def _plan(self, scenes):
        plan = ps.new_plan("youtube_long", {"kind": "vod", "video_id": "1"},
                           duration_sec=7200.0)
        plan["scenes"] = scenes
        plan["subtitles"] = []
        plan["sounds"] = []

        return plan

    # T8: 元から下限未満のクリップがあっても AI 編集は失敗しない (本件の修正)
    #
    # 20261124 と同じ形にする。短いクリップは**リップルより前**に置く。
    # 後ろに置くとリップル自身が (min_clip_sec を見て) 先に消すため、
    # V15 の判定には届かず再現にならない。
    #   V1: c1 [0,10) c2 [10,10.031) ← 触らない / c3 [10.031,25.031) ← ここを詰める
    def test_a_pre_existing_short_clip_does_not_fail_the_plan(self):
        timeline = _timeline([_Scene(100.0, [(0.0, 10.0), (10.0, 10.0 + _SHORT_SEC)]),
                              _Scene(500.0, [(0.0, 15.0)])],
                             self._dir.name)
        short_id = timeline.base_video_track().clips[1].id
        plan = self._plan([
            # 1 本目は素材の端ぴったり = 詰めない
            {"id": "sc1", "source_start": 100.0, "source_end": 100.0 + 10.0 + _SHORT_SEC,
             "role": "body", "order": 1},
            # 2 本目だけ前後を詰める (リップルは短いクリップより後ろで起きる)
            {"id": "sc2", "source_start": 505.0, "source_end": 512.0,
             "role": "body", "order": 2},
        ])

        applied, report = self._apply(timeline, plan,
                                      scene_indexes={"sc1": 0, "sc2": 1})

        ids = {clip.id for clip in applied.base_clips(include_disabled=True)}
        self.assertNotIn(short_id, ids, "0.031 秒のクリップは検証で消える")
        self.assertEqual(2, len(ids), "残りのクリップは生きている")
        durations = sorted(clip.duration
                           for clip in applied.base_clips(include_disabled=True))
        self.assertAlmostEqual(7.0, durations[0], places=3,
                               msg="plan の編集 (505〜512s) が残っている")
        notes = [n for n in report.notes if n["where"] == "timeline.before"]
        self.assertEqual(1, len(notes), "元からの不正は記録に残る")

    # T9: plan が本当に壊した場合は従来どおり巻き戻す
    def test_a_loss_caused_by_the_plan_still_rolls_back(self):
        timeline = _timeline([_Scene(100.0, [(0.0, 10.0)])], self._dir.name)
        stack = CommandStack()
        plan = self._plan([{"id": "sc1", "source_start": 102.0,
                            "source_end": 108.0, "role": "body", "order": 1}])

        def breaking_validate(target, min_clip_sec=0.05):
            track = target.base_video_track()
            track.clips = track.clips[1:]

            return target

        with mock.patch.object(plan_applier.project_io, "validate",
                               breaking_validate):
            with self.assertRaises(AiPlanError):
                self._apply(timeline, plan, stack=stack)

        clips = timeline.base_clips(include_disabled=True)
        self.assertEqual(1, len(clips), "消したクリップも戻る")
        self.assertAlmostEqual(10.0, clips[0].duration, places=3)
        self.assertFalse(stack.can_undo())


if __name__ == "__main__":
    unittest.main()
