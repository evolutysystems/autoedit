# 縦動画の切り抜きの書き出し経路 (src/timeline/renderer.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve9.md §8.1 B):
#   ・指定が無ければ FFmpeg へ渡すコマンドが従来と**完全一致**すること (R15)
#   ・3 分岐それぞれで切り抜きのフィルタが入ること
#   ・切り抜きはフェードより前 = オーバーレイ・字幕より前であること (R13)
#   ・噛み合わない指定では従来の正規化へ戻り、書き出しが止まらないこと (§4-4)
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.pipeline.pipeline_context import PipelineContext
from src.timeline import crop, renderer
from src.timeline.model import (
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

_SETTINGS = {"ffmpeg": {}, "blur": {"enabled": False}}


# 縦キャンバス (portrait=True) / 横キャンバスの Timeline を作る
def _build_timeline(portrait=True):
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True)]
    width, height = (1080, 1920) if portrait else (1920, 1080)
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 5.0, 0.0, 5.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
                  clips=[AudioClip("a1", "c1")])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1")
    return Timeline(fps=60, width=width, height=height,
                    orientation="portrait" if portrait else "landscape",
                    source={"media_id": "m1", "input_path": __file__},
                    media_pool=media, tracks=[video, audio, subtitle])


def _with_crop(timeline, mode=crop.MODE_SINGLE, background=crop.BG_BLUR, frames=None):
    media = timeline.media_by_id("m1")
    if frames is None:
        frames = ([(420, 0, 1080, 1080)] if mode == crop.MODE_SINGLE
                  else [(0, 0, 1080, 640), (500, 0, 910, 1078)])
    crop.store(timeline, crop.make_layout(mode, frames, media, timeline.width,
                                          timeline.height, background))
    return timeline


# FFmpeg を起動せず、渡されたコマンドだけを控える
class _Harness:

    def __init__(self, settings=None):
        self.commands = []
        self._tmp = tempfile.TemporaryDirectory()
        self.context = PipelineContext(input_path=__file__, settings=settings or _SETTINGS,
                                       working_dir=self._tmp.name)
        self._originals = {}

    def __enter__(self):
        from src.modules import ffmpeg_runner, silence_cutter

        self._originals = {
            (ffmpeg_runner, "execute"): ffmpeg_runner.execute,
            (ffmpeg_runner, "probe_duration"): ffmpeg_runner.probe_duration,
            (silence_cutter, "concat_files"): silence_cutter.concat_files,
        }
        ffmpeg_runner.execute = self._execute
        ffmpeg_runner.probe_duration = lambda *_a, **_k: 5.0
        silence_cutter.concat_files = self._concat
        return self

    def __exit__(self, *_exc):
        for (module, name), original in self._originals.items():
            setattr(module, name, original)
        self._tmp.cleanup()
        return False

    def _execute(self, cmd, **_kwargs):
        self.commands.append(list(cmd))
        self._touch(cmd[-1])

    def _concat(self, _parts, output_path, *_args, **_kwargs):
        self.commands.append(["concat", output_path])
        self._touch(output_path)

    @staticmethod
    def _touch(path):
        if not path or path.startswith("-") or "=" in os.path.basename(path):
            return
        try:
            with open(path, "wb") as handle:
                handle.write(b"x")
        except OSError:
            pass

    # 映像フィルタの指定をすべて集める
    def filters(self):
        specs = []
        for cmd in self.commands:
            for flag in ("-vf", "-filter_complex"):
                if flag in cmd:
                    specs.append(cmd[cmd.index(flag) + 1])
        return specs

    # 一時ディレクトリのパスを伏せたコマンド (比較用)
    def normalized(self):
        root = self._tmp.name
        return [[str(arg).replace(root, "<work>") for arg in cmd] for cmd in self.commands]


def _render(timeline, settings=None):
    harness = _Harness(settings)
    with harness:
        renderer.render(timeline, harness.context)
    return harness


class NoCropTest(unittest.TestCase):

    # 切り抜きが無ければ、縦でも横でも従来どおりの正規化になること (R15)
    def test_commands_are_unchanged(self):
        for portrait in (False, True):
            with self.subTest(portrait=portrait):
                harness = _render(_build_timeline(portrait=portrait))
                joined = " ".join(" ".join(c) for c in harness.commands)

                self.assertNotIn("crop=", joined)
                self.assertNotIn("vstack", joined)
                self.assertNotIn("boxblur", joined)

    # 素材とキャンバスが一致する横プロジェクトでは、映像フィルタが 1 つも付かないこと
    # (従来どおり。切り抜きの分岐を足したせいで正規化が走り始めてはいけない / R15)
    def test_landscape_has_no_video_filter(self):
        harness = _render(_build_timeline(portrait=False))

        self.assertEqual(harness.filters(), [])

    # 縦キャンバスで切り抜きが無い場合は、従来どおりの正規化 (収めて余白) になること
    def test_portrait_without_crop_uses_the_normalize_chain(self):
        harness = _render(_build_timeline(portrait=True))
        specs = [s for s in harness.filters() if "scale=" in s]

        self.assertTrue(specs)
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=decrease", specs[0])
        self.assertNotIn("crop=", specs[0])


class CropTest(unittest.TestCase):

    def test_single_black(self):
        timeline = _with_crop(_build_timeline(), background=crop.BG_BLACK)
        specs = _render(timeline).filters()
        crops = [s for s in specs if "crop=" in s]

        self.assertTrue(crops, "切り抜きが入っていません")
        self.assertIn("crop=1080:1080:420:0", crops[0])
        self.assertIn("pad=1080:1920", crops[0])
        self.assertNotIn("boxblur", crops[0])

    def test_single_blur(self):
        timeline = _with_crop(_build_timeline(), background=crop.BG_BLUR)
        crops = [s for s in _render(timeline).filters() if "crop=" in s]

        self.assertTrue(crops)
        self.assertIn("boxblur", crops[0])
        self.assertIn("overlay=(W-w)/2:(H-h)/2", crops[0])

    def test_split(self):
        timeline = _with_crop(_build_timeline(), mode=crop.MODE_SPLIT)
        crops = [s for s in _render(timeline).filters() if "crop=" in s]

        self.assertTrue(crops)
        self.assertIn("vstack=inputs=2", crops[0])
        self.assertIn("scale=1080:640", crops[0])
        self.assertIn("scale=1080:1280", crops[0])

    # 切り抜きはフェードより前 (= 出力の下地に当たる / R13)
    def test_crop_comes_before_the_fade(self):
        timeline = _with_crop(_build_timeline(), background=crop.BG_BLACK)
        settings = dict(_SETTINGS, silence_cut={"fade_enabled": True, "fade_duration_sec": 0.2})
        crops = [s for s in _render(timeline, settings).filters() if "crop=" in s]

        self.assertTrue(crops)
        self.assertIn("fade=t=in", crops[0])
        self.assertLess(crops[0].index("crop="), crops[0].index("fade=t=in"))

    # ぼかしの強さは設定から読むこと
    def test_blur_strength_comes_from_the_settings(self):
        timeline = _with_crop(_build_timeline(), background=crop.BG_BLUR)
        settings = dict(_SETTINGS, vertical={"crop": {"blur_radius": 5, "blur_power": 1}})
        crops = [s for s in _render(timeline, settings).filters() if "crop=" in s]

        self.assertIn("boxblur=5:1", crops[0])


class BrokenLayoutTest(unittest.TestCase):

    # 枠が素材の外を指していたら従来の正規化へ戻し、書き出しを止めないこと (§4-4)
    def test_out_of_bounds_falls_back(self):
        timeline = _with_crop(_build_timeline(), frames=[(1800, 0, 1080, 1080)])
        harness = _render(timeline)
        joined = " ".join(" ".join(c) for c in harness.commands)

        self.assertNotIn("crop=", joined)
        self.assertTrue(any("scale=1080:1920" in s for s in harness.filters()))

    # 素材を差し替えて寸法が変わった場合も同じ
    def test_source_size_changed_falls_back(self):
        timeline = _with_crop(_build_timeline())
        media = timeline.media_by_id("m1")
        media.width, media.height = 1280, 720
        harness = _render(timeline)

        self.assertNotIn("crop=", " ".join(" ".join(c) for c in harness.commands))


if __name__ == "__main__":
    unittest.main()
