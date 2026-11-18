# SE (効果音) の編集操作 (ver12 resolve1 §6) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・追加の置き先 (空きトラック → 新規トラック → 上限) の規約
#   ・移動・トリム・分割・削除・リップルが映像クリップと同じ規約で効くこと
#   ・音量・ミュートが SE にも効き、A1 の音声クリップの挙動は変わらないこと
import os
import unittest

from src.timeline import clipboard, commands
from src.timeline.model import (
    MEDIA_AUDIO,
    ORIGIN_SILENCE_CUT,
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

_MIN = 0.05
_CFG = {
    "enabled": True,
    "initial_tracks": 1,
    "track_id_prefix": "SE",
    "track_name_format": "SE {index}",
    "max_tracks": 2,
    "default_gain_db": 0.0,
}


# 本編 1 クリップ (30 秒) + 空の SE トラック 1 本
def _build():
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 30.0, 0.0, 30.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
                  clips=[AudioClip("a1", "c1")])
    sound = Track("SE1", TRACK_SOUND, 1, name="SE 1")
    return Timeline(fps=60, media_pool=media, tracks=[video, audio, sound])


def _se_media(path="C:/se/pon.wav", duration=2.0):
    return MediaRef("", MEDIA_AUDIO, path, duration, 0, 0, 0.0, True)


# SE を 1 つ置いて、そのノードを返す
def _add(timeline, start=5.0, duration=2.0, path="C:/se/pon.wav", track_id=None):
    command = commands.AddSoundClip(
        _se_media(path, duration), start, duration, track_id=track_id, cfg=_CFG)
    command.apply(timeline)
    return command


class AddTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build()

    def test_add_places_on_sound_track(self):
        command = _add(self.timeline, 5.0, 2.0)
        clip = self.timeline.clip_by_id(command.created_clip_id)
        self.assertIsInstance(clip, SoundClip)
        self.assertEqual(command.created_track_id, "SE1")
        self.assertAlmostEqual(clip.timeline_start, 5.0)
        self.assertAlmostEqual(clip.duration, 2.0)
        self.assertAlmostEqual(clip.source_out, 2.0)
        # 素材がプールへ入り、映像トラックは増えていない
        self.assertEqual(len(self.timeline.media_pool), 2)
        self.assertEqual(len(self.timeline.video_tracks()), 1)

    def test_same_path_reuses_media(self):
        _add(self.timeline, 0.0, 2.0)
        _add(self.timeline, 10.0, 2.0)
        self.assertEqual(len(self.timeline.media_pool), 2)   # 元の 1 件 + SE 1 件

    def test_overlap_moves_to_new_track(self):
        _add(self.timeline, 5.0, 2.0)
        command = _add(self.timeline, 6.0, 2.0)              # 重なる位置
        self.assertEqual(command.created_track_id, "SE2")
        self.assertEqual(len(self.timeline.sound_tracks()), 2)

    def test_track_limit(self):
        _add(self.timeline, 5.0, 2.0)
        _add(self.timeline, 6.0, 2.0)                        # SE2 を作る
        command = _add(self.timeline, 6.5, 2.0)              # 上限 (2) で置けない
        self.assertIsNone(command.created_clip_id)
        self.assertEqual(command.failure, "no_track")
        self.assertEqual(len(self.timeline.sound_tracks()), 2)

    def test_zero_duration_is_rejected(self):
        command = commands.AddSoundClip(_se_media(duration=0.0), 1.0, 0.0, cfg=_CFG)
        self.assertFalse(command.apply(self.timeline))
        self.assertEqual(command.failure, "duration")

    def test_disabled_by_setting(self):
        cfg = {**_CFG, "enabled": False}
        command = commands.AddSoundClip(_se_media(), 1.0, 2.0, cfg=cfg)
        self.assertFalse(command.apply(self.timeline))
        self.assertEqual(command.failure, "disabled")

    def test_ensure_initial_tracks(self):
        timeline = Timeline(fps=60)
        created = commands.ensure_initial_sound_tracks(timeline, {**_CFG,
                                                                  "initial_tracks": 2})
        self.assertEqual([t.id for t in created], ["SE1", "SE2"])
        # 2 回呼んでも増えない
        self.assertEqual(
            commands.ensure_initial_sound_tracks(timeline, {**_CFG,
                                                            "initial_tracks": 2}), [])


class EditTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build()
        self.clip_id = _add(self.timeline, 5.0, 2.0).created_clip_id

    def _clip(self):
        return self.timeline.clip_by_id(self.clip_id)

    def test_move(self):
        self.assertTrue(
            commands.MoveClip(self.clip_id, 9.0, _MIN).apply(self.timeline))
        self.assertAlmostEqual(self._clip().timeline_start, 9.0)

    def test_move_avoids_overlap_on_same_track(self):
        other = _add(self.timeline, 20.0, 2.0, track_id="SE1").created_clip_id
        self.assertEqual(self.timeline.track_of_clip(other).id, "SE1")
        # 既存ノードへ重なる位置はぶつからない所まで寄せられる
        commands.MoveClip(self.clip_id, 19.5, _MIN).apply(self.timeline)
        moved = self._clip()
        self.assertFalse(moved.timeline_start < 22.0 - 1e-6
                         and moved.timeline_end > 20.0 + 1e-6)

    def test_trim_right_is_capped_by_source(self):
        # 素材 2.0 秒を超えて伸ばせない
        commands.TrimClip(self.clip_id, commands.EDGE_RIGHT, 20.0, _MIN).apply(
            self.timeline)
        self.assertAlmostEqual(self._clip().duration, 2.0)

    def test_trim_left_moves_source_in(self):
        commands.TrimClip(self.clip_id, commands.EDGE_LEFT, 5.5, _MIN).apply(
            self.timeline)
        clip = self._clip()
        self.assertAlmostEqual(clip.timeline_start, 5.5)
        self.assertAlmostEqual(clip.duration, 1.5)
        self.assertAlmostEqual(clip.source_in, 0.5)

    def test_split(self):
        self.assertTrue(
            commands.SplitClip(self.clip_id, 6.0, _MIN).apply(self.timeline))
        clips = sorted(self.timeline.track_by_id("SE1").clips,
                       key=lambda c: c.timeline_start)
        self.assertEqual(len(clips), 2)
        self.assertIsInstance(clips[1], SoundClip)
        self.assertAlmostEqual(clips[0].duration, 1.0)
        self.assertAlmostEqual(clips[1].timeline_start, 6.0)
        self.assertAlmostEqual(clips[1].source_in, 1.0)
        # 音量は分割後も引き継がれる
        self.assertAlmostEqual(clips[1].gain_db, clips[0].gain_db)

    def test_delete_plain(self):
        commands.DeleteClip([self.clip_id], ripple=False, min_clip_sec=_MIN).apply(
            self.timeline)
        self.assertIsNone(self._clip())
        # 本編は動かない
        self.assertAlmostEqual(
            self.timeline.clip_by_id("c1").timeline_start, 0.0)

    def test_ripple_delete_of_video_shifts_sound(self):
        # 本編の頭 5 秒をリップル削除すると SE も 5 秒ぶん前へ詰まる (§6.4)
        commands.ripple_remove_range(
            self.timeline, 0.0, 5.0,
            commands.ripple_target_tracks(
                self.timeline, self.timeline.base_video_track(), True),
            _MIN)
        self.assertAlmostEqual(self._clip().timeline_start, 0.0)

    def test_locked_track_blocks_edit(self):
        self.timeline.track_by_id("SE1").locked = True
        self.assertFalse(
            commands.MoveClip(self.clip_id, 9.0, _MIN).apply(self.timeline))


class GainTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build()
        self.clip_id = _add(self.timeline, 5.0, 2.0).created_clip_id

    def test_set_gain(self):
        self.assertTrue(
            commands.SetAudioGain(self.clip_id, -6.0).apply(self.timeline))
        self.assertAlmostEqual(
            self.timeline.clip_by_id(self.clip_id).gain_db, -6.0)
        # 同じ値は変化なし (履歴を汚さない)
        self.assertFalse(
            commands.SetAudioGain(self.clip_id, -6.0).apply(self.timeline))

    def test_set_muted(self):
        self.assertTrue(
            commands.SetAudioMuted(self.clip_id, True).apply(self.timeline))
        clip = self.timeline.clip_by_id(self.clip_id)
        self.assertTrue(clip.muted)
        self.assertFalse(clip.is_audible())

    def test_audio_clip_still_works(self):
        # A1 のリンク音声の挙動は変わっていない
        self.assertTrue(commands.SetAudioGain("a1", 3.0).apply(self.timeline))
        self.assertAlmostEqual(self.timeline.clip_by_id("a1").gain_db, 3.0)

    def test_overlay_commands_ignore_sound(self):
        # SE は映像を持たないため位置・大きさのコマンドは効かない
        self.assertFalse(
            commands.MoveOverlay(self.clip_id, 0.5, 0.5).apply(self.timeline))
        self.assertFalse(
            commands.ResizeOverlay(self.clip_id, 0.5).apply(self.timeline))
        self.assertFalse(
            commands.ChangeZOrder(self.clip_id, commands.LAYER_TOP).apply(
                self.timeline))


class TrackCommandTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build()

    def test_add_track(self):
        command = commands.AddSoundTrack(cfg=_CFG)
        self.assertTrue(command.apply(self.timeline))
        self.assertEqual(command.created_track_id, "SE2")
        # 上限 (2) に達したら作らない
        self.assertFalse(commands.AddSoundTrack(cfg=_CFG).apply(self.timeline))

    def test_remove_track(self):
        commands.AddSoundTrack(cfg=_CFG).apply(self.timeline)
        _add(self.timeline, 5.0, 2.0, track_id="SE2")
        command = commands.RemoveSoundTrack("SE2")
        self.assertTrue(command.apply(self.timeline))
        self.assertEqual(command.removed_clips, 1)
        self.assertEqual([t.id for t in self.timeline.sound_tracks()], ["SE1"])

    def test_last_track_is_kept(self):
        self.assertFalse(commands.RemoveSoundTrack("SE1").apply(self.timeline))


class ClipboardTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build()
        # 実在するファイルを指す素材でないと貼り付け側が弾く
        self.path = os.path.abspath(__file__)
        self.clip_id = _add(self.timeline, 5.0, 2.0, path=self.path).created_clip_id

    def test_copy_and_paste(self):
        payload = clipboard.build_payload(self.timeline, [self.clip_id])
        self.assertEqual(payload["items"][0]["kind"], clipboard.KIND_SOUND)
        # 音量・ミュートも控える
        commands.SetAudioGain(self.clip_id, -9.0).apply(self.timeline)
        payload = clipboard.build_payload(self.timeline, [self.clip_id])
        self.assertAlmostEqual(payload["items"][0]["audio"]["gain_db"], -9.0)

        command = commands.PasteClips(payload, 15.0, min_clip_sec=_MIN,
                                      sound_tracks_cfg=_CFG)
        self.assertTrue(command.apply(self.timeline))
        pasted = self.timeline.clip_by_id(command.created_clip_ids[0])
        self.assertIsInstance(pasted, SoundClip)
        self.assertEqual(self.timeline.track_of_clip(pasted.id).id, "SE1")
        self.assertAlmostEqual(pasted.timeline_start, 15.0)
        self.assertAlmostEqual(pasted.duration, 2.0)
        self.assertAlmostEqual(pasted.gain_db, -9.0)

    def test_paste_into_timeline_without_sound_track(self):
        payload = clipboard.build_payload(self.timeline, [self.clip_id])
        empty = _build()
        empty.tracks = [t for t in empty.tracks if not t.is_sound()]
        command = commands.PasteClips(payload, 1.0, min_clip_sec=_MIN,
                                      sound_tracks_cfg=_CFG)
        self.assertTrue(command.apply(empty))
        self.assertEqual([t.id for t in empty.sound_tracks()], ["SE1"])


if __name__ == "__main__":
    unittest.main()
