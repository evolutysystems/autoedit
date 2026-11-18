# SE (効果音) が切り抜き (アーカイブ用) でも成立するか (ver12 resolve1 §20 D6)
# 実行: python -m unittest discover -s tests
# 重点:
#   ・クリップ用とアーカイブ用のどちらの Timeline でも SE トラックが用意されること
#   ・書き出しのためのクリップ分割 (split_by_clip) で SE が各クリップへ写ること
#     (写らないと切り抜きの出力だけ SE が消える)
#   ・クリップ境界をまたぐ SE が、両側で素材の続きとして鳴ること
#   ・使用しないクリップの範囲にある SE は書き出されないこと
# FFmpeg は起動しない。
import unittest

from src.archive import timeline_builder as archive_timeline
from src.timeline import commands
from src.timeline.model import (
    MEDIA_AUDIO,
    TRACK_AUDIO,
    TRACK_SOUND,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    AudioClip,
    Clip,
    MediaRef,
    SoundClip,
    Timeline,
    Track,
)

_CFG = {"enabled": True, "initial_tracks": 1, "track_id_prefix": "SE",
        "track_name_format": "SE {index}", "max_tracks": 4}


# clip1 (0〜6s) と clip2 (6〜12s) を持つアーカイブ用 Timeline
def _archive_timeline(sound_clips=()):
    timeline = Timeline(fps=60, width=1920, height=1080,
                        source={"archive": {"clips": []}})
    timeline.media_pool = [
        MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True),
        MediaRef("m2", MEDIA_AUDIO, __file__, 4.0, 0, 0, 0.0, True),
    ]
    timeline.tracks = [
        Track("V1", TRACK_VIDEO, 1, is_base=True, clips=[
            Clip("c1", "m1", 0.0, 6.0, 0.0, 6.0,
                 origin={"type": "silence_cut", "archive_clip_index": 1}),
            Clip("c2", "m1", 6.0, 6.0, 20.0, 26.0,
                 origin={"type": "silence_cut", "archive_clip_index": 2}),
        ]),
        Track("A1", TRACK_AUDIO, 1, link_track="V1",
              clips=[AudioClip("a1", "c1"), AudioClip("a2", "c2")]),
        Track("S1", TRACK_SUBTITLE, 1, clips=[]),
        Track("SE1", TRACK_SOUND, 1, name="SE 1", clips=list(sound_clips)),
    ]
    return timeline


class InitialTrackTest(unittest.TestCase):

    # クリップ用・アーカイブ用のどちらでも、開いた時点で SE トラックが並ぶ
    # (編集画面の __init__ が呼ぶ ensure_initial_sound_tracks と同じ経路)
    def test_both_kinds_get_a_sound_track(self):
        for timeline in (Timeline(fps=60), _archive_timeline()):
            timeline.tracks = [t for t in timeline.tracks if not t.is_sound()]
            commands.ensure_initial_sound_tracks(timeline, _CFG)
            self.assertEqual([t.id for t in timeline.sound_tracks()], ["SE1"])


class SplitByClipTest(unittest.TestCase):

    def test_sound_goes_to_its_clip(self):
        timeline = _archive_timeline([SoundClip("se1", "m2", 7.0, 1.0, gain_db=-6.0)])
        groups = dict(archive_timeline.split_by_clip(timeline))
        # clip1 には入らず、clip2 の中で 1.0 秒の位置になる
        self.assertEqual(groups[1].sound_clips(), [])
        carried = groups[2].sound_clips()
        self.assertEqual(len(carried), 1)
        self.assertAlmostEqual(carried[0].timeline_start, 1.0)
        self.assertAlmostEqual(carried[0].duration, 1.0)
        self.assertAlmostEqual(carried[0].gain_db, -6.0)

    def test_sound_across_the_boundary_is_trimmed_on_both_sides(self):
        # 5.5〜7.5s の SE (境界 6.0s をまたぐ)
        timeline = _archive_timeline([SoundClip("se1", "m2", 5.5, 2.0)])
        groups = dict(archive_timeline.split_by_clip(timeline))
        head = groups[1].sound_clips()[0]
        tail = groups[2].sound_clips()[0]
        # clip1 側は末尾 0.5 秒、clip2 側は先頭 1.5 秒
        self.assertAlmostEqual(head.timeline_start, 5.5)
        self.assertAlmostEqual(head.duration, 0.5)
        self.assertAlmostEqual(head.source_in, 0.0)
        self.assertAlmostEqual(tail.timeline_start, 0.0)
        self.assertAlmostEqual(tail.duration, 1.5)
        # 素材の続きから鳴る (切れ目で音が頭出しし直さない)
        self.assertAlmostEqual(tail.source_in, 0.5)

    def test_muted_sound_is_not_carried(self):
        timeline = _archive_timeline([SoundClip("se1", "m2", 7.0, 1.0, muted=True)])
        groups = dict(archive_timeline.split_by_clip(timeline))
        # ミュートは「鳴らす SE」に数えないが、配置自体は写してよい。
        # 出力側 (sound_mix) が鳴らさないため、どちらでも結果は同じ。
        self.assertEqual(groups[2].sound_clips(), [])

    def test_unused_clip_drops_its_sounds(self):
        timeline = _archive_timeline([SoundClip("se1", "m2", 7.0, 1.0)])
        timeline.clip_by_id("c2").enabled = False
        groups = dict(archive_timeline.split_by_clip(timeline))
        self.assertNotIn(2, groups)          # クリップごと書き出さない
        self.assertEqual(groups[1].sound_clips(), [])


if __name__ == "__main__":
    unittest.main()
