# EditPlan の Timeline への適用の単体テスト (docs/AiEditor/resolve1.md §11 / §20)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・適用後の Timeline が project_io.validate を通る (V15)
#   ・**既存コマンド経由**であり、AI の 1 手も Undo で戻せる (§11.1)
#   ・VOD 時間 → 素材時間 → Timeline 時間の変換が無音カットを跨いでも合う (§9.2 / Z2)
#   ・使わないシーンはリップル削除で詰める (③)
#   ・端の調整はリップルトリムで行い、テロップも一緒に詰まる (④)
#   ・同じ場所に既存テロップがあれば直し、無ければ足す (⑤⑥ / §19 の 6)
#   ・SE は登録済みプリセットのファイルだけを置く (⑦)
#   ・壊れる plan は全て巻き戻して AiPlanError (V15)
# ffmpeg も ffprobe も呼ばない (素材は空ファイル / probe は差し替える)。
import os
import tempfile
import unittest
from unittest import mock

from src.ai import plan_applier, plan_schema as ps, plan_validator as pv
from src.archive.timeline_builder import ORIGIN_ARCHIVE_INDEX
from src.exceptions import AiPlanError
from src.timeline import project_io
from src.timeline.commands import CommandStack
from src.timeline.model import (BASE_AUDIO_TRACK_ID, BASE_SUBTITLE_TRACK_ID,
                                BASE_VIDEO_TRACK_ID, MEDIA_AUDIO, MEDIA_VIDEO,
                                ORIGIN_ASR, ORIGIN_SILENCE_CUT, TRACK_AUDIO,
                                TRACK_SUBTITLE, TRACK_VIDEO, AudioClip, Clip,
                                MediaRef, SubtitleClip, Timeline, Track)

_SETTINGS = {
    "timeline": {"min_clip_sec": 0.05,
                 "sound_tracks": {"enabled": True, "initial_tracks": 1,
                                  "max_tracks": 4, "default_gain_db": 0.0}},
    # このファイルは subtitle_mode = "full" (AI に本文も追加も任せる) を記録する。
    # key / none は tests/test_error_20261125c_telop.py が受け持つ。
    "ai": {"plan": {"youtube_long": {"min_sec": 0, "max_sec": 0, "max_scenes": 0},
                    "subtitle_mode": "full"},
           "sounds": {"enabled": True, "presets": {}}},
}


# 1 シーンぶんの素材の作り方。
#   vod_start : 素材の先頭が元 VOD の何秒か
#   segments  : 素材内の [(start, end), ...] (無音カット後に残した区間)
class _Scene:

    def __init__(self, vod_start, segments, texts=None):
        self.vod_start = float(vod_start)
        self.segments = [(float(a), float(b)) for a, b in segments]
        self.texts = list(texts or [])

    @property
    def vod_end(self):
        return self.vod_start + max(end for _, end in self.segments)


# アーカイブ Timeline を手で組む (build_archive_timeline と同じ形)。
# 素材は実ファイルとして作る (project_io.validate がクリップを無効化しないため)。
def _timeline(scenes, folder):
    timeline = Timeline(fps=60, width=1920, height=1080)
    video = Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, name="Video 1", is_base=True)
    audio = Track(BASE_AUDIO_TRACK_ID, TRACK_AUDIO, 1, name="Audio 1",
                  link_track=BASE_VIDEO_TRACK_ID)
    subtitle = Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1")
    timeline.tracks = [video, audio, subtitle]

    cursor = 0.0
    clips_meta = []
    media_meta = []
    for index, scene in enumerate(scenes):
        path = os.path.join(folder, "media{0}.mp4".format(index))
        with open(path, "wb") as handle:
            handle.write(b"0")
        media = MediaRef(timeline.next_id("m"), MEDIA_VIDEO, path,
                         duration_sec=scene.vod_end - scene.vod_start,
                         width=1920, height=1080, fps=60.0, has_audio=True)
        timeline.media_pool.append(media)

        clip_start = cursor
        for seg_index, (start, end) in enumerate(scene.segments):
            clip_id = timeline.next_id("c")
            video.clips.append(Clip(
                clip_id, media.id, cursor, end - start,
                source_in=start, source_out=end,
                origin={"type": ORIGIN_SILENCE_CUT,
                        ORIGIN_ARCHIVE_INDEX: index, "segment_index": seg_index}))
            audio.clips.append(AudioClip(timeline.next_id("a"), clip_id))
            cursor += end - start
        for text, start, duration in scene.texts:
            subtitle.clips.append(SubtitleClip(
                timeline.next_id("s"), clip_start + start, duration, text=text,
                origin={"type": ORIGIN_ASR, ORIGIN_ARCHIVE_INDEX: index}))

        clips_meta.append({"index": index, "vod_start": scene.vod_start,
                           "vod_end": scene.vod_end, "score": 1.0,
                           "timeline_start": clip_start, "timeline_end": cursor,
                           "media_id": media.id, "media_role": "normalized"})
        media_meta.append({"media_id": media.id, "vod_start": scene.vod_start,
                           "vod_end": scene.vod_end, "media_role": "normalized"})

    timeline.source = {"input_path": "vod.mp4", "duration_sec": cursor,
                       "archive": {"vod_path": "vod.mp4", "clips": clips_meta,
                                   "media": media_meta, "curve": []}}
    timeline.normalize()

    return timeline


def _plan(scenes, subtitles=None, sounds=None, target="youtube_long"):
    plan = ps.new_plan(target, {"kind": "vod", "video_id": "1"}, duration_sec=7200.0)
    plan["scenes"] = scenes
    plan["subtitles"] = subtitles or []
    plan["sounds"] = sounds or []

    return plan


def _scene(scene_id, start, end, order=1, **extra):
    scene = {"id": scene_id, "source_start": start, "source_end": end,
             "role": "body", "order": order}
    scene.update(extra)

    return scene


# 尺を持つ SE の素材 (ffprobe を呼ばないための差し替え)。
def _fake_probe(duration=1.5):
    def probe(path):
        return MediaRef("se", MEDIA_AUDIO, path, duration_sec=duration,
                        has_audio=True)

    return probe


class ApplierTestBase(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._dir.cleanup()

    def build(self, scenes):
        return _timeline(scenes, self._dir.name)

    def apply(self, timeline, plan, stack=None, **kwargs):
        normalized, _ = pv.validate(plan, settings=_SETTINGS,
                                    exists=lambda path: True)

        return plan_applier.apply_plan(
            timeline, normalized, stack=stack, settings=_SETTINGS,
            probe=kwargs.pop("probe", _fake_probe()), **kwargs)


class SceneApplyTest(ApplierTestBase):

    # 100〜160 の素材を 110〜150 へ詰める (前後をリップルトリム)。
    def test_edges_are_trimmed(self):
        timeline = self.build([_Scene(100.0, [(0.0, 60.0)])])
        plan = _plan([_scene("sc1", 110.0, 150.0)])

        applied, report = self.apply(timeline, plan)
        clips = applied.base_clips(include_disabled=True)

        self.assertEqual(1, len(clips))
        self.assertAlmostEqual(40.0, clips[0].duration, places=3)
        self.assertAlmostEqual(10.0, clips[0].source_in, places=3)
        self.assertAlmostEqual(0.0, clips[0].timeline_start, places=3)
        self.assertIn("A4", [n["check"] for n in report.notes])

    # plan が使わないシーンはリップル削除で消え、後ろが前へ詰まる。
    def test_unused_scene_is_ripple_deleted(self):
        timeline = self.build([_Scene(100.0, [(0.0, 10.0)]),
                               _Scene(500.0, [(0.0, 20.0)])])
        plan = _plan([_scene("sc1", 500.0, 520.0)])

        applied, report = self.apply(timeline, plan, scene_indexes={"sc1": 1})
        clips = applied.base_clips(include_disabled=True)

        self.assertEqual(1, len(clips))
        self.assertAlmostEqual(0.0, clips[0].timeline_start, places=3)
        self.assertAlmostEqual(20.0, clips[0].duration, places=3)
        self.assertIn("A3", [n["check"] for n in report.notes])

    def test_gain_is_applied_to_the_linked_audio(self):
        timeline = self.build([_Scene(100.0, [(0.0, 10.0)])])
        plan = _plan([_scene("sc1", 100.0, 110.0, gain_db=-6.0)])

        applied, _ = self.apply(timeline, plan)
        gains = [clip.gain_db for track in applied.tracks if track.is_audio()
                 for clip in track.clips]

        self.assertEqual([-6.0], gains)

    def test_zoom_is_applied_to_the_base_clip(self):
        timeline = self.build([_Scene(100.0, [(0.0, 10.0)])])
        plan = _plan([_scene("sc1", 100.0, 110.0,
                             zoom={"scale": 1.5, "x": 0.2, "y": 0.0})])

        applied, _ = self.apply(timeline, plan)
        clip = applied.base_clips(include_disabled=True)[0]

        self.assertAlmostEqual(1.5, clip.transform.scale, places=3)
        self.assertAlmostEqual(0.2, clip.transform.x, places=3)

    # 素材の記録が無いシーンは触れない (壊すより落とす)。他のシーンは通る。
    def test_scene_without_a_media_record_is_reported(self):
        timeline = self.build([_Scene(100.0, [(0.0, 10.0)])])
        plan = _plan([_scene("sc1", 100.0, 110.0, order=1),
                      _scene("sc2", 900.0, 910.0, order=2)])

        applied, report = self.apply(timeline, plan,
                                     scene_indexes={"sc1": 0, "sc2": 9})

        self.assertEqual(1, len(applied.base_clips(include_disabled=True)))
        self.assertIn("A4", [n["check"] for n in report.notes])


class TimeCoordinateTest(ApplierTestBase):

    # 無音カットを跨ぐ素材: 素材内 0〜10 と 20〜30 が残っている。
    # VOD 125 秒 (= 素材内 25 秒) は Timeline では 15 秒。
    def test_subtitle_lands_on_the_timeline_position(self):
        timeline = self.build([_Scene(100.0, [(0.0, 10.0), (20.0, 30.0)])])
        plan = _plan([_scene("sc1", 100.0, 130.0)],
                     subtitles=[{"scene_id": "sc1", "source_start": 125.0,
                                 "source_end": 127.0, "text": "後半のテロップ",
                                 "track_role": "own"}])

        applied, _ = self.apply(timeline, plan)
        added = [clip for clip in applied.subtitle_clips(only_used=False)
                 if clip.text == "後半のテロップ"]

        self.assertEqual(1, len(added))
        self.assertAlmostEqual(15.0, added[0].timeline_start, places=3)
        self.assertAlmostEqual(2.0, added[0].duration, places=3)

    # カットで消えた区間 (素材内 10〜20) のテロップは置き場所が無い。
    def test_subtitle_inside_a_cut_is_dropped(self):
        timeline = self.build([_Scene(100.0, [(0.0, 10.0), (20.0, 30.0)])])
        plan = _plan([_scene("sc1", 100.0, 130.0)],
                     subtitles=[{"scene_id": "sc1", "source_start": 112.0,
                                 "source_end": 114.0, "text": "消えた区間",
                                 "track_role": "own"}])

        applied, report = self.apply(timeline, plan)

        self.assertEqual([], [clip for clip in applied.subtitle_clips(only_used=False)
                              if clip.text == "消えた区間"])
        self.assertIn("A5", [n["check"] for n in report.notes])

    # SE はシーン先頭からの相対秒。カットを跨いだ先でも Timeline 時刻へ写る。
    def test_sound_offset_follows_the_time_map(self):
        timeline = self.build([_Scene(100.0, [(0.0, 10.0), (20.0, 30.0)])])
        path = os.path.join(self._dir.name, "se.wav")
        with open(path, "wb") as handle:
            handle.write(b"0")
        settings = {**_SETTINGS,
                    "ai": {"plan": _SETTINGS["ai"]["plan"],
                           "sounds": {"enabled": True, "presets": {"surprise": path}}}}
        plan = _plan([_scene("sc1", 100.0, 130.0)],
                     sounds=[{"scene_id": "sc1", "offset_sec": 25.0,
                              "preset": "surprise", "gain_db": -3.0}])
        normalized, _ = pv.validate(plan, settings=settings,
                                    exists=lambda value: True)

        applied, _ = plan_applier.apply_plan(timeline, normalized,
                                             settings=settings,
                                             probe=_fake_probe(1.5))
        sounds = [clip for track in applied.sound_tracks() for clip in track.clips]

        self.assertEqual(1, len(sounds))
        self.assertAlmostEqual(15.0, sounds[0].timeline_start, places=3)
        self.assertAlmostEqual(-3.0, sounds[0].gain_db, places=3)

    def test_sound_without_a_duration_is_dropped(self):
        timeline = self.build([_Scene(100.0, [(0.0, 10.0)])])
        path = os.path.join(self._dir.name, "se.wav")
        with open(path, "wb") as handle:
            handle.write(b"0")
        settings = {**_SETTINGS,
                    "ai": {"plan": _SETTINGS["ai"]["plan"],
                           "sounds": {"enabled": True, "presets": {"surprise": path}}}}
        plan = _plan([_scene("sc1", 100.0, 110.0)],
                     sounds=[{"scene_id": "sc1", "offset_sec": 1.0,
                              "preset": "surprise"}])
        normalized, _ = pv.validate(plan, settings=settings,
                                    exists=lambda value: True)

        applied, report = plan_applier.apply_plan(timeline, normalized,
                                                  settings=settings,
                                                  probe=_fake_probe(0.0))

        self.assertEqual([], [clip for track in applied.sound_tracks()
                              for clip in track.clips])
        self.assertIn("A7", [n["check"] for n in report.notes])


class SubtitleApplyTest(ApplierTestBase):

    # 同じ場所に認識由来のテロップがあれば、本文ではなく見た目を直す。
    def test_existing_subtitle_is_edited(self):
        timeline = self.build([_Scene(100.0, [(0.0, 10.0)],
                                      texts=[("もとの文字起こし", 1.0, 2.0)])])
        plan = _plan([_scene("sc1", 100.0, 110.0)],
                     subtitles=[{"scene_id": "sc1", "source_start": 101.0,
                                 "source_end": 103.0, "text": "もとの文字起こし",
                                 "track_role": "own", "animation": "tsukkomi",
                                 "fade_in": True}])

        applied, _ = self.apply(timeline, plan)
        clips = applied.subtitle_clips(only_used=False)

        self.assertEqual(1, len(clips), "足さずに直す")
        self.assertEqual("tsukkomi", clips[0].animation)
        self.assertTrue(clips[0].fade_in)

    def test_new_subtitle_is_added(self):
        timeline = self.build([_Scene(100.0, [(0.0, 10.0)])])
        plan = _plan([_scene("sc1", 100.0, 110.0)],
                     subtitles=[{"scene_id": "sc1", "source_start": 105.0,
                                 "source_end": 107.0, "text": "AI が足した一言",
                                 "track_role": "own", "animation": "shake"}])

        applied, report = self.apply(timeline, plan)
        clips = applied.subtitle_clips(only_used=False)

        self.assertEqual(1, len(clips))
        self.assertEqual("AI が足した一言", clips[0].text)
        self.assertEqual("shake", clips[0].animation)
        self.assertIn("A5", [n["check"] for n in report.notes])

    # コメント役割は専用トラック (S2) へ移る。
    def test_comment_role_moves_to_its_track(self):
        timeline = self.build([_Scene(100.0, [(0.0, 10.0)])])
        plan = _plan([_scene("sc1", 100.0, 110.0)],
                     subtitles=[{"scene_id": "sc1", "source_start": 105.0,
                                 "source_end": 107.0, "text": "コメント",
                                 "track_role": "comment"}])

        applied, _ = self.apply(timeline, plan)
        clips = applied.subtitle_clips(only_used=False)

        self.assertEqual("comment", clips[0].role)
        self.assertEqual("", clips[0].animation, "コメントに動きは付かない")


class UndoTest(ApplierTestBase):

    # AI の編集も既存の履歴に乗る = 1 手ずつ戻せる (§11.1)。
    def test_every_edit_can_be_undone(self):
        timeline = self.build([_Scene(100.0, [(0.0, 60.0)])])
        before = [(clip.id, clip.timeline_start, clip.duration, clip.source_in)
                  for clip in timeline.base_clips(include_disabled=True)]
        stack = CommandStack()
        plan = _plan([_scene("sc1", 110.0, 150.0)],
                     subtitles=[{"scene_id": "sc1", "source_start": 120.0,
                                 "source_end": 122.0, "text": "足した",
                                 "track_role": "own"}])

        applied, _ = self.apply(timeline, plan, stack=stack)

        self.assertTrue(stack.can_undo())
        while stack.can_undo():
            stack.undo(applied)

        after = [(clip.id, clip.timeline_start, clip.duration, clip.source_in)
                 for clip in applied.base_clips(include_disabled=True)]
        self.assertEqual(before, after, "Undo で適用前へ戻る")
        self.assertEqual([], applied.subtitle_clips(only_used=False))

    def test_one_step_at_a_time(self):
        timeline = self.build([_Scene(100.0, [(0.0, 60.0)])])
        stack = CommandStack()
        plan = _plan([_scene("sc1", 100.0, 160.0)],
                     subtitles=[{"scene_id": "sc1", "source_start": 120.0,
                                 "source_end": 122.0, "text": "足した",
                                 "track_role": "own"}])

        applied, _ = self.apply(timeline, plan, stack=stack)
        self.assertEqual(1, len(applied.subtitle_clips(only_used=False)))

        stack.undo(applied)

        self.assertTrue(stack.can_redo())


class ValidateTest(ApplierTestBase):

    def test_applied_timeline_passes_validate(self):
        timeline = self.build([_Scene(100.0, [(0.0, 10.0), (20.0, 30.0)]),
                               _Scene(500.0, [(0.0, 15.0)])])
        plan = _plan([_scene("sc1", 505.0, 512.0, order=1),
                      _scene("sc2", 100.0, 125.0, order=2)])

        applied, _ = self.apply(timeline, plan,
                                scene_indexes={"sc1": 1, "sc2": 0})

        # validate は「直して続ける」実装であり、通れば何も消えない。
        before = {clip.id for track in applied.tracks for clip in track.clips}
        project_io.validate(applied, 0.05)
        after = {clip.id for track in applied.tracks for clip in track.clips}

        self.assertEqual(before, after)

    # validate がクリップを落としたら = 壊れた形になったら、積んだ手を全て戻す。
    # project_io.validate は「直して続ける」実装で例外を投げないため、
    # 「消えた」状況を作って巻き戻しの経路そのものを見る。
    def test_a_loss_at_validation_rolls_back(self):
        timeline = self.build([_Scene(100.0, [(0.0, 10.0)])])
        stack = CommandStack()
        plan = _plan([_scene("sc1", 102.0, 108.0)])

        def breaking_validate(target, min_clip_sec=0.05):
            track = target.base_video_track()
            track.clips = track.clips[1:]

            return target

        with mock.patch.object(plan_applier.project_io, "validate",
                               breaking_validate):
            with self.assertRaises(AiPlanError):
                self.apply(timeline, plan, stack=stack)

        clips = timeline.base_clips(include_disabled=True)
        self.assertEqual(1, len(clips), "消したクリップも戻る")
        self.assertAlmostEqual(10.0, clips[0].duration, places=3,
                               msg="適用前の尺へ戻っている")
        self.assertFalse(stack.can_undo(), "巻き戻し後に履歴は残らない")

    def test_validation_can_be_skipped(self):
        timeline = self.build([_Scene(100.0, [(0.0, 10.0)])])
        plan = _plan([_scene("sc1", 102.0, 108.0)])

        applied, _ = self.apply(timeline, plan, validate=False)

        self.assertAlmostEqual(6.0, applied.base_clips(include_disabled=True)[0].duration,
                               places=3)


if __name__ == "__main__":
    unittest.main()
