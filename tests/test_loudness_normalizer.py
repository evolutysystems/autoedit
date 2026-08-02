# ラウドネス正規化 (src/modules/loudness_normalizer.py) の単体テスト (resolve22 §9-1)
# 実行: python -m unittest discover -s tests
# FFmpeg を実行しない純関数 (フィルタ組み立て / JSON 抽出) と、
# FFmpeg 呼び出しをスタブ化したスキップ判定 (§5.5/§7) を検証する。
import unittest
from unittest import mock

from src.exceptions import FFmpegError
from src.modules import loudness_normalizer as ln

# 1パス目 stderr の実出力例 (loudnorm print_format=json)
_SAMPLE_STDERR = """\
[Parsed_loudnorm_0 @ 000001c2f3b3c840]
{
\t"input_i" : "-9.20",
\t"input_tp" : "-0.30",
\t"input_lra" : "5.10",
\t"input_thresh" : "-20.50",
\t"output_i" : "-14.10",
\t"output_tp" : "-1.20",
\t"output_lra" : "5.00",
\t"output_thresh" : "-25.30",
\t"normalization_type" : "dynamic",
\t"target_offset" : "0.40"
}"""


# テスト用設定 (loudness は既定値。ffmpeg は解決不能な実行ファイル名で実行を防ぐ)
def _settings(**loudness_overrides):
    loudness = {"enabled": True}
    loudness.update(loudness_overrides)
    return {
        "loudness": loudness,
        "ffmpeg": {"audio_codec": "aac", "audio_sample_rate": 48000,
                   "executable": "no_such_ffmpeg_for_test",
                   "ffprobe_executable": "no_such_ffprobe_for_test"},
    }


class TestConfig(unittest.TestCase):

    # 設定欠落時は既定値 (YouTube 目標) で補完する (§6)
    def test_defaults(self):
        cfg = ln.loudness_config({})
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["target_i"], -14.0)
        self.assertEqual(cfg["target_tp"], -1.0)
        self.assertEqual(cfg["target_lra"], 11.0)
        self.assertTrue(cfg["two_pass"])
        self.assertEqual(cfg["audio_bitrate"], "192k")


class TestFilterBuild(unittest.TestCase):

    def setUp(self):
        self.cfg = ln.loudness_config({})

    # 目標値のみ (2パス目・1パス dynamic の基本形)
    def test_targets_only(self):
        self.assertEqual(ln.build_loudnorm_filter(self.cfg),
                         "loudnorm=I=-14:TP=-1:LRA=11")

    # 測定パス (print_format=json 付き)
    def test_measurement_pass(self):
        self.assertEqual(ln.build_loudnorm_filter(self.cfg, print_json=True),
                         "loudnorm=I=-14:TP=-1:LRA=11:print_format=json")

    # 適用パス (実測値 + offset + linear=true / §5.2)
    def test_apply_pass_with_measured(self):
        measured = {"input_i": -9.2, "input_tp": -0.3, "input_lra": 5.1,
                    "input_thresh": -20.5, "target_offset": 0.4}
        self.assertEqual(
            ln.build_loudnorm_filter(self.cfg, measured=measured),
            "loudnorm=I=-14:TP=-1:LRA=11"
            ":measured_I=-9.2:measured_TP=-0.3:measured_LRA=5.1:measured_thresh=-20.5"
            ":offset=0.4:linear=true")


class TestParseJson(unittest.TestCase):

    # 実出力例から実測値を抽出する (文字列値 → float)
    def test_parse_sample(self):
        measured = ln.parse_loudnorm_json(_SAMPLE_STDERR)
        self.assertEqual(measured["input_i"], -9.2)
        self.assertEqual(measured["input_tp"], -0.3)
        self.assertEqual(measured["input_lra"], 5.1)
        self.assertEqual(measured["input_thresh"], -20.5)
        self.assertEqual(measured["target_offset"], 0.4)

    # JSON が無い / 壊れている / 必須キー欠落は None
    def test_parse_invalid(self):
        self.assertIsNone(ln.parse_loudnorm_json(""))
        self.assertIsNone(ln.parse_loudnorm_json("frame=100 fps=60"))
        self.assertIsNone(ln.parse_loudnorm_json("{ broken json"))
        self.assertIsNone(ln.parse_loudnorm_json('{"input_i": "-9.2"}'))


class TestNormalizeFileSkip(unittest.TestCase):

    # enabled=false は FFmpeg を一切呼ばず入力パスを返す (§5.5)
    def test_disabled_returns_input(self):
        with mock.patch.object(ln, "has_audio_stream") as probe:
            result = ln.normalize_file("in.mp4", "out.mp4", _settings(enabled=False))
        self.assertEqual(result, "in.mp4")
        probe.assert_not_called()

    # 音声ストリーム無しはスキップ (§5.5)
    def test_no_audio_returns_input(self):
        with mock.patch.object(ln, "has_audio_stream", return_value=False), \
                mock.patch.object(ln.ffmpeg_runner, "execute") as execute:
            result = ln.normalize_file("in.mp4", "out.mp4", _settings())
        self.assertEqual(result, "in.mp4")
        execute.assert_not_called()

    # 測定失敗 (JSON 抽出不能) は適用せず元パスで継続 (§7 / §10-7)
    def test_measure_failure_returns_input(self):
        with mock.patch.object(ln, "has_audio_stream", return_value=True), \
                mock.patch.object(ln.ffmpeg_runner, "probe_duration", return_value=10.0), \
                mock.patch.object(ln, "measure_loudness", return_value=None), \
                mock.patch.object(ln.ffmpeg_runner, "execute") as execute:
            result = ln.normalize_file("in.mp4", "out.mp4", _settings())
        self.assertEqual(result, "in.mp4")
        execute.assert_not_called()

    # 適用失敗 (FFmpegError) も元パスで継続する (§7)
    def test_apply_failure_returns_input(self):
        measured = {"input_i": -9.2, "input_tp": -0.3, "input_lra": 5.1,
                    "input_thresh": -20.5, "target_offset": 0.4}
        with mock.patch.object(ln, "has_audio_stream", return_value=True), \
                mock.patch.object(ln.ffmpeg_runner, "probe_duration", return_value=10.0), \
                mock.patch.object(ln, "measure_loudness", return_value=measured), \
                mock.patch.object(ln.ffmpeg_runner, "execute",
                                  side_effect=FFmpegError("失敗")):
            result = ln.normalize_file("in.mp4", "out.mp4", _settings())
        self.assertEqual(result, "in.mp4")


class TestNormalizeFileSuccess(unittest.TestCase):

    # 成功時は出力パスを返し、映像コピー + 既存キー流用の音声設定で適用する (§5.2)
    def test_success_returns_output_and_builds_command(self):
        measured = {"input_i": -9.2, "input_tp": -0.3, "input_lra": 5.1,
                    "input_thresh": -20.5, "target_offset": 0.4}
        with mock.patch.object(ln, "has_audio_stream", return_value=True), \
                mock.patch.object(ln.ffmpeg_runner, "probe_duration", return_value=10.0), \
                mock.patch.object(ln, "measure_loudness", return_value=measured), \
                mock.patch.object(ln.ffmpeg_runner, "execute", return_value=0) as execute:
            result = ln.normalize_file("in.mp4", "out.mp4", _settings())
        self.assertEqual(result, "out.mp4")
        cmd = execute.call_args[0][0]
        # 映像は無劣化コピー (タイムライン不変 / §4-7)
        self.assertIn("copy", cmd[cmd.index("-c:v") + 1])
        # 音声は既存 ffmpeg 節のコーデック/サンプルレート + loudness のビットレート
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        self.assertEqual(cmd[cmd.index("-b:a") + 1], "192k")
        self.assertEqual(cmd[cmd.index("-ar") + 1], "48000")
        # 適用フィルタは実測値 + linear=true (§5.2)
        af = cmd[cmd.index("-af") + 1]
        self.assertIn("measured_I=-9.2", af)
        self.assertIn("linear=true", af)
        # 進捗総尺が渡される (run_ffmpeg_progress 準拠)
        self.assertEqual(execute.call_args[1]["total_duration"], 10.0)

    # 1パス (two_pass=false) は測定せず dynamic 適用する (§3)
    def test_single_pass_skips_measurement(self):
        with mock.patch.object(ln, "has_audio_stream", return_value=True), \
                mock.patch.object(ln.ffmpeg_runner, "probe_duration", return_value=10.0), \
                mock.patch.object(ln, "measure_loudness") as measure, \
                mock.patch.object(ln.ffmpeg_runner, "execute", return_value=0) as execute:
            result = ln.normalize_file("in.mp4", "out.mp4", _settings(two_pass=False))
        self.assertEqual(result, "out.mp4")
        measure.assert_not_called()
        af = execute.call_args[0][0]
        af = af[af.index("-af") + 1]
        self.assertEqual(af, "loudnorm=I=-14:TP=-1:LRA=11")


if __name__ == "__main__":
    unittest.main()
