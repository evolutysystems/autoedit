# 出力経路への透かしの差し込み (ver5 resolve.md §5.4) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・未ログイン / ポイント機能を通らない呼び出しでは、コマンドが従来と 1 文字も変わらないこと (§4-5)
#   ・合成が走る経路では**混ぜ込み** (再エンコードを増やさない)、走らない経路では単独パスで入ること (§3.1)
#   ・透かしはチェーンの**最後** = 字幕やオーバーレイの上に重なること
#   ・2 度焼きしないこと (濃さが変わるため)
#   ・成果物ができたときだけ commit し、失敗・中断では cancel すること (R3 / R6)
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.exceptions import PipelineCancelled
from src.modules import output_writer, watermark_overlay
from src.pipeline import pipeline_runner
from src.pipeline.pipeline_context import PipelineContext
from src.services.points import Reservation
from src.timeline import renderer
from src.timeline.model import (
    ORIGIN_SILENCE_CUT,
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


# with_subtitle / with_overlay で renderer の 3 分岐を撃ち分ける
def _build_timeline(with_subtitle=False, with_overlay=False):
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True)]
    tracks = [Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 5.0, 0.0, 5.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])]
    if with_overlay:
        media.append(MediaRef("m2", "video", __file__, 100.0, 1920, 1080, 60, False))
        tracks.append(Track("V2", TRACK_VIDEO, 2, name="Video 2", clips=[
            Clip("c2", "m2", 0.0, 2.0, 0.0, 2.0, z_order=2),
        ]))
    tracks.append(Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=[
        AudioClip("a1", "c1"),
    ]))
    clips = [SubtitleClip("s1", 1.0, 2.0, "テスト字幕")] if with_subtitle else []
    tracks.append(Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1", clips=clips))
    return Timeline(fps=60, width=1920, height=1080,
                    source={"media_id": "m1", "input_path": __file__},
                    media_pool=media, tracks=tracks)


# FFmpeg を実際には起動せず、渡されたコマンドだけを控える
class _RendererHarness:

    def __init__(self, settings, watermark_required=False):
        self.commands = []
        self._tmp = tempfile.TemporaryDirectory()
        self.context = PipelineContext(
            input_path=__file__, settings=settings, working_dir=self._tmp.name)
        self.context.watermark_required = watermark_required
        self._originals = {}

    def __enter__(self):
        from src.modules import ffmpeg_runner, silence_cutter, subtitle_generator

        self._originals = {
            (ffmpeg_runner, "execute"): ffmpeg_runner.execute,
            (ffmpeg_runner, "probe_duration"): ffmpeg_runner.probe_duration,
            (silence_cutter, "concat_files"): silence_cutter.concat_files,
            (subtitle_generator, "build_subtitle_file"): subtitle_generator.build_subtitle_file,
        }
        ffmpeg_runner.execute = self._execute
        ffmpeg_runner.probe_duration = lambda *_a, **_k: 5.0
        silence_cutter.concat_files = self._concat
        subtitle_generator.build_subtitle_file = self._write_ass
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

    def _write_ass(self, _items, _profile, ass_path, **_kwargs):
        self._touch(ass_path)

    @staticmethod
    def _touch(path):
        if not path or path.startswith("-") or "=" in os.path.basename(path):
            return
        try:
            with open(path, "wb") as handle:
                handle.write(b"x")
        except OSError:
            pass

    # 映像フィルタの指定 (-vf / -filter_complex) をすべて集める
    def filter_specs(self):
        specs = []
        for cmd in self.commands:
            for flag in ("-vf", "-filter_complex"):
                if flag in cmd:
                    specs.append(cmd[cmd.index(flag) + 1])
        return specs

    # 透かしの素材を入力に取るコマンドの数
    def watermark_inputs(self):
        return len([c for c in self.commands
                    if any(str(a).endswith("watermark.png") for a in c)])


_SETTINGS = {"ffmpeg": {}, "blur": {"enabled": False}}


class WatermarkNotRequiredTest(unittest.TestCase):

    # 未ログイン / 残高十分なら、コマンドに透かしが 1 つも現れないこと (§4-5)
    def test_no_watermark_in_commands(self):
        for overlay in (False, True):
            with self.subTest(overlay=overlay):
                timeline = _build_timeline(with_subtitle=True, with_overlay=overlay)
                with _RendererHarness(_SETTINGS) as harness:
                    renderer.render(timeline, harness.context)
                    self.assertEqual(harness.watermark_inputs(), 0)
                    self.assertFalse(harness.context.watermark_applied)


class WatermarkRequiredTest(unittest.TestCase):

    def _render(self, timeline):
        harness = _RendererHarness(_SETTINGS, watermark_required=True)
        with harness:
            renderer.render(timeline, harness.context)
        return harness

    # 合成が走る経路: 合成のコマンドへ混ぜ込み、単独パスを増やさないこと (§3.1 案 3)
    def test_composite_path_mixes_in(self):
        harness = self._render(_build_timeline(with_subtitle=True, with_overlay=True))

        self.assertEqual(harness.watermark_inputs(), 1)
        specs = [s for s in harness.filter_specs() if "[wm]" in s]
        self.assertTrue(specs, "合成へ透かしが入っていません")
        self.assertIn("[wmout]", specs[0])
        # 透かしは最後 = 字幕やオーバーレイの上に重なること
        self.assertGreater(specs[0].index("[wm]overlay="), specs[0].index("ass="))

    # 字幕のみの経路: 合成が走らないため単独パスで入ること
    def test_subtitle_only_path_adds_a_pass(self):
        harness = self._render(_build_timeline(with_subtitle=True))

        self.assertEqual(harness.watermark_inputs(), 1)
        self.assertTrue(harness.context.watermark_applied)

    # 字幕もオーバーレイも無い経路: ここも単独パスで入ること
    def test_standalone_path(self):
        harness = self._render(_build_timeline())

        self.assertEqual(harness.watermark_inputs(), 1)
        self.assertTrue(harness.context.watermark_applied)

    # どの分岐でも、未適用のまま出力へ進まないこと (§4-2)
    def test_never_leaves_the_watermark_unapplied(self):
        for subtitle, overlay in ((True, True), (True, False), (False, False)):
            with self.subTest(subtitle=subtitle, overlay=overlay):
                harness = self._render(_build_timeline(subtitle, overlay))
                self.assertFalse(watermark_overlay.is_required(harness.context))

    # レンダリングで入れた透かしを output_writer が二度焼きしないこと
    def test_output_writer_does_not_apply_twice(self):
        harness = _RendererHarness(_SETTINGS, watermark_required=True)
        with harness:
            renderer.render(_build_timeline(), harness.context)
            before = harness.watermark_inputs()
            harness.context.settings = dict(
                _SETTINGS, general={"output_directory": harness.context.working_dir})
            output_writer.run(harness.context)
            self.assertEqual(harness.watermark_inputs(), before)


class LegacyOutputTest(unittest.TestCase):

    # 合成が走らないレガシー経路では output_writer が唯一の焼き込み地点になる (§5.4)
    def _run(self, required):
        from src.modules import ffmpeg_runner

        commands = []
        original = ffmpeg_runner.execute

        def _execute(cmd, **_kwargs):
            commands.append(list(cmd))
            with open(cmd[-1], "wb") as handle:
                handle.write(b"x")

        ffmpeg_runner.execute = _execute
        try:
            with tempfile.TemporaryDirectory() as tmp:
                settings = {"ffmpeg": {}, "general": {"output_directory": tmp}}
                context = PipelineContext(input_path=os.path.join(tmp, "src.mp4"),
                                          settings=settings, working_dir=tmp)
                context.output_profile = {"width": 1080, "height": 1920,
                                          "is_portrait": True, "orientation": "portrait"}
                context.watermark_required = required
                source = os.path.join(tmp, "cut.mp4")
                with open(source, "wb") as handle:
                    handle.write(b"x")
                context.set_current_video_path(source)

                final = output_writer.run(context)
                # 一時ディレクトリはこの with を抜けると消えるため、ここで確かめる
                return commands, os.path.exists(final), context
        finally:
            ffmpeg_runner.execute = original

    def test_applies_a_single_pass(self):
        commands, written, context = self._run(required=True)

        self.assertEqual(len(commands), 1, "単独パスが 1 回だけ走ること")
        self.assertTrue(written)
        self.assertTrue(context.watermark_applied)
        # 縦 1080 幅に対する比率で配置されること (R8)
        filter_complex = commands[0][commands[0].index("-filter_complex") + 1]
        self.assertIn("scale=270:-1", filter_complex)

    def test_untouched_when_not_required(self):
        commands, written, context = self._run(required=False)

        self.assertEqual(commands, [], "不要なのに FFmpeg を起動しています")
        self.assertTrue(written)
        self.assertFalse(context.watermark_applied)


# 予約 / 確定 / 解放の呼ばれ方だけを見る差し替え
class _FakePoints:

    def __init__(self, watermark_required=False):
        self.reserved = []
        self.committed = []
        self.cancelled = []
        self._required = watermark_required

    def reserve(self, job_type, output_type, project_id=None):
        reservation = Reservation(job_type, output_type, reservation_id="r1",
                                  watermark_required=self._required)
        self.reserved.append(reservation)
        return reservation

    def commit(self, reservation):
        self.committed.append(reservation)

    def cancel(self, reservation):
        self.cancelled.append(reservation)


class PipelineReservationTest(unittest.TestCase):

    def setUp(self):
        from src.modules import ffmpeg_runner, output_profile

        self._originals = {
            (ffmpeg_runner, "ensure_available"): ffmpeg_runner.ensure_available,
            (output_profile, "resolve_output_profile"): output_profile.resolve_output_profile,
            (pipeline_runner, "_run_timeline"): pipeline_runner._run_timeline,
        }
        ffmpeg_runner.ensure_available = lambda *_a, **_k: None
        output_profile.resolve_output_profile = lambda *_a, **_k: {
            "width": 1920, "height": 1080, "is_portrait": False, "orientation": "landscape"}
        self.contexts = []
        self.addCleanup(self._restore)

    def _restore(self):
        for (module, name), original in self._originals.items():
            setattr(module, name, original)

    def _run(self, points, outcome="ok"):
        def _fake_run(context):
            self.contexts.append(context)
            if outcome == "cancelled":
                raise PipelineCancelled("テスト: 中断")
            if outcome == "error":
                raise RuntimeError("テスト: 失敗")
            return "out.mp4"

        pipeline_runner._run_timeline = _fake_run
        settings = {"ffmpeg": {}, "timeline": {"enabled": True},
                    "silence_cut": {"mode": "edit_points"}}
        return pipeline_runner.run_pipeline(__file__, settings, points=points)

    # 成果物ができてから確定すること (R3)
    def test_commits_after_the_output(self):
        points = _FakePoints()
        self._run(points)

        self.assertEqual(len(points.reserved), 1)
        self.assertEqual(points.committed, points.reserved)
        self.assertEqual(points.cancelled, [])
        self.assertEqual(points.reserved[0].job_type, "clip")
        self.assertEqual(points.reserved[0].output_type, "video")

    # サーバーが「透かしあり」と答えたらコンテキストへ載ること (R9)
    def test_watermark_flag_comes_from_the_server(self):
        self._run(_FakePoints(watermark_required=True))
        self.assertTrue(self.contexts[0].watermark_required)

        self.contexts.clear()
        self._run(_FakePoints(watermark_required=False))
        self.assertFalse(self.contexts[0].watermark_required)

    # 失敗したら消費しないこと (R6)
    def test_cancels_on_failure(self):
        points = _FakePoints()
        with self.assertRaises(RuntimeError):
            self._run(points, outcome="error")

        self.assertEqual(points.committed, [])
        self.assertEqual(points.cancelled, points.reserved)

    # 中断 (編集画面のキャンセル) でも消費しないこと (R6)
    def test_cancels_on_user_cancel(self):
        points = _FakePoints()
        with self.assertRaises(PipelineCancelled):
            self._run(points, outcome="cancelled")

        self.assertEqual(points.committed, [])
        self.assertEqual(points.cancelled, points.reserved)

    # ポイントを注入しない呼び出し (CLI / テスト) は従来どおり動くこと
    def test_without_points(self):
        self.assertEqual(self._run(None), "out.mp4")
        self.assertFalse(self.contexts[0].watermark_required)


if __name__ == "__main__":
    unittest.main()
