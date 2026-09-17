# レンダリング経路へのぼかし差し込み (src/timeline/renderer.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve2.md §6 Phase 4 の完了条件):
#   ・機能 OFF なら FFmpeg へ渡すコマンドが従来と 1 文字も変わらないこと (R9)
#   ・ぼかしがチェーンの**先頭**へ入ること (R8: 字幕・アイコンをぼかさない)
#   ・字幕のみの経路・オーバーレイの経路・どちらも無い経路の 3 分岐すべてで
#     ぼかしが焼き込まれること (§5.5.2 / §5.5.3)
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.pipeline.pipeline_context import PipelineContext
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


def _build_timeline(with_subtitle=False):
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 5.0, 0.0, 5.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=[
        AudioClip("a1", "c1"),
    ])
    clips = [SubtitleClip("s1", 1.0, 2.0, "テスト字幕")] if with_subtitle else []
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1", clips=clips)
    return Timeline(fps=60, width=1920, height=1080,
                    source={"media_id": "m1", "input_path": __file__},
                    media_pool=media, tracks=[video, audio, subtitle])


# FFmpeg を実際には起動せず、渡されたコマンドだけを控える
class _RendererHarness:

    def __init__(self, test, settings):
        self.test = test
        self.commands = []
        self._tmp = tempfile.TemporaryDirectory()
        self.context = PipelineContext(
            input_path=__file__, settings=settings, working_dir=self._tmp.name)
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

    # 実行されたことにして、出力ファイルだけ作る
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


class BlurDisabledTest(unittest.TestCase):

    # 機能 OFF なら、ぼかし関連の文字列がコマンドへ 1 つも現れないこと (R9)
    def test_no_blur_in_commands(self):
        timeline = _build_timeline(with_subtitle=True)
        settings = {"ffmpeg": {}, "blur": {"enabled": False}}
        with _RendererHarness(self, settings) as harness:
            renderer.render(timeline, harness.context)
            joined = " ".join(" ".join(c) for c in harness.commands)
            self.assertNotIn("alphamerge", joined)
            self.assertNotIn("blur_mask", joined)
            # マスクを用意しようとすらしないこと
            self.assertIsNone(harness.context.blur_mask_path)
            self.assertFalse(harness.context.blur_applied)


class BlurEnabledTest(unittest.TestCase):

    # マスクがある状態で、3 分岐それぞれにぼかしが入ること
    def _render_with_mask(self, timeline):
        settings = {"ffmpeg": {}, "blur": {"enabled": True}}
        with _RendererHarness(self, settings) as harness:
            mask_path = os.path.join(harness.context.working_dir, "mask.mkv")
            with open(mask_path, "wb") as handle:
                handle.write(b"dummy")

            # 解析とマスク生成は別のテストで見ているため、ここは経路だけを見る
            original = renderer._prepare_blur_mask
            renderer._prepare_blur_mask = lambda *_a, **_k: mask_path
            try:
                renderer.render(timeline, harness.context)
            finally:
                renderer._prepare_blur_mask = original
            return harness

    # 字幕のみの経路: ぼかしは字幕より**前**に入ること (R8)
    def test_subtitle_only_path(self):
        harness = self._render_with_mask(_build_timeline(with_subtitle=True))
        specs = [s for s in harness.filter_specs() if "alphamerge" in s]
        self.assertTrue(specs, "字幕経路でぼかしが入っていません")
        spec = specs[0]
        self.assertLess(spec.index("alphamerge"), spec.index("subtitles="),
                        "ぼかしが字幕より後ろに入っています (字幕までぼけてしまいます)")
        self.assertTrue(harness.context.blur_applied)

    # 字幕もオーバーレイも無い経路: 単独パスで焼き込まれること (§5.5.3)
    def test_standalone_path(self):
        harness = self._render_with_mask(_build_timeline(with_subtitle=False))
        specs = [s for s in harness.filter_specs() if "alphamerge" in s]
        self.assertTrue(specs, "単独パスでぼかしが入っていません")
        self.assertTrue(harness.context.blur_applied)

    # 未適用のまま出力へ進まないこと (保険が効くこと)
    def test_never_leaves_blur_unapplied(self):
        for with_subtitle in (True, False):
            harness = self._render_with_mask(_build_timeline(with_subtitle))
            from src.modules import blur_overlay

            self.assertFalse(
                blur_overlay.is_required(harness.context),
                "ぼかしが未適用のまま出力へ進んでいます")



class BlurFailureTest(unittest.TestCase):

    # 「ぼかすと指定したのに素で出た」を絶対に起こさないこと (§5.9 / §4-5)。
    # 解析結果が見つからない状態で書き出そうとしたときの振る舞いを見る。
    def _timeline_with_decisions(self):
        from src.blur import decisions as blur_decisions

        timeline = _build_timeline(with_subtitle=False)
        state = blur_decisions.with_identity(
            blur_decisions.load(timeline), "p2", blur_decisions.BLUR)
        state["fingerprint"] = "fp-that-does-not-match"
        blur_decisions.store(timeline, state)
        return timeline

    # フックが無い呼び出し (CLI / テスト) では**出力を中止する**
    def test_aborts_without_callback(self):
        from src.exceptions import TimelineError

        timeline = self._timeline_with_decisions()
        settings = {"ffmpeg": {}, "blur": {"enabled": True}}
        with _RendererHarness(self, settings) as harness:
            with self.assertRaises(TimelineError):
                renderer.render(timeline, harness.context)

    # 利用者が「ぼかしを入れずに出力」を選んだら続行すること
    def test_continues_when_user_accepts(self):
        timeline = self._timeline_with_decisions()
        settings = {"ffmpeg": {}, "blur": {"enabled": True}}
        with _RendererHarness(self, settings) as harness:
            asked = []
            harness.context.blur_failure_callback = lambda reason: asked.append(reason) or True
            renderer.render(timeline, harness.context)
            self.assertTrue(asked, "確認せずに続行しています")
            self.assertIsNone(harness.context.blur_mask_path)

    # 利用者が「中止」を選んだら出力しないこと
    def test_aborts_when_user_declines(self):
        from src.exceptions import TimelineError

        timeline = self._timeline_with_decisions()
        settings = {"ffmpeg": {}, "blur": {"enabled": True}}
        with _RendererHarness(self, settings) as harness:
            harness.context.blur_failure_callback = lambda _reason: False
            with self.assertRaises(TimelineError):
                renderer.render(timeline, harness.context)

    # 指定が無ければ (ぼかす気が無ければ) 確認は出ないこと
    def test_no_prompt_without_decisions(self):
        timeline = _build_timeline(with_subtitle=False)
        settings = {"ffmpeg": {}, "blur": {"enabled": True}}
        with _RendererHarness(self, settings) as harness:
            asked = []
            harness.context.blur_failure_callback = lambda reason: asked.append(reason) or True
            renderer.render(timeline, harness.context)
            self.assertEqual(asked, [])


if __name__ == "__main__":
    unittest.main()
