# 無音カットで消えた区間を戻す / 切り出し原点の記録 (docs/request/ver13/resolve2.md)
# 実行: python -m unittest discover -s tests
#
# 要件 (docs/request/ver13/resolve2.md §6):
#   ・A 無音カットの穴を既存素材から戻す (取り直さない)
#   ・C cut_region の原点を実測して素材の VOD 原点として記録し、
#     セクション追加では要求位置より手前の余りを落とす
# ffmpeg も ffprobe も呼ばない (probe と measure は差し替える)。
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import timeline_builder as tb
from src.archive.timeline_builder import ORIGIN_ARCHIVE_INDEX
from src.timeline.model import (
    BASE_AUDIO_TRACK_ID,
    BASE_SUBTITLE_TRACK_ID,
    BASE_VIDEO_TRACK_ID,
    MEDIA_VIDEO,
    ORIGIN_SILENCE_CUT,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    AudioClip,
    Clip,
    MediaRef,
    Timeline,
    Track,
)

# 素材 m1 は VOD 100.0 から 60 秒ぶん。V1 には 3 区間だけ載っており、
# 残りの 3 箇所 (先頭・中間 2 つ) が無音カットで消えた穴である。
#
#   素材内:  0 ---- 5 ====== 15 ---- 20 ====== 30 ---- 40 ====== 50 ---- 60
#            ↑穴          ↑使用          ↑穴        ↑使用   ↑穴     ↑使用  ↑穴
_USED = [(5.0, 15.0), (20.0, 30.0), (40.0, 50.0)]
_MEDIA_VOD_START = 100.0
_MEDIA_DURATION = 60.0


def _timeline(used=None, media_vod_start=_MEDIA_VOD_START, with_media_row=True):
    timeline = Timeline(fps=60, width=1920, height=1080)
    video = Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, name="Video 1", is_base=True)
    audio = Track(BASE_AUDIO_TRACK_ID, TRACK_AUDIO, 1, name="Audio 1",
                  link_track=BASE_VIDEO_TRACK_ID)
    subtitle = Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1")
    timeline.tracks = [video, audio, subtitle]
    media = MediaRef("m1", MEDIA_VIDEO, "m1.mp4", duration_sec=_MEDIA_DURATION,
                     width=1920, height=1080, fps=60.0, has_audio=True)
    timeline.media_pool.append(media)

    cursor = 0.0
    for index, (start, end) in enumerate(used if used is not None else _USED):
        clip = Clip(timeline.next_id("c"), "m1", cursor, end - start,
                    source_in=start, source_out=end,
                    origin={"type": ORIGIN_SILENCE_CUT, ORIGIN_ARCHIVE_INDEX: 1,
                            "segment_index": index})
        video.clips.append(clip)
        audio.clips.append(AudioClip(timeline.next_id("a"), clip.id))
        cursor += end - start

    section = {"index": 1, "media_id": "m1", "vod_start": media_vod_start,
               "vod_end": media_vod_start + _MEDIA_DURATION}
    if not with_media_row:
        # 素材の復元情報をどこにも持たない状態 (archive_media_entry が
        # clips 側へも落ちるため、そちらの media_id も外す)
        section.pop("media_id")
    timeline.source = {"archive": {"clips": [section], "media": []}}
    if with_media_row:
        tb.put_archive_media(timeline, "m1", media_vod_start,
                             media_vod_start + _MEDIA_DURATION)

    return timeline


def _vod_ranges(timeline):
    return tb.used_vod_ranges(timeline, 1)


class PlanGapFillTest(unittest.TestCase):

    # T1: 先頭・中間・末尾の穴を素材内の秒で返す
    def test_all_gaps_are_found(self):
        fills = tb.plan_gap_fill(_timeline(), 0.0, 1000.0)
        self.assertEqual([(f["source_in"], f["source_out"]) for f in fills],
                         [(0.0, 5.0), (15.0, 20.0), (30.0, 40.0), (50.0, 60.0)])
        self.assertEqual([(f["vod_start"], f["vod_end"]) for f in fills],
                         [(100.0, 105.0), (115.0, 120.0), (130.0, 140.0), (150.0, 160.0)])
        # 隣のクリップの番号を継ぐ
        self.assertEqual({f["clip_index"] for f in fills}, {1})

    # T2: 指定範囲の外の穴は返さない / またがる穴は切り取る
    def test_gaps_are_clipped_to_the_request(self):
        fills = tb.plan_gap_fill(_timeline(), 118.0, 135.0)
        self.assertEqual([(f["vod_start"], f["vod_end"]) for f in fills],
                         [(118.0, 120.0), (130.0, 135.0)])

    def test_request_outside_any_gap_returns_nothing(self):
        # 105-115 は使用中 (穴ではない)
        self.assertEqual(tb.plan_gap_fill(_timeline(), 106.0, 114.0), [])

    # T3: 復元情報の無い素材は対象外
    def test_media_without_a_record_is_skipped(self):
        self.assertEqual(
            tb.plan_gap_fill(_timeline(with_media_row=False), 0.0, 1000.0), [])

    # 10 秒以下の穴も返す (used_gap_merge_sec の統合を掛けない / 要望 B)
    def test_short_gaps_are_offered(self):
        fills = tb.plan_gap_fill(_timeline(), 114.0, 121.0)
        self.assertEqual([(f["vod_start"], f["vod_end"]) for f in fills],
                         [(115.0, 120.0)])


class RestoreArchiveGapsTest(unittest.TestCase):

    def _apply(self, timeline, start, end):
        fills = tb.plan_gap_fill(timeline, start, end)
        command = tb.RestoreArchiveGaps(fills)
        return command, command.apply(timeline), fills

    # T4: V1 と A1 へクリップが足され、尺のぶんだけ後ろがずれる
    def test_video_and_audio_clips_are_added(self):
        timeline = _timeline()
        video = timeline.base_video_track()
        audio = timeline.audio_track_for(video.id)
        before_video, before_audio = len(video.clips), len(audio.clips)
        before_duration = timeline.duration_sec()

        _command, ok, fills = self._apply(timeline, 0.0, 1000.0)
        self.assertTrue(ok)
        self.assertEqual(len(video.clips), before_video + len(fills))
        self.assertEqual(len(audio.clips), before_audio + len(fills))
        added = sum(f["source_out"] - f["source_in"] for f in fills)
        self.assertAlmostEqual(timeline.duration_sec(), before_duration + added, places=3)

    # T5: 戻したあと、その素材の使用区間が連続になる
    def test_the_media_becomes_continuous(self):
        timeline = _timeline()
        self.assertEqual(len(_vod_ranges(timeline)), 3)     # 穴が 2 つある
        self._apply(timeline, 0.0, 1000.0)
        self.assertEqual(_vod_ranges(timeline),
                         [(_MEDIA_VOD_START, _MEDIA_VOD_START + _MEDIA_DURATION)])

    # 指定範囲だけ戻すと、その範囲だけが連続になる
    def test_partial_restore_fills_only_the_request(self):
        timeline = _timeline()
        self._apply(timeline, 115.0, 120.0)
        self.assertEqual(_vod_ranges(timeline),
                         [(105.0, 130.0), (140.0, 150.0)])

    # T6: 字幕は増えない (穴に認識結果が無いため)
    def test_no_subtitles_are_added(self):
        timeline = _timeline()
        subtitle = timeline.track_by_id(BASE_SUBTITLE_TRACK_ID)
        self._apply(timeline, 0.0, 1000.0)
        self.assertEqual(subtitle.clips, [])

    # 戻したクリップは V1 上で時系列どおりに並ぶ
    def test_restored_clips_are_in_order(self):
        timeline = _timeline()
        self._apply(timeline, 0.0, 1000.0)
        video = timeline.base_video_track()
        ordered = sorted(video.clips, key=lambda c: c.timeline_start)
        self.assertEqual([c.source_in for c in ordered],
                         [0.0, 5.0, 15.0, 20.0, 30.0, 40.0, 50.0])

    # 戻せる穴が無ければ何もしない
    def test_no_gaps_means_no_change(self):
        timeline = _timeline(used=[(0.0, _MEDIA_DURATION)])
        command = tb.RestoreArchiveGaps(tb.plan_gap_fill(timeline, 0.0, 1000.0))
        self.assertFalse(command.apply(timeline))


class MediaVodStartTest(unittest.TestCase):

    # T9: media 行が実測原点を使う。無ければ要求位置 (回帰)
    def test_helper_prefers_the_measured_origin(self):
        self.assertEqual(tb._media_vod_start({"start": 100.0,
                                              "media_vod_start": 98.3}), 98.3)
        self.assertEqual(tb._media_vod_start({"start": 100.0}), 100.0)
        self.assertEqual(tb._media_vod_start({}), 0.0)


class PrepareOneClipOriginTest(unittest.TestCase):

    # T8: prepare_one_clip が実測した原点を entry へ入れる
    def test_measured_origin_is_recorded(self):
        from src.archive import clip_writer

        clip = {"index": 0, "start": 100.0, "end": 110.0, "score": 0.0}
        settings = {"volume_analysis": {}, "vertical": {}, "subtitle": {},
                    "archive": {"clip_pipeline": {}}}
        with mock.patch.object(clip_writer, "cut_region", lambda *a, **k: a[3]), \
                mock.patch.object(clip_writer.cut_origin, "measure",
                                  return_value=98.3) as measured, \
                mock.patch.object(clip_writer.loudness_normalizer, "normalize_file",
                                  side_effect=lambda src, dest, *a, **k: src), \
                mock.patch.object(clip_writer, "_apply_volume_analysis",
                                  lambda *a, **k: None), \
                mock.patch.object(clip_writer.output_profile,
                                  "resolve_output_profile", return_value=None), \
                mock.patch.object(clip_writer.subtitle_generator,
                                  "build_effective_subtitle_cfg", return_value={}), \
                mock.patch.object(clip_writer, "_prepare_edit_points",
                                  return_value=([(0.0, 10.0)], [], 10.0)):
            import tempfile
            with tempfile.TemporaryDirectory() as workdir:
                entry = clip_writer.prepare_one_clip(
                    "vod.mp4", settings, settings, clip, {}, workdir,
                    use_timeline=True)
        # 素材の原点 = source_origin_sec (0) + 実測値
        self.assertEqual(entry["media_vod_start"], 98.3)
        measured.assert_called_once()


if __name__ == "__main__":
    unittest.main()
