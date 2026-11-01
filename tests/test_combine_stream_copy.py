# 最終結合のストリームコピー (src/archive/clip_writer.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/quality/resolve3.md §10 / R1):
#   ・部品の規格が揃っていればコピー結合、揃わなければ従来の再エンコード
#   ・archive.combine.reencode=true では常に従来経路
#   ・probe が失敗したら安全側 (再エンコード) へ落ちる
#   ・コピー結合が失敗したら再エンコードで 1 回だけ繋ぎ直す
#   ・中止と容量不足は再試行せず貫通する
#   ・fps は r_frame_rate の文字列で比較する (60/1 と 60000/1001 を区別する)
# FFmpeg は起動しない。どちらの経路が呼ばれたかだけを検証する。
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import clip_writer
from src.exceptions import DiskSpaceError, FFmpegError, PipelineCancelled

_PARAMS = {"v_codec": "h264", "width": 1920, "height": 1080, "pix_fmt": "yuv420p",
           "fps": "60/1", "a_codec": "aac", "sample_rate": 48000, "channels": 2}


class _Recorder:

    # concat の 2 経路と probe を差し替え、どちらが呼ばれたかを記録する
    def __init__(self, testcase, params_by_path=None, probe_error=None,
                 copy_error=None):
        self.copy_calls = []
        self.reencode_calls = []
        self._params = params_by_path or {}
        self._probe_error = probe_error
        self._copy_error = copy_error

        testcase.addCleanup(setattr, clip_writer.silence_cutter, "concat_files",
                            clip_writer.silence_cutter.concat_files)
        testcase.addCleanup(setattr, clip_writer.concat_processor, "concat",
                            clip_writer.concat_processor.concat)
        testcase.addCleanup(setattr, clip_writer.ffmpeg_runner, "probe_stream_params",
                            clip_writer.ffmpeg_runner.probe_stream_params)
        testcase.addCleanup(setattr, clip_writer.ffmpeg_runner, "probe_duration",
                            clip_writer.ffmpeg_runner.probe_duration)
        clip_writer.silence_cutter.concat_files = self._copy
        clip_writer.concat_processor.concat = self._reencode
        clip_writer.ffmpeg_runner.probe_stream_params = self._probe
        clip_writer.ffmpeg_runner.probe_duration = lambda _p, _c: 100.0

    def _copy(self, paths, output_path, _cfg, reencode=False, total_duration=0.0,
              on_progress=None, cancel_cb=None):
        self.copy_calls.append((list(paths), reencode, total_duration))
        if self._copy_error is not None:
            raise self._copy_error
        return output_path

    def _reencode(self, paths, output_path, _cfg, total_duration=0.0, on_progress=None,
                  cancel_cb=None):
        self.reencode_calls.append((list(paths), total_duration))
        return output_path

    def _probe(self, path, _cfg):
        if self._probe_error is not None:
            raise self._probe_error
        return self._params.get(path, dict(_PARAMS))


class CombineStreamCopyTest(unittest.TestCase):

    # 規格が揃っていればコピーで繋ぐ (再エンコードは呼ばれない)
    def test_same_params_use_stream_copy(self):
        rec = _Recorder(self)
        clip_writer._combine(["a.mp4", "b.mp4"], "out.mp4", {}, settings={})
        self.assertEqual(len(rec.copy_calls), 1)
        self.assertEqual(rec.copy_calls[0][1], False)      # reencode=False
        self.assertEqual(rec.reencode_calls, [])

    # 寸法が違えば従来の再エンコード経路へ落ちる (OP/ED 混在時)
    def test_different_size_falls_back_to_reencode(self):
        other = dict(_PARAMS, width=1280, height=720)
        rec = _Recorder(self, params_by_path={"b.mp4": other})
        clip_writer._combine(["a.mp4", "b.mp4"], "out.mp4", {}, settings={})
        self.assertEqual(rec.copy_calls, [])
        self.assertEqual(len(rec.reencode_calls), 1)

    # fps は文字列で比べる (60/1 と 60000/1001 を同一とみなさない)
    def test_ntsc_fps_is_not_treated_as_60(self):
        other = dict(_PARAMS, fps="60000/1001")
        rec = _Recorder(self, params_by_path={"b.mp4": other})
        clip_writer._combine(["a.mp4", "b.mp4"], "out.mp4", {}, settings={})
        self.assertEqual(rec.copy_calls, [])
        self.assertEqual(len(rec.reencode_calls), 1)

    # 音声コーデックが違っても落ちる
    def test_different_audio_codec_falls_back(self):
        other = dict(_PARAMS, a_codec="mp3")
        rec = _Recorder(self, params_by_path={"b.mp4": other})
        clip_writer._combine(["a.mp4", "b.mp4"], "out.mp4", {}, settings={})
        self.assertEqual(rec.copy_calls, [])
        self.assertEqual(len(rec.reencode_calls), 1)

    # reencode=true では規格が揃っていても従来経路 (切り戻し)
    def test_reencode_setting_forces_the_old_path(self):
        rec = _Recorder(self)
        settings = {"archive": {"combine": {"reencode": True}}}
        clip_writer._combine(["a.mp4", "b.mp4"], "out.mp4", {}, settings=settings)
        self.assertEqual(rec.copy_calls, [])
        self.assertEqual(len(rec.reencode_calls), 1)

    # probe が失敗したら安全側 (再エンコード) へ落ちる
    def test_probe_failure_falls_back_to_reencode(self):
        rec = _Recorder(self, probe_error=FFmpegError("probe 失敗"))
        clip_writer._combine(["a.mp4", "b.mp4"], "out.mp4", {}, settings={})
        self.assertEqual(rec.copy_calls, [])
        self.assertEqual(len(rec.reencode_calls), 1)

    # 映像か音声が欠けている部品はコピーで繋がない
    def test_missing_stream_falls_back_to_reencode(self):
        no_audio = dict(_PARAMS, a_codec=None)
        rec = _Recorder(self, params_by_path={"a.mp4": no_audio, "b.mp4": no_audio})
        clip_writer._combine(["a.mp4", "b.mp4"], "out.mp4", {}, settings={})
        self.assertEqual(rec.copy_calls, [])
        self.assertEqual(len(rec.reencode_calls), 1)

    # コピー結合が失敗したら再エンコードで 1 回だけ繋ぎ直す (成果物は必ず出す)
    def test_copy_failure_retries_with_reencode(self):
        rec = _Recorder(self, copy_error=FFmpegError("コピー不可"))
        clip_writer._combine(["a.mp4", "b.mp4"], "out.mp4", {}, settings={})
        self.assertEqual(len(rec.copy_calls), 1)
        self.assertEqual(len(rec.reencode_calls), 1)

    # 中止は再試行せず貫通する
    def test_cancel_is_not_retried(self):
        rec = _Recorder(self, copy_error=PipelineCancelled("中止"))
        with self.assertRaises(PipelineCancelled):
            clip_writer._combine(["a.mp4", "b.mp4"], "out.mp4", {}, settings={})
        self.assertEqual(rec.reencode_calls, [])

    # 容量不足も再試行せず貫通する
    def test_disk_full_is_not_retried(self):
        rec = _Recorder(self, copy_error=DiskSpaceError("容量不足"))
        with self.assertRaises(DiskSpaceError):
            clip_writer._combine(["a.mp4", "b.mp4"], "out.mp4", {}, settings={})
        self.assertEqual(rec.reencode_calls, [])

    # 進捗用の総尺は部品の合計 (取得できた分だけ足す)
    def test_total_duration_is_summed(self):
        rec = _Recorder(self)
        clip_writer._combine(["a.mp4", "b.mp4"], "out.mp4", {}, settings={})
        self.assertAlmostEqual(rec.copy_calls[0][2], 200.0)

    # 1 本だけなら従来どおり複製する (どちらの concat も呼ばない)
    def test_single_part_is_copied(self):
        rec = _Recorder(self)
        copied = []
        self.addCleanup(setattr, clip_writer.shutil, "copy",
                        clip_writer.shutil.copy)
        clip_writer.shutil.copy = lambda src, dst: copied.append((src, dst))
        clip_writer._combine(["a.mp4"], "out.mp4", {}, settings={})
        self.assertEqual(copied, [("a.mp4", "out.mp4")])
        self.assertEqual(rec.copy_calls, [])
        self.assertEqual(rec.reencode_calls, [])


if __name__ == "__main__":
    unittest.main()
