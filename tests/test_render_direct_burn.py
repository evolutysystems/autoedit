# ベース映像構築の省略 (src/timeline/renderer._direct_source) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/quality/resolve3.md §10 / R3b):
#   ・既定 (direct_burn=false) では**必ず None** = 従来経路 (§13-5 の判断)
#   ・7 条件すべてを満たすときだけ省略する。1 つでも外したら従来経路
#   ・入力引数が素材を -ss/-t で切る形になる
#   ・burn_subtitle は input_args を渡さなければ従来と 1 文字も変わらないコマンドを作る
# FFmpeg は起動しない。判定と組み立てだけを検証する。
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.modules import ffmpeg_runner, subtitle_generator
from src.timeline import renderer
from src.timeline.builder import timeline_config
from src.timeline.model import (
    AudioClip,
    Clip,
    MediaRef,
    ORIGIN_SILENCE_CUT,
    SubtitleClip,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    Timeline,
    Track,
)

_FPS = 60
_W, _H = 1920, 1080


class _Context:

    # renderer が見る最小限の PipelineContext 相当
    def __init__(self, settings):
        self.settings = settings
        self.blur_mask_path = None
        self.working_dir = os.path.dirname(__file__)


def _settings(direct_burn=True):
    return {
        "ffmpeg": {"output_width": _W, "output_height": _H, "output_fps": _FPS,
                   "preset": "medium", "crf": 20},
        "timeline": {"render": {"direct_burn": direct_burn}},
    }


def _media(media_id="m1", kind="video", has_audio=True):
    return MediaRef(media_id, kind, __file__, 100.0, _W, _H, _FPS, has_audio)


def _timeline(media=None, clips=1, subtitles=True, gap=0.0, muted=False,
              gain_db=0.0, with_audio_clip=True):
    media = media or _media()
    base_clips = []
    audio_clips = []
    start = 0.0
    for index in range(clips):
        base_clips.append(Clip(
            f"c{index}", media.id, start, 10.0, 12.3,
            origin={"type": ORIGIN_SILENCE_CUT, "segment_index": index}))
        if with_audio_clip:
            audio_clips.append(AudioClip(
                f"a{index}", f"c{index}", muted=muted, gain_db=gain_db))
        start += 10.0 + gap
    tracks = [
        Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=base_clips),
        Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
              clips=audio_clips),
    ]
    if subtitles:
        tracks.append(Track("T1", TRACK_SUBTITLE, 1, name="Subtitle 1", clips=[
            SubtitleClip("s1", 1.0, 2.0, text="字幕")]))
    return Timeline(fps=_FPS, width=_W, height=_H,
                    source={"media_id": media.id, "input_path": __file__,
                            "duration_sec": 100.0},
                    media_pool=[media], tracks=tracks)


def _direct(timeline, settings, context=None):
    context = context or _Context(settings)
    return renderer._direct_source(
        timeline, context, timeline_config(settings), settings["ffmpeg"])


class DirectBurnConditionTest(unittest.TestCase):

    # 既定 (false) では必ず従来経路 (§13-5)
    def test_disabled_by_default(self):
        self.assertIsNone(_direct(_timeline(), _settings(direct_burn=False)))

    # 条件をすべて満たすときだけ省く
    def test_single_clip_with_subtitles_is_direct(self):
        direct = _direct(_timeline(), _settings())
        self.assertIsNotNone(direct)
        self.assertEqual(direct["input_args"][:2], ["-ss", "12.300"])
        self.assertIn("-i", direct["input_args"])
        self.assertAlmostEqual(direct["duration"], 10.0, places=2)

    # 条件 1: クリップが 2 本以上なら従来経路 (A/V 同期対策の連結が要る)
    def test_two_clips_use_the_old_path(self):
        self.assertIsNone(_direct(_timeline(clips=2), _settings()))

    # 条件 2: ギャップがあれば従来経路 (黒+無音の挿入が要る)
    def test_gap_uses_the_old_path(self):
        self.assertIsNone(_direct(_timeline(clips=2, gap=1.0), _settings()))

    # 条件 3: 字幕が無ければ省く意味が無い (焼き込みが走らない)
    def test_without_subtitles_uses_the_old_path(self):
        self.assertIsNone(_direct(_timeline(subtitles=False), _settings()))

    # 条件 4: 静止画は無音生成の 2 入力構成になるため従来経路
    def test_image_media_uses_the_old_path(self):
        image = MediaRef("img1", "image", __file__, None, 800, 600, 0.0, False)
        self.assertIsNone(_direct(_timeline(media=image), _settings()))

    # 条件 4: 音声を持たない素材も従来経路
    def test_without_audio_uses_the_old_path(self):
        self.assertIsNone(_direct(_timeline(media=_media(has_audio=False)),
                                  _settings()))

    # 条件 4: 音声クリップが無い構成も従来経路
    def test_without_audio_clip_uses_the_old_path(self):
        self.assertIsNone(_direct(_timeline(with_audio_clip=False), _settings()))

    # 条件 4: ミュート指定も従来経路
    def test_muted_uses_the_old_path(self):
        self.assertIsNone(_direct(_timeline(muted=True), _settings()))

    # 条件 5: 音声ゲインが入っていれば従来経路
    def test_audio_gain_uses_the_old_path(self):
        self.assertIsNone(_direct(_timeline(gain_db=-3.0), _settings()))

    # 条件 6: ぼかしマスクがあれば従来経路 (pre_chains が競合する)
    def test_blur_mask_uses_the_old_path(self):
        settings = _settings()
        context = _Context(settings)
        context.blur_mask_path = "mask.mkv"
        self.assertIsNone(_direct(_timeline(), settings, context=context))


class BurnSubtitleInputArgsTest(unittest.TestCase):

    def _capture(self):
        calls = []
        self.addCleanup(setattr, ffmpeg_runner, "execute", ffmpeg_runner.execute)
        ffmpeg_runner.execute = (
            lambda cmd, **kwargs: calls.append(list(cmd)) or 0)
        return calls

    # input_args を渡さなければ従来どおり入力 1 本だけ (コマンドは不変)
    def test_without_input_args_reads_one_input(self):
        calls = self._capture()
        subtitle_generator.burn_subtitle(
            "base.mp4", "sub.ass", "out.mp4", {"output_fps": _FPS})
        cmd = calls[0]
        self.assertEqual(cmd.count("-i"), 1)
        self.assertEqual(cmd[cmd.index("-i") + 1], "base.mp4")
        self.assertNotIn("-ss", cmd)

    # input_args を渡すと素材を直接読み、区間も切る
    def test_input_args_replace_the_input(self):
        calls = self._capture()
        subtitle_generator.burn_subtitle(
            "base.mp4", "sub.ass", "out.mp4", {"output_fps": _FPS},
            input_args=["-ss", "12.300", "-i", "source.mp4", "-t", "10.000"])
        cmd = calls[0]
        self.assertEqual(cmd.count("-i"), 1)
        self.assertEqual(cmd[cmd.index("-i") + 1], "source.mp4")
        self.assertIn("-ss", cmd)
        self.assertNotIn("base.mp4", cmd)

    # pre_chains は従来どおり字幕より前へ入る (直結の正規化がここへ乗る)
    def test_pre_chains_come_before_the_subtitles(self):
        calls = self._capture()
        subtitle_generator.burn_subtitle(
            "base.mp4", "sub.ass", "out.mp4", {"output_fps": _FPS},
            pre_chains=["[0:v]scale=1920:1080[vpre]"])
        spec = calls[0][calls[0].index("-vf") + 1]
        self.assertTrue(spec.startswith("[0:v]scale=1920:1080[vpre];[vpre]fps="))


if __name__ == "__main__":
    unittest.main()
