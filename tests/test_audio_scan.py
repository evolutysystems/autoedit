# 全長 1 パスの音声解析 (src/archive/features.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/quality/resolve3.md §10 / R5):
#   ・astats 出力のパース (-inf / nan を弾く)
#   ・1 秒値→セル値の按分が**エネルギー平均** (dB の算術平均ではない)
#   ・per_cell と single_pass で採点の TOP 選択が一致すること (§13-2 の確認条件)
#   ・引き継ぎの照合 (params 不一致で測り直す)
# FFmpeg は起動しない。scan_audio だけ差し替えて判定を検証する。
import math
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import features, scoring


def _settings(noise_db=-30, min_silence=0.3, mode="single_pass"):
    return {
        "ffmpeg": {"audio_sample_rate": 48000},
        "volume_analysis": {"base_noise_db": noise_db,
                            "base_min_silence_sec": min_silence},
        "archive": {"scoring": {"feature_mode": mode}},
    }


class ParsePerSecTest(unittest.TestCase):

    # frame ヘッダの pts_time を秒インデックスへ使う (error 20261102 R2)
    def test_frame_header_decides_the_second(self):
        text = ("frame:0    pts:0       pts_time:0\n"
                "lavfi.astats.Overall.Peak_level=-10.0\n"
                "lavfi.astats.Overall.RMS_level=-23.4\n"
                "frame:1    pts:48000   pts_time:1\n"
                "lavfi.astats.Overall.Peak_level=-18.5\n"
                "lavfi.astats.Overall.RMS_level=-31.2\n")
        self.assertEqual(features._parse_per_sec(text), [
            {"rms": -23.4, "peak": -10.0},
            {"rms": -31.2, "peak": -18.5},
        ])

    # 秒が飛んでいても後ろがずれない (飛んだ秒は穴として残す)
    # 出現順に詰めると以降の時刻がすべてずれ、発話区間へ無音部の値が割り当たる
    def test_a_missing_second_stays_a_hole(self):
        text = ("frame:0    pts:0       pts_time:0\n"
                "lavfi.astats.Overall.Peak_level=-10.0\n"
                "lavfi.astats.Overall.RMS_level=-23.4\n"
                "frame:3    pts:144000  pts_time:3\n"
                "lavfi.astats.Overall.Peak_level=-18.5\n"
                "lavfi.astats.Overall.RMS_level=-31.2\n")
        self.assertEqual(features._parse_per_sec(text), [
            {"rms": -23.4, "peak": -10.0},
            {"rms": None, "peak": None},
            {"rms": None, "peak": None},
            {"rms": -31.2, "peak": -18.5},
        ])

    # 行が壊れた出力でも、壊れた行だけを捨てて残りは正しい秒へ入る
    # (error 20261102 §3 で実際に出ていた混線の実物を縮めたもの)
    def test_broken_lines_are_discarded(self):
        text = ("frame:0    pts:0       pts_time:0\n"
                "lavfi.astats.Overall.Peak_level=-10.0\n"
                "lavfi.astats.Overall.RMS_level=-tats.Overall.Peak_level=-34.4\n"
                "frame:1    pts:48000   pts_time:1\n"
                "lavfi.astats.Overall.Peak_level=-4avfi.astats.Overall.RMS=-24.8\n"
                "lavfi.astats.Overall.RMS_level=-31.2\n"
                "frame:2    pts:96000   pts_time:2\n"
                "lavfi.as18  pts:20064000 pts_time:418\n"
                "lavfi.astats.Overall.RMS_level=-27.0\n")
        self.assertEqual(features._parse_per_sec(text), [
            {"rms": None, "peak": -10.0},     # RMS の行が壊れた
            {"rms": -31.2, "peak": None},     # Peak の行が壊れた
            {"rms": -27.0, "peak": None},     # frame ヘッダに混ざった行は捨てる
        ])

    # silencedetect のメタデータが混ざっても無視する (ametadata を key 無しで出すため)
    def test_silencedetect_metadata_is_ignored(self):
        text = ("frame:0    pts:0       pts_time:0\n"
                "lavfi.silence_end=104.9885\n"
                "lavfi.silence_duration=0.342062\n"
                "lavfi.astats.Overall.Peak_level=-10.0\n"
                "lavfi.astats.Overall.RMS_level=-23.4\n")
        self.assertEqual(features._parse_per_sec(text),
                         [{"rms": -23.4, "peak": -10.0}])

    # CRLF の出力でも行末一致のパターンが外れない
    def test_crlf_output(self):
        text = ("frame:0    pts:0       pts_time:0\r\n"
                "lavfi.astats.Overall.Peak_level=-10.0\r\n"
                "lavfi.astats.Overall.RMS_level=-23.4\r\n")
        self.assertEqual(features._parse_per_sec(text),
                         [{"rms": -23.4, "peak": -10.0}])

    # RMS と Peak を出現順に 1 件ずつ対応させる (frame ヘッダが無い旧形式 / 後方互換)
    def test_pairs_rms_and_peak_in_order(self):
        text = ("lavfi.astats.Overall.RMS_level=-23.4\n"
                "lavfi.astats.Overall.Peak_level=-10.0\n"
                "lavfi.astats.Overall.RMS_level=-31.2\n"
                "lavfi.astats.Overall.Peak_level=-18.5\n")
        self.assertEqual(features._parse_per_sec(text), [
            {"rms": -23.4, "peak": -10.0},
            {"rms": -31.2, "peak": -18.5},
        ])

    # 無音部の -inf は None にする (そのまま使うと平均が壊れる)
    def test_minus_inf_becomes_none(self):
        text = ("lavfi.astats.Overall.RMS_level=-inf\n"
                "lavfi.astats.Overall.Peak_level=-inf\n")
        self.assertEqual(features._parse_per_sec(text),
                         [{"rms": None, "peak": None}])

    # nan も弾く
    def test_nan_becomes_none(self):
        text = ("lavfi.astats.Overall.RMS_level=nan\n"
                "lavfi.astats.Overall.Peak_level=nan\n")
        self.assertEqual(features._parse_per_sec(text),
                         [{"rms": None, "peak": None}])

    # 出力が空なら空リスト (呼び出し側が従来方式へ落ちる)
    def test_empty_output(self):
        self.assertEqual(features._parse_per_sec(""), [])


class EnergyMeanTest(unittest.TestCase):

    # 同じ値ならそのまま
    def test_same_values(self):
        self.assertAlmostEqual(features.energy_mean_db([-20.0, -20.0]), -20.0)

    # dB の算術平均ではなくエネルギー平均になっている
    # (volumedetect の mean_volume は RMS のため、算術平均では値がずれる)
    def test_is_not_the_arithmetic_mean(self):
        value = features.energy_mean_db([-20.0, -26.0])
        self.assertNotAlmostEqual(value, -23.0, places=2)
        expected = 10.0 * math.log10((10 ** -2.0 + 10 ** -2.6) / 2.0)
        self.assertAlmostEqual(value, expected)

    # 有効値が無ければ None
    def test_all_none(self):
        self.assertIsNone(features.energy_mean_db([None, None]))
        self.assertIsNone(features.energy_mean_db([]))

    # None は分母にも入れない
    def test_none_is_skipped(self):
        self.assertAlmostEqual(features.energy_mean_db([-20.0, None]), -20.0)


class RegionLevelsTest(unittest.TestCase):

    def setUp(self):
        self.per_sec = [
            {"rms": -20.0, "peak": -5.0},
            {"rms": -30.0, "peak": -12.0},
            {"rms": -40.0, "peak": -22.0},
        ]

    # 区間内の RMS はエネルギー平均 / Peak は最大値
    def test_mean_is_energy_mean_and_max_is_peak(self):
        mean_db, max_db = features.region_levels(self.per_sec, 0, 3)
        self.assertAlmostEqual(
            mean_db, features.energy_mean_db([-20.0, -30.0, -40.0]))
        self.assertEqual(max_db, -5.0)

    # 区間を切ると対応する 1 秒値だけを見る
    def test_window_is_respected(self):
        mean_db, max_db = features.region_levels(self.per_sec, 1, 2)
        self.assertAlmostEqual(mean_db, -30.0)
        self.assertEqual(max_db, -12.0)

    # 1 秒未満の区間でも重なる 1 件を見る
    def test_sub_second_window(self):
        mean_db, max_db = features.region_levels(self.per_sec, 1.2, 1.4)
        self.assertAlmostEqual(mean_db, -30.0)
        self.assertEqual(max_db, -12.0)

    # 1 秒値が無ければ (None, None)
    def test_without_per_sec(self):
        self.assertEqual(features.region_levels([], 0, 3), (None, None))

    # 範囲外は (None, None) に倒れる
    def test_out_of_range(self):
        self.assertEqual(features.region_levels(self.per_sec, 10, 12), (None, None))


class ScanMatchesTest(unittest.TestCase):

    def _scan(self, noise_db=-30, min_silence=0.3, step=1.0):
        return {"per_sec": [{"rms": -20.0, "peak": -5.0}],
                "params": {"noise_db": noise_db,
                           "min_silence_sec": min_silence,
                           "step_sec": step}}

    # 同じ条件で測った結果はそのまま使える
    def test_same_params_match(self):
        self.assertTrue(features.scan_matches(self._scan(), _settings()))

    # 閾値を変えられていたら測り直す (古い値で切らないため)
    def test_different_noise_db_does_not_match(self):
        self.assertFalse(
            features.scan_matches(self._scan(noise_db=-25), _settings()))

    # 最小無音長を変えられていても測り直す
    def test_different_min_silence_does_not_match(self):
        self.assertFalse(
            features.scan_matches(self._scan(min_silence=0.5), _settings()))

    # 1 秒値を持たない結果は使えない
    def test_empty_scan_does_not_match(self):
        self.assertFalse(features.scan_matches({"per_sec": [], "params": {}},
                                               _settings()))
        self.assertFalse(features.scan_matches(None, _settings()))

    # params が壊れていても落ちない
    def test_broken_params_does_not_match(self):
        broken = {"per_sec": [1], "params": {"noise_db": -30,
                                             "min_silence_sec": "x",
                                             "step_sec": 1.0}}
        self.assertFalse(features.scan_matches(broken, _settings()))


class FeatureModeTest(unittest.TestCase):

    # 1 パスの per_sec から作ったセルが volumedetect 版と**同じ TOP を選ぶ**
    # (§13-2: single_pass を既定にする前提の確認)
    def test_both_modes_select_the_same_top(self):
        # 盛り上がり (大きい音) を 90〜135 秒へ置いた 1 秒値を作る
        per_sec = []
        for second in range(180):
            loud = 90 <= second < 135
            per_sec.append({"rms": -12.0 if loud else -32.0,
                            "peak": -3.0 if loud else -20.0})
        scan = {"duration": 180.0, "silence_ranges": [], "per_sec": per_sec,
                "params": features.scan_params(_settings())}

        cell_sec = 45
        _duration, single_cells = features._cells_from_scan(scan, cell_sec)

        # per_cell 相当のセル (volumedetect が同じ区間を測ったとみなす)
        per_cell_cells = []
        for index in range(4):
            start, end = index * cell_sec, (index + 1) * cell_sec
            mean_db, max_db = features.region_levels(per_sec, start, end)
            per_cell_cells.append({"start": start, "end": end,
                                   "mean_db": mean_db, "max_db": max_db,
                                   "silence_ratio": 0.0,
                                   "w_count": 0, "comment_count": 0})

        self.assertEqual([c["start"] for c in single_cells],
                         [c["start"] for c in per_cell_cells])
        self.assertEqual(
            [round(c["mean_db"], 6) for c in single_cells],
            [round(c["mean_db"], 6) for c in per_cell_cells])
        # 大声セルの判定 (採点で TOP を決める材料) も一致する
        self.assertEqual(scoring.loud_threshold(single_cells, 0.8),
                         scoring.loud_threshold(per_cell_cells, 0.8))

    # セルの無音率は無音区間の按分で求める (1 パスでも従来と同じ規則)
    def test_silence_ratio_is_prorated(self):
        per_sec = [{"rms": -20.0, "peak": -5.0} for _ in range(90)]
        scan = {"duration": 90.0, "silence_ranges": [(0.0, 22.5)],
                "per_sec": per_sec, "params": features.scan_params(_settings())}
        _duration, cells = features._cells_from_scan(scan, 45)
        self.assertAlmostEqual(cells[0]["silence_ratio"], 0.5)
        self.assertAlmostEqual(cells[1]["silence_ratio"], 0.0)

    # 1 パスが使えない構成では従来の per_cell 経路へ落ち、scan は None になる
    def test_scan_failure_falls_back_to_per_cell(self):
        calls = []

        def _boom(_path, _settings, progress_cb=None):
            calls.append(True)
            raise RuntimeError("astats 不可")

        def _no_duration(_path, _cfg):
            return 0.0

        self.addCleanup(setattr, features, "scan_audio", features.scan_audio)
        self.addCleanup(setattr, features.ffmpeg_runner, "probe_duration",
                        features.ffmpeg_runner.probe_duration)
        features.scan_audio = _boom
        features.ffmpeg_runner.probe_duration = _no_duration

        duration, cells, scan = features.extract_cells("vod.mp4", _settings(), 45)
        self.assertTrue(calls)
        self.assertEqual((duration, cells, scan), (0.0, [], None))

    # per_cell を選んだときは 1 パスを呼ばない
    def test_per_cell_mode_does_not_scan(self):
        calls = []

        def _boom(_path, _settings, progress_cb=None):
            calls.append(True)
            raise AssertionError("per_cell では呼ばれてはならない")

        def _no_duration(_path, _cfg):
            return 0.0

        self.addCleanup(setattr, features, "scan_audio", features.scan_audio)
        self.addCleanup(setattr, features.ffmpeg_runner, "probe_duration",
                        features.ffmpeg_runner.probe_duration)
        features.scan_audio = _boom
        features.ffmpeg_runner.probe_duration = _no_duration

        features.extract_cells("vod.mp4", _settings(mode="per_cell"), 45)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
