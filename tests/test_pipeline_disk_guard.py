# パイプラインの容量ガードと中間ファイルの寿命の単体テスト
# (docs/error/fromUser/resolve.md §7)
# 実行: python -m unittest discover -s tests
# 重点:
#   ・空きが足りなければ 1 バイトも書かずに止まること
#   ・general.work_directory が効き、終了時にフォルダごと消えること
#   ・release_intermediate で前段を解放できること
#   ・容量不足だけは「失敗しても継続」の対象から外れること
import os
import tempfile
import unittest
from unittest import mock

from src.exceptions import DiskSpaceError, FFmpegError
from src.modules import loudness_normalizer
from src.pipeline import pipeline_runner
from src.pipeline.pipeline_context import PipelineContext
from src.utils import disk


class WorkDirectoryTest(unittest.TestCase):

    # 未指定なら従来どおり %TEMP% の一時フォルダを使う
    def test_defaults_to_the_system_temp(self):
        context = PipelineContext("in.mp4", {})
        try:
            self.assertTrue(os.path.isdir(context.working_dir))
            self.assertIn("autoedit_", os.path.basename(context.working_dir))
        finally:
            context.cleanup()

    # 指定したフォルダの**配下に実行ごとの専用フォルダ**を掘る
    def test_uses_the_configured_folder(self):
        with tempfile.TemporaryDirectory() as base:
            context = PipelineContext("in.mp4", {}, working_dir=base)
            working = context.working_dir
            self.assertTrue(working.startswith(base))
            self.assertNotEqual(os.path.abspath(working), os.path.abspath(base))
            # 連結用の部品など allocate を通らない中間物も置かれる
            with open(os.path.join(working, "tl_part_00000.mov"), "wb") as handle:
                handle.write(b"x")
            context.cleanup()
            # 実行ごとのフォルダは中身ごと消える (指定フォルダ自体は残す)
            self.assertFalse(os.path.exists(working))
            self.assertTrue(os.path.isdir(base))

    # 設定を読み出して渡していること (空なら None = 従来どおり)
    def test_work_directory_setting_is_resolved(self):
        self.assertIsNone(pipeline_runner._work_directory({}))
        self.assertIsNone(
            pipeline_runner._work_directory({"general": {"work_directory": "  "}}))
        with tempfile.TemporaryDirectory() as base:
            self.assertEqual(
                pipeline_runner._work_directory({"general": {"work_directory": base}}),
                base)

    # 使えない指定は既定の一時フォルダへ倒す (設定の誤りで実行不能にしない)
    def test_unusable_work_directory_falls_back(self):
        with mock.patch("os.makedirs", side_effect=OSError("no")):
            self.assertIsNone(
                pipeline_runner._work_directory({"general": {"work_directory": "Z:/x"}}))


class ReleaseIntermediateTest(unittest.TestCase):

    def test_releases_and_reports_the_reclaimed_size(self):
        context = PipelineContext("in.mp4", {})
        try:
            path = context.allocate_intermediate("timeline_base.mov")
            with open(path, "wb") as handle:
                handle.write(b"x" * 4096)

            freed = context.release_intermediate("timeline_base.mov")

            self.assertEqual(freed, 4096)
            self.assertFalse(os.path.exists(path))
            self.assertNotIn("timeline_base.mov", context.intermediate_paths)
        finally:
            context.cleanup()

    # 無いものを解放してもエラーにしない (二重解放に耐える)
    def test_releasing_twice_is_harmless(self):
        context = PipelineContext("in.mp4", {})
        try:
            context.allocate_intermediate("missing.mp4")
            self.assertEqual(context.release_intermediate("missing.mp4"), 0)
            self.assertEqual(context.release_intermediate("missing.mp4"), 0)
        finally:
            context.cleanup()


class SpaceGuardTest(unittest.TestCase):

    # 空きゼロなら中間ファイルを 1 つも作らずに止まる
    def test_run_stops_before_writing_anything(self):
        settings = {"general": {"min_free_space_gb": 20}}
        with mock.patch.object(disk, "free_bytes", return_value=1024):
            with self.assertRaises(DiskSpaceError):
                pipeline_runner._ensure_space_for_run("in.mp4", settings)

    # 0 なら確認しない (従来どおり動かしたい利用者のため)
    def test_zero_requirement_skips_the_check(self):
        with mock.patch.object(disk, "free_bytes") as measured:
            pipeline_runner._ensure_space_for_run(
                "in.mp4", {"general": {"min_free_space_gb": 0}})
        measured.assert_not_called()

    # 十分な空きがあれば通る
    def test_passes_when_enough(self):
        with mock.patch.object(disk, "free_bytes", return_value=500 * disk.GIB):
            pipeline_runner._ensure_space_for_run(
                "in.mp4", {"general": {"min_free_space_gb": 20}})


class NormalizerDiskErrorTest(unittest.TestCase):

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self._temp.name, "normalized.mp4")

    def tearDown(self):
        self._temp.cleanup()

    # 容量不足は握り潰さず再送出する (原因から離れた所で症状を出さないため)
    def test_disk_space_error_is_re_raised(self):
        settings = {"loudness": {"enabled": True, "two_pass": False}}
        with mock.patch.object(loudness_normalizer, "has_audio_stream", return_value=True), \
                mock.patch("src.modules.ffmpeg_runner.probe_duration", return_value=10.0), \
                mock.patch("src.modules.ffmpeg_runner.execute_capture",
                           side_effect=DiskSpaceError("空き容量が足りません")):
            with self.assertRaises(DiskSpaceError):
                loudness_normalizer.normalize_file("in.mp4", self.out, settings)

    # それ以外の FFmpeg 失敗は従来どおり元の音声で継続する
    def test_other_ffmpeg_errors_still_continue(self):
        settings = {"loudness": {"enabled": True, "two_pass": False}}
        with mock.patch.object(loudness_normalizer, "has_audio_stream", return_value=True), \
                mock.patch("src.modules.ffmpeg_runner.probe_duration", return_value=10.0), \
                mock.patch("src.modules.ffmpeg_runner.execute_capture",
                           side_effect=FFmpegError("別の失敗")):
            result = loudness_normalizer.normalize_file("in.mp4", self.out, settings)
        self.assertEqual(result, "in.mp4")


if __name__ == "__main__":
    unittest.main()
