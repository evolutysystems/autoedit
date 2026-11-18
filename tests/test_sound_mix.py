# SE (効果音) のミックス (src/timeline/sound_mix.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver12/resolve1.md §9):
#   ・区間に重なる SE だけを、正しい素材位置・遅延・音量で拾うこと
#   ・amix へ normalize=0 を渡すこと (足すほど全体が小さくなるのを防ぐ)
#   ・出力の差し替えが映像をコピー (-c:v copy) で通すこと
#   ・SE が 1 件も無ければ ffmpeg を 1 回も起動しないこと
# FFmpeg は起動しない。組み立てたコマンドだけを検証する。
import os
import tempfile
import unittest

from src.timeline import sound_mix
from src.timeline.model import (
    MEDIA_AUDIO,
    TRACK_SOUND,
    TRACK_VIDEO,
    Clip,
    MediaRef,
    SoundClip,
    Timeline,
    Track,
)

_WORK = []


def setUpModule():
    _WORK.append(tempfile.mkdtemp(prefix="sound_mix_test_"))


def tearDownModule():
    import shutil
    for path in _WORK:
        shutil.rmtree(path, ignore_errors=True)


# 実在する音声ファイルの代わり (中身は見ない。存在チェックだけ通ればよい)
def _se_file(name="pon.wav"):
    path = os.path.join(_WORK[0], name)
    if not os.path.exists(path):
        with open(path, "wb") as handle:
            handle.write(b"\0")
    return path


# 本編 30 秒 + SE 2 件の Timeline
def _build(gain_db=-6.0):
    timeline = Timeline(fps=60)
    timeline.media_pool = [
        MediaRef("m1", "video", _se_file("body.mp4"), 30.0, 1920, 1080, 60, True),
        MediaRef("m2", MEDIA_AUDIO, _se_file(), 2.0, 0, 0, 0.0, True),
    ]
    timeline.tracks = [
        Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True,
              clips=[Clip("c1", "m1", 0.0, 30.0, 0.0, 30.0)]),
        Track("SE1", TRACK_SOUND, 1, name="SE 1", clips=[
            SoundClip("se1", "m2", 5.0, 2.0, gain_db=gain_db),
            SoundClip("se2", "m2", 20.0, 2.0),
        ]),
    ]
    return timeline


class CollectTest(unittest.TestCase):

    def test_collect_all(self):
        pieces = sound_mix.collect_pieces(_build())
        self.assertEqual([p["clip_id"] for p in pieces], ["se1", "se2"])
        self.assertAlmostEqual(pieces[0]["delay_sec"], 5.0)
        self.assertAlmostEqual(pieces[0]["duration"], 2.0)
        self.assertAlmostEqual(pieces[0]["source_in"], 0.0)
        self.assertAlmostEqual(pieces[0]["gain_db"], -6.0)

    def test_window_clips_and_offsets(self):
        # タイル [6, 10) に掛かるのは se1 の後半 1 秒だけ
        pieces = sound_mix.collect_pieces(_build(), 6.0, 10.0)
        self.assertEqual([p["clip_id"] for p in pieces], ["se1"])
        self.assertAlmostEqual(pieces[0]["source_in"], 1.0)
        self.assertAlmostEqual(pieces[0]["duration"], 1.0)
        self.assertAlmostEqual(pieces[0]["delay_sec"], 0.0)

    def test_window_delay_is_relative(self):
        pieces = sound_mix.collect_pieces(_build(), 4.0, 8.0)
        self.assertAlmostEqual(pieces[0]["delay_sec"], 1.0)

    def test_muted_and_disabled_are_skipped(self):
        timeline = _build()
        timeline.track_by_id("SE1").clips[0].muted = True
        timeline.track_by_id("SE1").clips[1].enabled = False
        self.assertEqual(sound_mix.collect_pieces(timeline), [])
        self.assertFalse(sound_mix.has_sound(timeline))

    def test_missing_media_is_skipped(self):
        timeline = _build()
        timeline.media_by_id("m2").path = os.path.join(_WORK[0], "nothere.wav")
        self.assertEqual(sound_mix.collect_pieces(timeline), [])

    def test_beyond_timeline_is_cut(self):
        # 本編 (30 秒) より後ろの SE は鳴らない (resolve1 §4.3)
        timeline = _build()
        timeline.track_by_id("SE1").clips.append(
            SoundClip("se9", "m2", 100.0, 2.0))
        self.assertEqual([p["clip_id"] for p in sound_mix.collect_pieces(timeline)],
                         ["se1", "se2"])


class FilterTest(unittest.TestCase):

    def test_input_chains(self):
        pieces = sound_mix.collect_pieces(_build())
        args, chains, labels = sound_mix.input_chains(pieces, 1, 48000)
        self.assertEqual(labels, ["[se0]", "[se1]"])
        # 入力は -ss / -t で素材側を切る
        self.assertIn("-ss", args)
        self.assertEqual(args[args.index("-i") + 1], pieces[0]["path"])
        # 音量と遅延が入る
        self.assertIn("volume=-6dB", chains[0])
        self.assertIn("adelay=5000:all=1", chains[0])
        # 音量 0 dB の SE には volume を付けない (無駄なフィルタを足さない)
        self.assertNotIn("volume=", chains[1])

    def test_amix_normalize_is_off_by_default(self):
        mix = sound_mix.amix_filter(["[0:a]", "[se0]"])
        self.assertIn("amix=inputs=2", mix)
        self.assertIn("normalize=0", mix)
        self.assertIn("duration=first", mix)

    def test_amix_single_label(self):
        self.assertIsNone(sound_mix.amix_filter(["[0:a]"]))


# ffmpeg を起動せず、組み立てたコマンドだけを控える差し替え
class _FakeRunner:

    def __init__(self):
        self.commands = []

    def execute(self, command, **_kwargs):
        self.commands.append(list(command))
        # 出力先 (コマンドの末尾) を空ファイルとして作る
        out = command[-1]
        with open(out, "wb") as handle:
            handle.write(b"\0")
        return 0

    @staticmethod
    def get_ffmpeg_exe(_cfg):
        return "ffmpeg"

    @staticmethod
    def get_progress_timeout_sec(_cfg):
        return 0

    @staticmethod
    def audio_bitrate_options(_cfg, audio_codec=None):
        return ["-b:a", "192k"]


class _Context:

    def __init__(self, work_dir):
        self.working_dir = work_dir
        self.settings = {"ffmpeg": {"audio_sample_rate": 48000, "audio_codec": "aac"}}
        self.cancel_cb = None

    def allocate_intermediate(self, filename):
        return os.path.join(self.working_dir, filename)


class ApplyTest(unittest.TestCase):

    def setUp(self):
        self.runner = _FakeRunner()
        self._real_runner = sound_mix.ffmpeg_runner
        self._real_has_audio = sound_mix._has_audio_stream
        sound_mix.ffmpeg_runner = self.runner
        sound_mix._has_audio_stream = lambda *_a, **_k: True
        self.context = _Context(_WORK[0])
        self.video = _se_file("rendered.mp4")

    def tearDown(self):
        sound_mix.ffmpeg_runner = self._real_runner
        sound_mix._has_audio_stream = self._real_has_audio

    def test_no_sound_runs_nothing(self):
        timeline = _build()
        timeline.tracks = [t for t in timeline.tracks if not t.is_sound()]
        self.assertEqual(
            sound_mix.apply(timeline, self.context, self.video), self.video)
        self.assertEqual(self.runner.commands, [])

    def test_apply_builds_two_passes(self):
        result = sound_mix.apply(_build(), self.context, self.video)
        self.assertNotEqual(result, self.video)
        self.assertEqual(len(self.runner.commands), 2)

        bed, merge = self.runner.commands
        # ① 無音を土台に SE を並べた wav
        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=48000",
                      " ".join(bed))
        self.assertIn("pcm_s16le", bed)
        self.assertIn("normalize=0", " ".join(bed))
        # ② 本編へ混ぜる: 映像はコピー / 音声だけ焼き直す
        self.assertIn("-c:v", merge)
        self.assertEqual(merge[merge.index("-c:v") + 1], "copy")
        self.assertIn("-shortest", merge)
        self.assertEqual(merge[merge.index("-c:a") + 1], "aac")
        self.assertIn("[0:a][1:a]amix=inputs=2", " ".join(merge))

    def test_batches_when_over_max_inputs(self):
        timeline = _build()
        track = timeline.track_by_id("SE1")
        track.clips = [SoundClip(f"se{i}", "m2", float(i), 0.5)
                       for i in range(1, 7)]
        sound_mix.apply(timeline, self.context, self.video,
                        cfg={"max_mix_inputs": 3, "mix_normalize": False})
        # 1 束 2 件 → 3 束 + 束の合流 + 本編への混ぜ込み = 5 回
        self.assertEqual(len(self.runner.commands), 5)

    def test_failure_keeps_original(self):
        def boom(*_args, **_kwargs):
            raise OSError("ffmpeg failed")
        self.runner.execute = boom
        self.assertEqual(
            sound_mix.apply(_build(), self.context, self.video), self.video)


if __name__ == "__main__":
    unittest.main()
