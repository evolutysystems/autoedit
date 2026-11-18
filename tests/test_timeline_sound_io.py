# SE (効果音) の保存・復元 (ver12 resolve1 §12) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・位置・音量・ミュート・素材が往復すること
#   ・素材が欠落した SE は無効化され、読み込みそのものは止まらないこと
#   ・SE を持たない旧プロジェクトがそのまま読めること (後方互換)
#   ・リンク検証 (_validate_audio_links) が SE を巻き添えで消さないこと
import os
import shutil
import tempfile
import unittest

from src.timeline import project_io
from src.timeline.model import (
    MEDIA_AUDIO,
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


def _build(media_path, se_path):
    media = [
        MediaRef("m1", "video", media_path, 100.0, 1920, 1080, 60, True),
        MediaRef("m2", MEDIA_AUDIO, se_path, 2.0, 0, 0, 0.0, True),
    ]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 30.0, 0.0, 30.0, origin={"type": "silence_cut"}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
                  clips=[AudioClip("a1", "c1")])
    sound = Track("SE1", TRACK_SOUND, 1, name="SE 1", clips=[
        SoundClip("se1", "m2", 5.0, 2.0, source_in=0.25, source_out=2.25,
                  gain_db=-4.5, muted=False),
    ])
    return Timeline(
        fps=60, width=1920, height=1080,
        source={"input_path": media_path, "media_path": media_path,
                "media_id": "m1", "duration_sec": 100.0},
        media_pool=media, tracks=[video, audio, sound],
    )


class SoundIoTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="timeline_sound_io_test_")
        self.media_path = os.path.join(self._dir, "body.mp4")
        self.se_path = os.path.join(self._dir, "pon.wav")
        for path in (self.media_path, self.se_path):
            with open(path, "wb") as handle:
                handle.write(b"\0")
        self.timeline = _build(self.media_path, self.se_path)

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_round_trip(self):
        restored = project_io.from_json(project_io.to_json(self.timeline))
        track = restored.track_by_id("SE1")
        self.assertTrue(track.is_sound())
        clip = track.clips[0]
        self.assertIsInstance(clip, SoundClip)
        self.assertAlmostEqual(clip.timeline_start, 5.0)
        self.assertAlmostEqual(clip.duration, 2.0)
        self.assertAlmostEqual(clip.source_in, 0.25)
        self.assertAlmostEqual(clip.gain_db, -4.5)
        self.assertFalse(clip.muted)
        self.assertEqual(clip.media_id, "m2")

    def test_muted_round_trip(self):
        self.timeline.track_by_id("SE1").clips[0].muted = True
        restored = project_io.from_json(project_io.to_json(self.timeline))
        self.assertTrue(restored.track_by_id("SE1").clips[0].muted)

    def test_save_and_load_file(self):
        path = os.path.join(self._dir, "project.timeline.json")
        project_io.save(self.timeline, path)
        restored, _meta = project_io.load_project(path)
        self.assertAlmostEqual(restored.sound_clips()[0].gain_db, -4.5)

    def test_missing_media_disables_clip(self):
        # 素材が消えていても読み込みは成功し、該当 SE だけ無効になる
        os.remove(self.se_path)
        restored = project_io.from_json(project_io.to_json(self.timeline))
        clip = restored.track_by_id("SE1").clips[0]
        self.assertFalse(clip.enabled)
        self.assertEqual(restored.sound_clips(), [])
        # 本編は無事
        self.assertTrue(restored.clip_by_id("c1").enabled)

    def test_audio_link_validation_keeps_sound(self):
        # SE はリンクを持たないが、音声クリップの検証で消されない
        restored = project_io.from_json(project_io.to_json(self.timeline))
        self.assertEqual(len(restored.track_by_id("SE1").clips), 1)

    def test_old_project_without_sound_track(self):
        # SE トラックを持たない旧プロジェクトがそのまま読める
        self.timeline.tracks = [t for t in self.timeline.tracks if not t.is_sound()]
        restored = project_io.from_json(project_io.to_json(self.timeline))
        self.assertEqual(restored.sound_tracks(), [])
        self.assertEqual([t.id for t in restored.tracks], ["V1", "A1"])

    def test_overlap_is_corrected(self):
        # 同一 SE トラック内の重なりは後勝ちで詰められる (映像と同じ規約)
        track = self.timeline.track_by_id("SE1")
        track.clips.append(SoundClip("se2", "m2", 6.0, 2.0))
        restored = project_io.from_json(project_io.to_json(self.timeline))
        clips = sorted(restored.track_by_id("SE1").clips,
                       key=lambda c: c.timeline_start)
        self.assertGreaterEqual(clips[1].timeline_start,
                                clips[0].timeline_end - 1e-6)


if __name__ == "__main__":
    unittest.main()
