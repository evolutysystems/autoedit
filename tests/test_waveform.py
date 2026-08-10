# 音声クリップの波形データ (src/gui/timeline/waveform.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・生 PCM をバケツごとのピーク・RMS へ正しく畳めること
#   ・素材内の時刻 → 列 の対応がずれないこと (ずれると波形と音が食い違う)
#   ・取得途中でも、取れている範囲だけを返すこと
#   ・numpy が無い環境でも同じピークが出ること
import os
import struct
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from src.gui.timeline import waveform
    _QT_AVAILABLE = True
except ImportError:  # pragma: no cover (PySide6 が無い環境)
    _QT_AVAILABLE = False


# 振幅 amplitudes (0.0〜1.0) を 1 バケツぶんずつ並べた s16le のバイト列を作る
def _pcm(amplitudes, samples_per_bucket):
    values = []
    for amplitude in amplitudes:
        level = int(round(amplitude * 32767))
        # バケツ内で +level と -level を交互に置く (ピーク = level / RMS = level)
        values.extend([level, -level] * (samples_per_bucket // 2))
    return struct.pack("<%dh" % len(values), *values)


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が無い環境ではスキップ")
class ReduceTest(unittest.TestCase):

    def test_peak_and_rms(self):
        peak, rms = waveform._reduce(_pcm([1.0, 0.5, 0.0], 80), 80)
        self.assertEqual(len(peak), 3)
        for index, expected in enumerate((1.0, 0.5, 0.0)):
            self.assertAlmostEqual(float(peak[index]), expected, places=3)
            self.assertAlmostEqual(float(rms[index]), expected, places=3)

    # 端数 (バケツに満たない残り) は切り捨てる = 呼び出し側が次の塊へ繰り越す
    def test_ignores_partial_bucket(self):
        raw = _pcm([1.0, 1.0], 80) + struct.pack("<10h", *([100] * 10))
        peak, _rms = waveform._reduce(raw, 80)
        self.assertEqual(len(peak), 2)

    def test_returns_none_when_too_short(self):
        peak, rms = waveform._reduce(struct.pack("<4h", 1, 2, 3, 4), 80)
        self.assertIsNone(peak)
        self.assertIsNone(rms)


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が無い環境ではスキップ")
class ColumnsTest(unittest.TestCase):

    # 1 秒 = 10 バケツ。0.0〜0.9 の階段を 3 秒ぶん入れる
    def _peaks(self, seconds=3):
        peaks = waveform.Peaks(10)
        for _ in range(seconds):
            peak, rms = waveform._reduce(
                _pcm([index / 10.0 for index in range(10)], 80), 80)
            peaks.append(peak, rms)
        return peaks

    # 1 列 = 1 バケツのとき、その時刻の値がそのまま出る
    def test_maps_source_seconds(self):
        columns, _rms = self._peaks().columns(1.0, 2.0, 10)
        self.assertEqual(len(columns), 10)
        for index in range(10):
            self.assertAlmostEqual(float(columns[index]), index / 10.0, places=3)

    # 縮小 (1 列に複数バケツ) では、その範囲の最大値を採る = 大きい音が消えない
    def test_takes_maximum_when_shrunk(self):
        columns, _rms = self._peaks().columns(0.0, 1.0, 2)
        self.assertEqual(len(columns), 2)
        self.assertAlmostEqual(float(columns[0]), 0.4, places=3)
        self.assertAlmostEqual(float(columns[1]), 0.9, places=3)

    # 拡大 (1 バケツが複数列) では、同じ値が並ぶ (隙間ができない)
    def test_repeats_when_zoomed_in(self):
        columns, _rms = self._peaks().columns(1.0, 1.2, 8)
        self.assertEqual(len(columns), 8)
        self.assertAlmostEqual(float(columns[0]), 0.0, places=3)
        self.assertAlmostEqual(float(columns[-1]), 0.1, places=3)

    # 取得途中: 持っている範囲までしか返さない (残りは取得後に描き足す)
    def test_truncates_beyond_available(self):
        peaks = self._peaks(seconds=1)
        columns, _rms = peaks.columns(0.0, 2.0, 20)
        self.assertEqual(len(columns), 10)

    def test_returns_none_before_any_data(self):
        self.assertIsNone(waveform.Peaks(10).columns(0.0, 1.0, 10))
        self.assertIsNone(self._peaks(seconds=1).columns(5.0, 6.0, 10))

    def test_can_skip_rms(self):
        _columns, rms = self._peaks().columns(0.0, 1.0, 10, with_rms=False)
        self.assertIsNone(rms)


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が無い環境ではスキップ")
class NoNumpyTest(unittest.TestCase):

    # numpy の有無で結果が変わらないこと (無い環境では RMS を出さない)
    def test_same_peaks_without_numpy(self):
        amplitudes = [0.1, 0.9, 0.3, 0.0, 0.6, 0.2, 1.0, 0.4, 0.5, 0.7]
        raw = _pcm(amplitudes, 80)
        expected, _rms = waveform._reduce(raw, 80)
        expected = [round(float(v), 3) for v in expected]

        saved = waveform._np
        waveform._np = None
        try:
            peak, rms = waveform._reduce(raw, 80)
            self.assertIsNone(rms)
            self.assertEqual([round(float(v), 3) for v in peak], expected)

            peaks = waveform.Peaks(10)
            peaks.append(peak, rms)
            columns, rms_columns = peaks.columns(0.0, 1.0, 5)
            self.assertIsNone(rms_columns)
            # 2 バケツずつの最大値
            self.assertEqual(
                [round(float(v), 3) for v in columns],
                [round(max(amplitudes[i * 2:i * 2 + 2]), 3) for i in range(5)])
        finally:
            waveform._np = saved


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が無い環境ではスキップ")
class ConfigTest(unittest.TestCase):

    # 既定値 (setting.json に waveform 節が無くても動くこと)
    def test_defaults(self):
        from src.timeline.builder import timeline_config
        cfg = timeline_config({})["waveform"]
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["scale"], "linear")
        self.assertGreater(cfg["sample_rate"], 0)
        self.assertGreater(cfg["resolution_hz"], 0)

    def test_reads_settings(self):
        from src.timeline.builder import timeline_config
        cfg = timeline_config({"timeline": {"waveform": {
            "enabled": False, "scale": "LOG", "resolution_hz": 50}}})["waveform"]
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["scale"], "log")
        self.assertEqual(cfg["resolution_hz"], 50)

    # 不正値は既定へ落とす (画面が開かなくなるより、波形が粗い方がまし)
    def test_invalid_values_fall_back(self):
        from src.timeline.builder import timeline_config
        cfg = timeline_config({"timeline": {"waveform": {
            "sample_rate": 0, "resolution_hz": "x"}}})["waveform"]
        self.assertEqual(cfg["sample_rate"], 8000)
        self.assertEqual(cfg["resolution_hz"], 100)


if __name__ == "__main__":
    unittest.main()
