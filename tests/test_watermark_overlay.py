# 透かしの焼き込み (src/modules/watermark_overlay.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve.md §5.3):
#   ・大きさと余白がキャンバス**幅**に対する比率で決まること (R8)
#   ・入れるかどうかはサーバーの応答由来のフラグだけで決まること (R9)
#   ・未適用のまま出力へ進まないこと (§4-2)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.modules import ffmpeg_runner, watermark_overlay


# watermark_required / watermark_applied だけを持つ最小のコンテキスト
class _Flags:

    def __init__(self, required=False, applied=False):
        self.watermark_required = required
        self.watermark_applied = applied


class ConfigTest(unittest.TestCase):

    def test_defaults_when_section_is_missing(self):
        cfg = watermark_overlay.config({})
        self.assertEqual(cfg["scale_ratio"], 0.25)
        self.assertEqual(cfg["opacity"], 0.75)
        self.assertEqual(cfg["position"], "bottom_right")
        self.assertEqual(cfg["margin_ratio"], 0.02)

    def test_values_are_clamped(self):
        cfg = watermark_overlay.config({"watermark": {"scale_ratio": 9.0, "opacity": -1.0}})
        self.assertEqual(cfg["scale_ratio"], 1.0)
        self.assertEqual(cfg["opacity"], 0.0)

    def test_unknown_position_falls_back(self):
        cfg = watermark_overlay.config({"watermark": {"position": "middle"}})
        self.assertEqual(cfg["position"], "bottom_right")

    def test_broken_values_fall_back(self):
        cfg = watermark_overlay.config({"watermark": {"scale_ratio": "大きめ"}})
        self.assertEqual(cfg["scale_ratio"], 0.25)


class GeometryTest(unittest.TestCase):

    # 横 1920 と縦 1080 で「キャンバス幅に対する比率」が同じになること (R8)
    def test_width_follows_the_canvas_width(self):
        cfg = watermark_overlay.config({})
        landscape, _margin = watermark_overlay._geometry(1920, cfg)
        portrait, _margin = watermark_overlay._geometry(1080, cfg)
        self.assertEqual(landscape, 480)
        self.assertEqual(portrait, 270)

    # yuv420p で扱えるよう幅は偶数にそろえること
    def test_width_is_even(self):
        cfg = watermark_overlay.config({"watermark": {"scale_ratio": 0.3333}})
        width, _margin = watermark_overlay._geometry(1001, cfg)
        self.assertEqual(width % 2, 0)

    def test_margin_follows_the_canvas_width(self):
        cfg = watermark_overlay.config({})
        _width, margin = watermark_overlay._geometry(1920, cfg)
        self.assertEqual(margin, 38)

    def test_position_expressions(self):
        self.assertEqual(watermark_overlay._overlay_expr("bottom_right", 38), "W-w-38:H-h-38")
        self.assertEqual(watermark_overlay._overlay_expr("top_left", 10), "10:10")


class IsRequiredTest(unittest.TestCase):

    # ポイント機能を通らない呼び出し (CLI / テスト) では False (§4-5)
    def test_plain_object_is_not_required(self):
        self.assertFalse(watermark_overlay.is_required(object()))

    def test_required_and_not_applied(self):
        self.assertTrue(watermark_overlay.is_required(_Flags(required=True)))

    def test_applied_is_not_required_again(self):
        self.assertFalse(watermark_overlay.is_required(_Flags(required=True, applied=True)))

    def test_not_required_even_if_unapplied(self):
        self.assertFalse(watermark_overlay.is_required(_Flags(required=False)))


class BuildChainTest(unittest.TestCase):

    def setUp(self):
        self.cfg = watermark_overlay.config({})

    def test_chain_scales_and_overlays(self):
        args, chains, out_label = watermark_overlay.build_chain(
            "[v3]", "[wmout]", 1920, 1080, self.cfg, 2)

        self.assertEqual(args[0], "-i")
        self.assertTrue(args[1].endswith("watermark.png"))
        self.assertEqual(out_label, "[wmout]")
        self.assertEqual(chains[0], "[2:v]scale=480:-1,format=rgba,"
                                    "colorchannelmixer=aa=0.750[wm]")
        self.assertEqual(chains[1], "[v3][wm]overlay=W-w-38:H-h-38[wmout]")

    # 素材が無い環境では黙って省く (出力自体は止めない / §4-3)
    def test_without_the_asset(self):
        original = watermark_overlay.asset_path
        watermark_overlay.asset_path = lambda: None
        try:
            args, chains, out_label = watermark_overlay.build_chain(
                "[v0]", "[wmout]", 1920, 1080, self.cfg, 1)
        finally:
            watermark_overlay.asset_path = original

        self.assertIsNone(args)
        self.assertEqual(chains, [])
        self.assertEqual(out_label, "[v0]", "素材が無いのにラベルを進めています")


# FFmpeg を起動せずコマンドだけ控える
class _FfmpegSpy:

    def __init__(self, test):
        self.test = test
        self.commands = []
        self._original = ffmpeg_runner.execute

    def __enter__(self):
        ffmpeg_runner.execute = self._execute
        return self

    def __exit__(self, *_exc):
        ffmpeg_runner.execute = self._original
        return False

    def _execute(self, command, **_kwargs):
        self.commands.append(list(command))


class ApplyTest(unittest.TestCase):

    def test_single_pass_copies_the_audio(self):
        cfg = watermark_overlay.config({})
        with _FfmpegSpy(self) as spy:
            result = watermark_overlay.apply(
                "in.mp4", "out.mp4", (1920, 1080), cfg, {})

        self.assertEqual(result, "out.mp4")
        command = spy.commands[0]
        self.assertEqual(command[-1], "out.mp4")
        # 音声は再エンコードしない (無音入力でも失敗しないよう任意扱い)
        self.assertIn("-c:a", command)
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertIn("0:a?", command)
        filter_complex = command[command.index("-filter_complex") + 1]
        self.assertIn("scale=480:-1", filter_complex)
        self.assertIn("overlay=W-w-38:H-h-38", filter_complex)

    def test_without_the_asset_returns_the_input(self):
        cfg = watermark_overlay.config({})
        original = watermark_overlay.asset_path
        watermark_overlay.asset_path = lambda: None
        try:
            with _FfmpegSpy(self) as spy:
                result = watermark_overlay.apply("in.mp4", "out.mp4", (1920, 1080), cfg, {})
        finally:
            watermark_overlay.asset_path = original

        self.assertEqual(result, "in.mp4")
        self.assertEqual(spy.commands, [], "素材が無いのに FFmpeg を起動しています")


# ensure_applied は PipelineContext の一部だけを使う
class _FakeContext:

    def __init__(self, required):
        self.settings = {"ffmpeg": {}}
        self.watermark_required = required
        self.watermark_applied = False

    def allocate_intermediate(self, name):
        return os.path.join("work", name)

    def progress_subcallback(self, _label):
        return lambda *_a, **_k: None


class EnsureAppliedTest(unittest.TestCase):

    def test_applies_once(self):
        context = _FakeContext(required=True)
        with _FfmpegSpy(self) as spy:
            first = watermark_overlay.ensure_applied(context, "base.mp4", (1920, 1080), {})
            second = watermark_overlay.ensure_applied(context, first, (1920, 1080), {})

        self.assertTrue(context.watermark_applied)
        self.assertEqual(len(spy.commands), 1, "2 回焼き込んでいます (濃さが変わります)")
        self.assertEqual(second, first)

    def test_does_nothing_when_not_required(self):
        context = _FakeContext(required=False)
        with _FfmpegSpy(self) as spy:
            result = watermark_overlay.ensure_applied(context, "base.mp4", (1920, 1080), {})

        self.assertEqual(result, "base.mp4")
        self.assertEqual(spy.commands, [])
        self.assertFalse(context.watermark_applied)


if __name__ == "__main__":
    unittest.main()
