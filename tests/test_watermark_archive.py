# アーカイブ切り抜き経路への透かしとポイントの差し込み (ver5 resolve.md §5.4 経路 C / D)
# 実行: python -m unittest discover -s tests
# 重点:
#   ・出力本数によらず予約は 1 本であること (§2.5 / ジョブ単位 100pt)
#   ・結果画面でのキャンセル (出力 0 件) では消費しないこと (R6)
#   ・Timeline 経路は合成へ混ぜ込み、レガシー経路は単独パスで入ること (§3.1)
#   ・個別出力にも結合出力にも透かしが入ること (確認事項 #6)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import clip_writer
from src.services.points import Reservation


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


class ArchiveReservationTest(unittest.TestCase):

    def setUp(self):
        self._original = clip_writer._write_clips
        self.addCleanup(lambda: setattr(clip_writer, "_write_clips", self._original))
        self.flags = []

    def _stub(self, outputs=None, error=None):
        def _fake(_input_path, _settings, _clips, _progress_cb=None, _result_callback=None,
                  _curve=None, watermark_required=False):
            self.flags.append(watermark_required)
            if error is not None:
                raise error
            return list(outputs or [])

        clip_writer._write_clips = _fake

    # 出力が 2 本でも予約と確定は 1 回ずつ (ジョブ単位 100pt / §2.5)
    def test_one_reservation_per_job(self):
        self._stub(outputs=["clip1.mp4", "combined.mp4"])
        points = _FakePoints()

        outputs = clip_writer.write_clips("vod.mp4", {}, [{"use": True}], points=points)

        self.assertEqual(len(outputs), 2)
        self.assertEqual(len(points.reserved), 1)
        self.assertEqual(points.committed, points.reserved)
        self.assertEqual(points.cancelled, [])
        self.assertEqual(points.reserved[0].job_type, "archive")
        self.assertEqual(points.reserved[0].output_type, "video")

    # 結果画面でキャンセルすると出力 0 件 → 消費しない (R6)
    def test_cancel_when_nothing_is_written(self):
        self._stub(outputs=[])
        points = _FakePoints()

        self.assertEqual(clip_writer.write_clips("vod.mp4", {}, [], points=points), [])
        self.assertEqual(points.committed, [])
        self.assertEqual(points.cancelled, points.reserved)

    # 失敗でも消費しない (R6)
    def test_cancel_on_failure(self):
        self._stub(error=RuntimeError("テスト: 失敗"))
        points = _FakePoints()

        with self.assertRaises(RuntimeError):
            clip_writer.write_clips("vod.mp4", {}, [{"use": True}], points=points)
        self.assertEqual(points.committed, [])
        self.assertEqual(points.cancelled, points.reserved)

    # 透かしの要否はサーバーの応答から書き出し側へ渡ること (R9)
    def test_watermark_flag_is_passed_down(self):
        self._stub(outputs=["combined.mp4"])
        clip_writer.write_clips("vod.mp4", {}, [], points=_FakePoints(watermark_required=True))
        clip_writer.write_clips("vod.mp4", {}, [], points=_FakePoints(watermark_required=False))
        clip_writer.write_clips("vod.mp4", {}, [], points=None)

        self.assertEqual(self.flags, [True, False, False])


# 焼き込み 1 本ぶんの prepared (レガシー経路が見るキーだけ)
def _prepared(index, clip_dir):
    return {
        "index": index,
        "start": 0.0,
        "end": 10.0,
        "prepared_path": os.path.join(clip_dir, f"clip{index}.mp4"),
        "profile": {"width": 1920, "height": 1080, "is_portrait": False},
        "eff_cfg": {},
        "items": [],
    }


class LegacyBurnTest(unittest.TestCase):

    # レガシー経路 (合成が走らない) では、焼き込みの後に単独パスで入ること (§3.1)
    def setUp(self):
        from src.modules import ffmpeg_runner

        self.commands = []
        originals = {
            (ffmpeg_runner, "execute"): ffmpeg_runner.execute,
            (ffmpeg_runner, "probe_duration"): ffmpeg_runner.probe_duration,
            (clip_writer, "_burn_one"): clip_writer._burn_one,
            (clip_writer, "_decorate_clip"): clip_writer._decorate_clip,
        }
        ffmpeg_runner.execute = lambda cmd, **_k: self.commands.append(list(cmd))
        ffmpeg_runner.probe_duration = lambda *_a, **_k: 10.0
        clip_writer._burn_one = lambda prepared, *_a, **_k: prepared["prepared_path"]
        clip_writer._decorate_clip = lambda _source, out, *_a, **_k: ([out], out)
        self.addCleanup(
            lambda: [setattr(m, n, o) for (m, n), o in originals.items()])

    def _burn(self, required):
        prepared = {1: _prepared(1, "work")}
        edited = [{"index": 1, "items": [], "theme": "", "use": True}]
        return clip_writer._burn_clips(
            "vod.mp4", edited, prepared, {}, {}, "work", None, {"enabled": False}, "",
            watermark_required=required)

    def test_adds_a_single_pass(self):
        burned = self._burn(required=True)

        self.assertEqual(len(self.commands), 1, "単独パスが 1 回だけ走ること")
        self.assertTrue(self.commands[0][-1].endswith("watermarked.mp4"))
        # 本編 (body) が透かし入りのファイルになる = 個別出力にも結合出力にも入る (確認事項 #6)
        self.assertTrue(burned[0]["body"].endswith("watermarked.mp4"))
        self.assertEqual(burned[0]["parts"], [burned[0]["body"]])

    def test_untouched_when_not_required(self):
        burned = self._burn(required=False)

        self.assertEqual(self.commands, [], "不要なのに FFmpeg を起動しています")
        self.assertTrue(burned[0]["body"].endswith("clip1.mp4"))


class TimelineRenderTest(unittest.TestCase):

    # Timeline 経路では renderer へ要否を渡し、合成へ混ぜ込ませること (§3.1 案 3)
    def test_flag_reaches_the_render_context(self):
        from src.archive import timeline_builder as archive_timeline
        from src.timeline import renderer

        seen = []
        originals = {
            (archive_timeline, "split_by_clip"): archive_timeline.split_by_clip,
            (renderer, "render"): renderer.render,
            (clip_writer, "_decorate_clip"): clip_writer._decorate_clip,
        }
        archive_timeline.split_by_clip = lambda _timeline: [(1, object())]
        renderer.render = lambda _timeline, context: seen.append(context.watermark_required)
        clip_writer._decorate_clip = lambda _source, out, *_a, **_k: ([out], out)
        self.addCleanup(
            lambda: [setattr(m, n, o) for (m, n), o in originals.items()])

        import tempfile

        with tempfile.TemporaryDirectory() as workdir:
            prepared = {1: dict(_prepared(1, workdir), normalized_path=__file__)}
            for required in (True, False):
                clip_writer._render_clips(
                    "vod.mp4", object(), [{"index": 1, "use": True}], prepared,
                    {}, {}, {}, workdir, None, {"enabled": False}, "",
                    watermark_required=required)

        self.assertEqual(seen, [True, False])


if __name__ == "__main__":
    unittest.main()
