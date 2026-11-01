# 縦プロジェクトへのテロップトラックの引き継ぎ (ver9 resolve15 §3.1 / §3.2) の単体テスト
# 実行: python -m unittest discover -s tests
#
# 背景:
#   縦プロジェクトの生成はテロップトラックを S1 の 1 本へ潰していたため、
#   トラックの本数も、ヘッダーで決めた色 (ver9 resolve13) も渡っていなかった。
#   さらに S1 と S2 のテロップが同じトラックで重なり、開き直すときの
#   _validate_overlaps が後ろへ押し出して時刻まで壊れていた (§1.3)。
#
# 重点:
#   ・本数・名前・番号・有効/無効・色がそのまま渡ること (§3.1)
#   ・テロップが元のトラックへ入ること (§3.2)
#   ・空のトラックも本数として渡ること (R2)
#   ・開き直しても時刻がずれないこと (§1.3 の副作用が消えること)
#   ・carry_subtitle_tracks=false で従来どおり 1 本へ潰せること (§3.4)
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.timeline import crop, project_io, subtitle_style, vertical_builder
from src.timeline.model import (
    ORIGIN_SILENCE_CUT,
    ROLE_COMMENT,
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


# S1 (色なし) / S2 コメント / S3 サブ色 / S4 (空・無効) を持つ横 Timeline。
# クリップは隙間なく並ぶ (アーカイブ用と同じ形)。
def _timeline():
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 5.0, 10.0, 15.0, origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c2", "m1", 5.0, 4.0, 40.0, 44.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=[
        AudioClip("a1", "c1"), AudioClip("a2", "c2"),
    ])
    tracks = [video, audio]
    tracks.append(Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1", clips=[
        SubtitleClip("s1", 1.0, 2.0, "本人"),
    ]))
    tracks.append(Track("S2", TRACK_SUBTITLE, 2, name="Comment", clips=[
        SubtitleClip("s2", 1.2, 2.0, "コメント", role=ROLE_COMMENT),
    ]))
    tracks.append(Track("S3", TRACK_SUBTITLE, 3, name="Subtitle 3", color="#2ABCFF",
                        clips=[SubtitleClip("s3", 1.4, 2.0, "サブ色")]))
    tracks.append(Track("S4", TRACK_SUBTITLE, 4, name="Subtitle 4", color="#FFEE00",
                        enabled=False))
    source = {"media_id": "m1", "input_path": os.path.abspath(__file__),
              "archive": {"vod_path": "x.mp4"}}
    return Timeline(fps=60, width=1920, height=1080, source=source,
                    media_pool=media, tracks=tracks)


def _settings(carry=True):
    return {"vertical": {"crop": {"carry_subtitle_tracks": carry}}}


class CarryTracksTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _timeline()
        self.vertical, self.warnings = vertical_builder.build(
            self.timeline, ["c1", "c2"], None, _settings(), close_gaps=True)

    # 本数・番号・名前・有効/無効がそのまま渡ること (§3.1)
    def test_track_structure(self):
        tracks = sorted(self.vertical.subtitle_tracks(), key=lambda t: t.index)

        self.assertEqual([(t.id, t.index, t.name, t.enabled) for t in tracks],
                         [("S1", 1, "Subtitle 1", True),
                          ("S2", 2, "Comment", True),
                          ("S3", 3, "Subtitle 3", True),
                          ("S4", 4, "Subtitle 4", False)])

    # ヘッダーで決めた色が渡ること (R3 / §3.1)
    def test_track_colors(self):
        self.assertEqual(
            {t.id: t.color for t in self.vertical.subtitle_tracks()},
            {"S1": "", "S2": "", "S3": "#2ABCFF", "S4": "#FFEE00"})

    # 色の解決 (焼き込み・プレビュー・Resolve 出力が通る口) に載ること
    def test_track_color_map(self):
        self.assertEqual(subtitle_style.track_color_map(self.vertical),
                         {"s3": "#2ABCFF"})

    # テロップが元のトラックへ入ること (§3.2)
    def test_clips_keep_their_track(self):
        placed = {t.id: [c.text for c in t.clips]
                  for t in self.vertical.subtitle_tracks()}

        self.assertEqual(placed, {"S1": ["本人"], "S2": ["コメント"],
                                  "S3": ["サブ色"], "S4": []})

    # 役割はクリップのまま (トラックから振り直さない / §2.2)
    def test_roles_are_untouched(self):
        comment = self.vertical.track_by_id("S2").clips[0]

        self.assertEqual(comment.role, ROLE_COMMENT)

    # 同時刻に重ねたテロップが押し出されないこと (§1.3 の副作用)
    def test_overlapping_subtitles_keep_their_time(self):
        times = {t.id: [round(c.timeline_start, 3) for c in t.clips]
                 for t in self.vertical.subtitle_tracks()}

        self.assertEqual(times, {"S1": [1.0], "S2": [1.2], "S3": [1.4], "S4": []})

    # 保存して開き直しても時刻が変わらないこと
    def test_round_trip_keeps_time(self):
        dest = os.path.join(tempfile.mkdtemp(), "v.timeline.json")
        project_io.save(self.vertical, dest, generator="vertical", project_path=dest)
        loaded = project_io.load(dest)

        self.assertEqual(
            {t.id: [round(c.timeline_start, 3) for c in t.clips]
             for t in loaded.subtitle_tracks()},
            {"S1": [1.0], "S2": [1.2], "S3": [1.4], "S4": []})
        self.assertEqual(loaded.track_by_id("S3").color, "#2ABCFF")

    # locked は引き継がない (作りたてのプロジェクトで編集を止めない / §4 Q1)
    def test_locked_is_not_carried(self):
        self.timeline.track_by_id("S3").locked = True

        vertical, _warnings = vertical_builder.build(
            self.timeline, ["c1", "c2"], None, _settings(), close_gaps=True)

        self.assertFalse(vertical.track_by_id("S3").locked)

    # 元の Timeline は変わらないこと
    def test_source_timeline_is_untouched(self):
        self.assertEqual([t.id for t in self.timeline.subtitle_tracks()],
                         ["S1", "S2", "S3", "S4"])
        self.assertEqual([len(t.clips) for t in self.timeline.subtitle_tracks()],
                         [1, 1, 1, 0])


class EdgeCaseTest(unittest.TestCase):

    # 字幕トラックが 1 本も無くても S1 ができること (§3.3)
    def test_without_subtitle_tracks(self):
        timeline = _timeline()
        timeline.tracks = [t for t in timeline.tracks if not t.is_subtitle()]

        vertical, _warnings = vertical_builder.build(
            timeline, ["c1", "c2"], None, _settings(), close_gaps=True)

        tracks = vertical.subtitle_tracks()
        self.assertEqual([(t.id, t.index) for t in tracks], [("S1", 1)])
        self.assertEqual(tracks[0].clips, [])

    # carry_subtitle_tracks=false は従来どおり 1 本へ潰すこと (§3.4)
    def test_carry_disabled(self):
        vertical, _warnings = vertical_builder.build(
            _timeline(), ["c1", "c2"], None, _settings(carry=False), close_gaps=True)

        tracks = vertical.subtitle_tracks()
        self.assertEqual([t.id for t in tracks], ["S1"])
        self.assertEqual([c.text for c in tracks[0].clips],
                         ["本人", "コメント", "サブ色"])
        self.assertEqual(tracks[0].color, "")

    # 設定の既定は「引き継ぐ」であること
    def test_default_setting(self):
        self.assertTrue(crop.config({})["carry_subtitle_tracks"])
        self.assertFalse(
            crop.config({"vertical": {"crop": {"carry_subtitle_tracks": False}}})
            ["carry_subtitle_tracks"])


if __name__ == "__main__":
    unittest.main()
