# SE (効果音) のプレビュー反映 (ver12 resolve1 §10 / §11) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・プロキシのタイル鍵が SE の有無・音量・ミュートで変わること
#     (ここが変わらないと、音量を変えても古いタイルが鳴り続ける)
#   ・タイルの ffmpeg コマンドへ SE が本編とは別の amix (normalize=0) で入ること
#   ・SE があるときは認識用音声を初回再生へ再利用しないこと (SE が抜けるため)
# FFmpeg は起動しない。
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.modules import subtitle_generator
from src.timeline import compositor, proxy_render
from src.timeline.audio_source import AudioChunkSource
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

_FPS = 60
_TILE_SEC = 4.0
_WORK = []


def setUpModule():
    _WORK.append(tempfile.mkdtemp(prefix="sound_preview_test_"))


def tearDownModule():
    for path in _WORK:
        shutil.rmtree(path, ignore_errors=True)
    _WORK.clear()


def _se_path():
    path = os.path.join(_WORK[0], "pon.wav")
    if not os.path.exists(path):
        with open(path, "wb") as handle:
            handle.write(b"\0")
    return path


def _timeline(with_sound=True):
    media = [
        MediaRef("m1", "video", __file__, 100.0, 1920, 1080, _FPS, True),
        MediaRef("m2", MEDIA_AUDIO, _se_path(), 2.0, 0, 0, 0.0, True),
    ]
    tracks = [
        Track("V1", TRACK_VIDEO, 1, is_base=True,
              clips=[Clip("c1", "m1", 0.0, 10.0, 0.0, 10.0)]),
        Track("A1", TRACK_AUDIO, 1, link_track="V1", clips=[AudioClip("a1", "c1")]),
    ]
    if with_sound:
        tracks.append(Track("SE1", TRACK_SOUND, 1, name="SE 1", clips=[
            SoundClip("se1", "m2", 1.0, 2.0, gain_db=-3.0),
        ]))
    return Timeline(fps=_FPS, width=1920, height=1080,
                    source={"media_id": "m1", "input_path": __file__},
                    media_pool=media, tracks=tracks)


def _settings():
    return {
        "ffmpeg": {"output_fps": _FPS, "audio_sample_rate": 48000},
        "silence_cut": {"fade_enabled": False},
        "subtitle": {}, "vertical": {}, "watermark": {}, "blur": {},
        "timeline": {"sound_tracks": {"mix_normalize": False}},
    }


def _env(timeline, settings):
    profile = compositor.CanvasProfile.proxy(timeline, settings["ffmpeg"], 0.5)
    eff_cfg = subtitle_generator.build_effective_subtitle_cfg({}, {}, None)
    return proxy_render.ProxyEnvironment(
        settings, profile,
        {"tile_sec": _TILE_SEC, "preset": "ultrafast", "crf": 28,
         "audio_bitrate": "96k"},
        _WORK[0], eff_cfg=eff_cfg,
        font_profile=subtitle_generator.build_font_profile(eff_cfg))


class TileKeyTest(unittest.TestCase):

    def setUp(self):
        self.settings = _settings()

    def _key(self, timeline, tile=0):
        return proxy_render.tile_key(timeline, _env(timeline, self.settings), tile)

    def test_sound_changes_key(self):
        self.assertNotEqual(self._key(_timeline(False)), self._key(_timeline(True)))

    def test_gain_changes_key(self):
        timeline = _timeline()
        before = self._key(timeline)
        timeline.clip_by_id("se1").gain_db = -12.0
        self.assertNotEqual(before, self._key(timeline))

    def test_mute_changes_key(self):
        timeline = _timeline()
        before = self._key(timeline)
        timeline.clip_by_id("se1").muted = True
        self.assertNotEqual(before, self._key(timeline))

    def test_other_tile_is_not_affected(self):
        # SE は 1〜3 秒。タイル 1 (4〜8 秒) の鍵は SE の変更で変わらない
        timeline = _timeline()
        before = self._key(timeline, tile=1)
        timeline.clip_by_id("se1").gain_db = -12.0
        self.assertEqual(before, self._key(timeline, tile=1))


class TileCommandTest(unittest.TestCase):

    def test_sound_is_mixed_into_tile(self):
        timeline = _timeline()
        settings = _settings()
        env = _env(timeline, settings)
        command = proxy_render.plan_tile(
            timeline, env, 0, os.path.join(_WORK[0], "tile.mp4"))
        text = " ".join(command)
        self.assertIn(_se_path(), command)
        self.assertIn("volume=-3dB", text)
        self.assertIn("adelay=1000:all=1", text)
        # 本編とは別の amix で、normalize=0 (足しても本編が小さくならない)
        self.assertIn("normalize=0", text)
        self.assertIn("[pseout]", text)

    def test_no_sound_keeps_command_unchanged(self):
        plain = _timeline(False)
        settings = _settings()
        command = proxy_render.plan_tile(
            plain, _env(plain, settings), 0, os.path.join(_WORK[0], "tile.mp4"))
        self.assertNotIn("pseout", " ".join(command))


class InitialAudioTest(unittest.TestCase):

    def test_initial_audio_is_not_reused_when_sound_exists(self):
        # 認識用音声には SE が入っていないため、再利用すると鳴らない
        asr_audio = os.path.join(_WORK[0], "asr.wav")
        with open(asr_audio, "wb") as handle:
            handle.write(b"\0")
        source = AudioChunkSource(_timeline(True), _settings(), _WORK[0], asr_audio)
        self.assertIsNone(source.cached(0.0, 5.0))
        # SE が無ければ従来どおり再利用する
        plain = AudioChunkSource(_timeline(False), _settings(), _WORK[0], asr_audio)
        self.assertEqual(plain.cached(0.0, 5.0), (asr_audio, 0.0))


if __name__ == "__main__":
    unittest.main()
