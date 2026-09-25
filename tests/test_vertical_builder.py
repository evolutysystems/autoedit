# 縦プロジェクトの生成 (src/timeline/vertical_builder.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve9.md §8.1 C):
#   ・選んだクリップが 0 秒起点へ詰まること (R7)
#   ・素材・音声・字幕が正しく引き継がれること (R8 / R9 / R10)
#   ・クリップ用プロジェクトとして開けること (R14)
#   ・元の Timeline を変更しないこと (§4-5)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.timeline import crop, project_io, vertical_builder
from src.timeline.model import (
    ORIGIN_SILENCE_CUT,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    AudioClip,
    Clip,
    MediaRef,
    SubtitleClip,
    Timeline,
    Track,
)


def _timeline(with_overlay=False, with_blur=False):
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True),
             MediaRef("m2", "image", __file__, None, 400, 400, 0, False)]
    # c1: 0.0-5.0 (素材 10.0 から) / c2: 8.0-12.0 (素材 40.0 から)
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 5.0, 10.0, 15.0, origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c2", "m1", 8.0, 4.0, 40.0, 44.0, origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c3", "m1", 12.0, 3.0, 60.0, 63.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=[
        AudioClip("a1", "c1", gain_db=3.0), AudioClip("a2", "c2", muted=True),
        AudioClip("a3", "c3"),
    ])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1", clips=[
        SubtitleClip("s1", 1.0, 2.0, "c1 の中"),
        SubtitleClip("s2", 6.0, 1.0, "すき間"),
        SubtitleClip("s3", 8.5, 5.0, "c2 からはみ出す"),
    ])
    tracks = [video, audio, subtitle]
    if with_overlay:
        tracks.insert(1, Track("V2", TRACK_VIDEO, 2, name="Video 2", clips=[
            Clip("o1", "m2", 1.0, 2.0, 0.0, 2.0, z_order=2),
        ]))

    source = {"media_id": "m1", "input_path": os.path.abspath(__file__),
              "media_path": os.path.abspath(__file__), "archive": {"vod_path": "x.mp4"}}
    if with_blur:
        source["blur"] = {"version": 5, "specs": []}
    return Timeline(fps=60, width=1920, height=1080, source=source,
                    media_pool=media, tracks=tracks)


def _layout(timeline):
    media = timeline.media_by_id("m1")
    return crop.make_layout(crop.MODE_SINGLE, [(420, 0, 1080, 1080)], media, 1080, 1920)


class BuildTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _timeline()
        self.vertical, self.warnings = vertical_builder.build(
            self.timeline, ["c1", "c2"], _layout(self.timeline), {}, close_gaps=True)

    # キャンバスが縦になること
    def test_canvas(self):
        self.assertEqual((self.vertical.width, self.vertical.height), (1080, 1920))
        self.assertEqual(self.vertical.orientation, "portrait")
        self.assertEqual(self.vertical.fps, 60)

    # 0 秒起点へ詰まり、素材の範囲は変わらないこと (R7)
    def test_clips_are_packed(self):
        clips = self.vertical.base_video_track().clips

        self.assertEqual([c.id for c in clips], ["c1", "c2"])
        self.assertEqual([c.timeline_start for c in clips], [0.0, 5.0])
        self.assertEqual([c.source_in for c in clips], [10.0, 40.0])
        self.assertEqual([c.duration for c in clips], [5.0, 4.0])

    # 空白を残す指定では間隔が保たれること
    def test_keep_gaps(self):
        vertical, _warnings = vertical_builder.build(
            self.timeline, ["c1", "c2"], _layout(self.timeline), {}, close_gaps=False)

        self.assertEqual([c.timeline_start for c in vertical.base_video_track().clips],
                         [0.0, 8.0])

    # リンク音声が引き継がれること (R9)
    def test_audio(self):
        audio = self.vertical.audio_tracks()[0].clips

        self.assertEqual([(a.link_clip, a.gain_db, a.muted) for a in audio],
                         [("c1", 3.0, False), ("c2", 0.0, True)])

    # 素材は使うものだけ (R8)
    def test_media_pool(self):
        self.assertEqual([m.id for m in self.vertical.media_pool], ["m1"])
        self.assertEqual(self.vertical.media_pool[0].path,
                         self.timeline.media_pool[0].path)

    # 重なる字幕が時刻シフトされ、はみ出しは切り詰められること (R10)
    def test_subtitles(self):
        subs = self.vertical.subtitle_tracks()[0].clips

        self.assertEqual([s.text for s in subs], ["c1 の中", "c2 からはみ出す"])
        self.assertEqual(subs[0].timeline_start, 1.0)
        # c2 は元 8.0 → 新 5.0。字幕は 8.5 開始なので 5.5 へ移り、c2 の終端 (12.0) で切れる
        self.assertAlmostEqual(subs[1].timeline_start, 5.5)
        self.assertAlmostEqual(subs[1].duration, 3.5)

    # 範囲外の字幕は引き継がず、知らせること
    def test_dropped_subtitle_is_reported(self):
        self.assertTrue(any("字幕" in text for text in self.warnings), self.warnings)

    # クリップ用プロジェクトとして開けること (R14)
    def test_project_kind_is_clip(self):
        self.assertEqual(project_io.project_kind(self.vertical), project_io.KIND_CLIP)
        self.assertNotIn("archive", self.vertical.source)
        self.assertEqual(self.vertical.source["input_path"],
                         self.timeline.source["input_path"])

    # 切り抜きの指定が入ること
    def test_crop_is_stored(self):
        layout = crop.load(self.vertical)

        self.assertIsNotNone(layout)
        self.assertEqual(layout["mode"], crop.MODE_SINGLE)

    # 元の Timeline を変更しないこと (§4-5)
    def test_source_timeline_untouched(self):
        self.assertEqual([c.timeline_start for c in self.timeline.base_video_track().clips],
                         [0.0, 8.0, 12.0])
        self.assertEqual(len(self.timeline.subtitle_tracks()[0].clips), 3)
        self.assertIn("archive", self.timeline.source)

    # 保存して読み戻せること
    def test_round_trip(self):
        restored = project_io.from_json(project_io.to_json(self.vertical))

        self.assertEqual(restored.width, 1080)
        self.assertEqual(len(restored.base_video_track().clips), 2)
        self.assertIsNotNone(crop.load(restored))


class WarningTest(unittest.TestCase):

    # オーバーレイは引き継がず、黙って落とさないこと (§3.6)
    def test_overlay_is_reported(self):
        timeline = _timeline(with_overlay=True)
        vertical, warnings = vertical_builder.build(
            timeline, ["c1"], _layout(timeline), {})

        self.assertEqual(len(vertical.video_tracks()), 1, "オーバーレイを引き継いでいます")
        self.assertTrue(any("オーバーレイ" in text for text in warnings), warnings)

    # ぼかしの指定も引き継がず、知らせること
    def test_blur_is_reported(self):
        timeline = _timeline(with_blur=True)
        vertical, warnings = vertical_builder.build(
            timeline, ["c1"], _layout(timeline), {})

        self.assertIsNone((vertical.source or {}).get("blur"))
        self.assertTrue(any("ぼかし" in text for text in warnings), warnings)


class TargetClipsTest(unittest.TestCase):

    # OP/ED と素材の無いクリップは対象外
    def test_filters(self):
        timeline = _timeline()
        clips = vertical_builder.target_clips(timeline, ["c2", "c1", "missing"])

        self.assertEqual([c.id for c in clips], ["c1", "c2"], "開始順に並んでいません")

    def test_empty_selection(self):
        timeline = _timeline()

        self.assertEqual(vertical_builder.target_clips(timeline, []), [])
        with self.assertRaises(ValueError):
            vertical_builder.build(timeline, [], _layout(timeline), {})


if __name__ == "__main__":
    unittest.main()
