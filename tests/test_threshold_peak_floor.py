# ver11 resolve1 の単体テスト — カット閾値の自動推定とピーク音量の下限
# 実行: python -m unittest discover -s tests
#
# 何を守るテストか (docs/request/ver11/resolve1.md §4.2):
#   ・ピーク下限が無いと、ピークが立たない定常ノイズが発話候補に混ざって
#     カット閾値がノイズフロアまで落ちる (問題の再現)
#   ・ピーク下限を入れると発話区間だけが母集団に残る
#   ・全区間が下限未満でも諦めず、従来どおり全区間で算出する (安全弁)
#   ・設定が無い / null なら従来と同じ値になる (切り戻し経路)
#
# 1 パス解析 (scan) の経路を使うため **ffmpeg を 1 回も起動しない**。
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.modules import volume_analyzer

# 1 秒ごとの (rms_db, peak_db)
# 発話はピークが立つ / 定常ノイズは立たない (実測の S5 に合わせた値)
_SPEECH = (-18.0, -2.0)
_NOISE = (-38.0, -28.0)

# 区間の区切りに入れる無音 (build_keep_segments で 6 区間に割れる)
_SILENCE_RANGES = [(5.0, 5.5), (10.0, 10.5), (15.0, 15.5),
                   (20.0, 20.5), (25.0, 25.5)]
_DURATION = 30.0


def _settings(**overrides):
    volume_analysis = {
        "base_noise_db": -30,
        "base_min_silence_sec": 0.3,
        "min_region_sec": 0.8,
        "max_regions": 100,
        "metric": "percentile",
        "percentile": 10,
    }
    volume_analysis.update(overrides)
    return {"volume_analysis": volume_analysis, "ffmpeg": {}}


# kinds は 5 秒ぶんずつの種類 ("s" = 発話 / "n" = 定常ノイズ)
def _scan(kinds):
    per_sec = []
    for kind in kinds:
        value = _SPEECH if kind == "s" else _NOISE
        per_sec.extend([{"rms": value[0], "peak": value[1]}] * 5)
    return {
        "per_sec": per_sec,
        "duration": _DURATION,
        "silence_ranges": list(_SILENCE_RANGES),
    }


class ThresholdPeakFloorTest(unittest.TestCase):

    def setUp(self):
        # analyze_min_speech_db は入力の存在だけ確かめる (scan があれば読まない)
        handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        handle.close()
        self.path = handle.name

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _analyze(self, kinds, **overrides):
        return volume_analyzer.analyze_min_speech_db(
            self.path, _settings(**overrides), scan=_scan(kinds))

    # 問題の再現: 候補の半分がノイズだと、分位点がノイズ側へ落ちる
    def test_noise_regions_drag_threshold_down(self):
        result = self._analyze(["s", "n", "s", "n", "s", "n"])
        self.assertEqual(result["region_count"], 6)
        self.assertEqual(result["min_db"], -38)

    # ピーク下限を入れると発話区間だけが残る
    def test_peak_floor_excludes_noise_regions(self):
        result = self._analyze(["s", "n", "s", "n", "s", "n"],
                               min_region_peak_db=-20)
        self.assertEqual(result["min_db"], -18)
        self.assertEqual(result["measured_count"], 3)

    # 外した区間数が記録される
    def test_dropped_count_is_reported(self):
        result = self._analyze(["s", "n", "s", "n", "s", "n"],
                               min_region_peak_db=-20)
        self.assertEqual(result["dropped_quiet_count"], 3)

    # 下限の値を変えても、発話とノイズの間 (max -28 〜 -2) なら結果は同じ
    def test_floor_is_insensitive_within_the_gap(self):
        for floor in (-25, -20, -15, -10):
            with self.subTest(floor=floor):
                result = self._analyze(["s", "n", "s", "n", "s", "n"],
                                       min_region_peak_db=floor)
                self.assertEqual(result["min_db"], -18)

    # 安全弁: 全区間が下限未満なら、諦めず従来どおり全区間で算出する
    def test_all_regions_quiet_falls_back(self):
        result = self._analyze(["n", "n", "n", "n", "n", "n"],
                               min_region_peak_db=-20)
        self.assertEqual(result["min_db"], -38)
        self.assertEqual(result["measured_count"], 6)
        self.assertEqual(result["dropped_quiet_count"], 0)

    # 切り戻し: null / キー無しでは従来と同じ値になる
    def test_absent_setting_keeps_legacy_behaviour(self):
        legacy = self._analyze(["s", "n", "s", "n", "s", "n"])
        for value in (None, ""):
            with self.subTest(value=value):
                result = self._analyze(["s", "n", "s", "n", "s", "n"],
                                       min_region_peak_db=value)
                self.assertEqual(result["min_db"], legacy["min_db"])
                self.assertEqual(result["dropped_quiet_count"], 0)

    # 発話だけの素材では、下限があっても 1 区間も外れない (正常な素材を壊さない)
    def test_speech_only_material_is_untouched(self):
        without = self._analyze(["s", "s", "s", "s", "s", "s"])
        with_floor = self._analyze(["s", "s", "s", "s", "s", "s"],
                                   min_region_peak_db=-20)
        self.assertEqual(with_floor["min_db"], without["min_db"])
        self.assertEqual(with_floor["dropped_quiet_count"], 0)

    # 不正値は既定 (-20) として扱う (ハードコードせず _to_float 経由)
    def test_invalid_setting_uses_default(self):
        result = self._analyze(["s", "n", "s", "n", "s", "n"],
                               min_region_peak_db="あ")
        self.assertEqual(result["min_db"], -18)


if __name__ == "__main__":
    unittest.main()
