# SE (効果音) のデータモデル (ver12 resolve1 §4) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・SoundClip が固有の時刻を持ち、辞書を往復しても値が変わらないこと
#   ・SE が Timeline の総尺を伸ばさないこと (§4.3)
#   ・既存の音声トラック (A1 / AudioClip) の扱いが一切変わらないこと
import unittest

from src.timeline.model import (
    MEDIA_AUDIO,
    ORIGIN_SILENCE_CUT,
    ORIGIN_USER_SE,
    TRACK_AUDIO,
    TRACK_SOUND,
    TRACK_VIDEO,
    AudioClip,
    Clip,
    MediaRef,
    SoundClip,
    Timeline,
    Track,
)


# V1 + A1 + SE1 を持つ Timeline を組む
def _build_timeline():
    timeline = Timeline(fps=60, width=1920, height=1080)
    timeline.media_pool = [
        MediaRef("m1", "video", "C:/work/body.mp4", 60.0, 1920, 1080, 60, True),
        MediaRef("m2", MEDIA_AUDIO, "C:/se/pon.wav", 1.5, 0, 0, 0.0, True),
    ]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 20.0, 0.0, 20.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
                  clips=[AudioClip("a1", "c1")])
    sound = Track("SE1", TRACK_SOUND, 1, name="SE 1", clips=[
        SoundClip("se1", "m2", 5.0, 1.5, gain_db=-3.0),
    ])
    timeline.tracks = [video, audio, sound]
    return timeline


class SoundClipTest(unittest.TestCase):

    def test_has_own_time(self):
        # AudioClip と違い、リンク先を見ずに自前の時刻を返す
        clip = SoundClip("se1", "m2", 5.0, 1.5)
        self.assertEqual(clip.timeline_start, 5.0)
        self.assertEqual(clip.duration, 1.5)
        self.assertEqual(clip.timeline_end, 6.5)
        self.assertEqual(clip.source_out, 1.5)
        self.assertTrue(clip.contains(5.5))
        self.assertFalse(clip.contains(6.5))

    def test_is_clip_subclass(self):
        # 移動・トリム・分割の既存コマンドが効くための前提 (resolve1 §2)
        self.assertIsInstance(SoundClip("se1", "m2", 0.0, 1.0), Clip)

    def test_default_origin(self):
        self.assertEqual(SoundClip("se1", "m2", 0.0, 1.0).origin_type(),
                         ORIGIN_USER_SE)

    def test_round_trip(self):
        clip = SoundClip("se1", "m2", 5.0, 1.5, source_in=0.25, source_out=1.75,
                         gain_db=-3.5, muted=True, enabled=False)
        restored = SoundClip.from_dict(clip.to_dict())
        self.assertEqual(restored.id, "se1")
        self.assertEqual(restored.media_id, "m2")
        self.assertAlmostEqual(restored.timeline_start, 5.0)
        self.assertAlmostEqual(restored.duration, 1.5)
        self.assertAlmostEqual(restored.source_in, 0.25)
        self.assertAlmostEqual(restored.source_out, 1.75)
        self.assertAlmostEqual(restored.gain_db, -3.5)
        self.assertTrue(restored.muted)
        self.assertFalse(restored.enabled)

    def test_copy_keeps_type_and_values(self):
        clip = SoundClip("se1", "m2", 5.0, 1.5, gain_db=6.0, muted=True)
        copied = clip.copy()
        self.assertIsInstance(copied, SoundClip)
        self.assertAlmostEqual(copied.gain_db, 6.0)
        self.assertTrue(copied.muted)
        copied.gain_db = 0.0
        self.assertAlmostEqual(clip.gain_db, 6.0)   # 控えは独立している

    def test_is_audible(self):
        self.assertTrue(SoundClip("se1", "m2", 0.0, 1.0).is_audible())
        self.assertFalse(SoundClip("se1", "m2", 0.0, 1.0, muted=True).is_audible())
        self.assertFalse(SoundClip("se1", "m2", 0.0, 1.0, enabled=False).is_audible())


class SoundTrackTest(unittest.TestCase):

    def test_track_kind(self):
        timeline = _build_timeline()
        track = timeline.track_by_id("SE1")
        self.assertTrue(track.is_sound())
        self.assertFalse(track.is_audio())
        self.assertFalse(track.is_video())
        # 音声トラックの一覧には入らない (A1 の扱いを変えない)
        self.assertEqual([t.id for t in timeline.audio_tracks()], ["A1"])
        self.assertEqual([t.id for t in timeline.sound_tracks()], ["SE1"])

    def test_track_round_trip(self):
        timeline = _build_timeline()
        restored = Track.from_dict(timeline.track_by_id("SE1").to_dict())
        self.assertTrue(restored.is_sound())
        self.assertIsInstance(restored.clips[0], SoundClip)
        self.assertAlmostEqual(restored.clips[0].gain_db, -3.0)

    def test_sound_clips_skips_silent_nodes(self):
        timeline = _build_timeline()
        track = timeline.track_by_id("SE1")
        track.clips.append(SoundClip("se2", "m2", 10.0, 1.0, muted=True))
        track.clips.append(SoundClip("se3", "m2", 12.0, 1.0, enabled=False))
        self.assertEqual([c.id for c in timeline.sound_clips()], ["se1"])
        self.assertEqual([c.id for c in timeline.sound_clips(only_audible=False)],
                         ["se1", "se2", "se3"])

    def test_disabled_track_is_silent(self):
        timeline = _build_timeline()
        timeline.track_by_id("SE1").enabled = False
        self.assertEqual(timeline.sound_clips(), [])

    def test_duration_is_not_extended_by_sound(self):
        # 本編より後ろへ置いた SE で総尺が伸びない (§4.3)
        timeline = _build_timeline()
        self.assertAlmostEqual(timeline.duration_sec(), 20.0)
        timeline.track_by_id("SE1").clips.append(
            SoundClip("se9", "m2", 100.0, 1.5))
        self.assertAlmostEqual(timeline.duration_sec(), 20.0)

    def test_next_sound_track_id(self):
        timeline = _build_timeline()
        self.assertEqual(timeline.next_sound_track_id(), ("SE2", 2))

    def test_used_media_ids_includes_sound(self):
        # 保存時の素材コピー・欠落復旧の対象に SE の素材も入る
        self.assertIn("m2", _build_timeline().used_media_ids())

    def test_sound_clip_is_not_overlay_element(self):
        # 合成 (映像オーバーレイ) には出てこない
        timeline = _build_timeline()
        self.assertEqual(
            [c.id for c in timeline.overlay_elements(include_disabled=True)], [])


class MediaRefAudioTest(unittest.TestCase):

    def test_is_audio(self):
        media = MediaRef("m2", MEDIA_AUDIO, "C:/se/pon.wav", 1.5)
        self.assertTrue(media.is_audio())
        self.assertFalse(media.is_image())
        # トリムの上限は素材尺 (画像のように無制限ではない)
        self.assertAlmostEqual(media.max_source_sec(), 1.5)


if __name__ == "__main__":
    unittest.main()
