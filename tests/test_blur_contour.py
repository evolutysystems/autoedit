# 輪郭の扱い (src/blur/contour.py) と輪郭の取り出し (src/blur/silhouette.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve3.md §3.3 / §5.3 / §8.1):
#   ・打ち直し・始点揃え・量子化の往復で形が崩れないこと
#   ・前後の輪郭を混ぜるとき、始点が輪郭の別の場所へ飛んでいても形がねじれないこと
#   ・マスクの外周をたどると、くびれや凹みのある形でも一周して閉じること
#   ・輪郭モデルが無ければ「使えない」と理由付きで返すこと (矩形へ落とす / §3.3)
import math
import unittest

import numpy as np

from src.blur import contour, silhouette
from src.blur.config import config


def _circle(count, radius=0.4, offset=0):
    points = []
    for index in range(count):
        theta = 2.0 * math.pi * (index + offset) / count
        points.append((0.5 + radius * math.cos(theta), 0.5 + radius * math.sin(theta)))
    return points


class ContourTest(unittest.TestCase):

    # 打ち直すと指定した点数になり、面積がほぼ保たれること
    def test_resample_keeps_area(self):
        square = [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)]
        resampled = contour.resample(square, 64)
        self.assertEqual(len(resampled), 64)
        self.assertAlmostEqual(contour.area(resampled), 0.64, delta=0.01)

    # 始点が一番上の点になり、向きが時計回り (画面座標で符号付き面積が正) にそろうこと
    def test_align_starts_at_top_clockwise(self):
        counter_clockwise = list(reversed(_circle(32)))
        aligned = contour.align(counter_clockwise)
        ys = [p[1] for p in aligned]
        self.assertAlmostEqual(aligned[0][1], min(ys))
        self.assertGreater(contour._signed_area(np.asarray(aligned)), 0)

    # 量子化の往復で、座標の誤差が 1 段 (1/255) 以内に収まること
    def test_encode_round_trip(self):
        shape = contour.normalize(_circle(64), 64)
        decoded = contour.decode(contour.encode(shape))
        self.assertEqual(len(decoded), 64)
        error = np.abs(np.asarray(decoded) - np.asarray(shape)).max()
        self.assertLessEqual(error, 1.0 / 255 + 1e-9)

    # 壊れた文字列は None になること (解析結果の破損で落とさない)
    def test_decode_broken(self):
        self.assertIsNone(contour.decode("###"))
        self.assertIsNone(contour.decode(""))

    # 始点がずれた同じ形を混ぜても、形がねじれないこと (実素材で顔がぼけた不具合の再発防止)
    def test_interpolate_aligns_shifted_start(self):
        before = _circle(64)
        after = _circle(64, offset=17)          # 同じ円を 17 点ずらしたもの
        middle = np.asarray(contour.interpolate(before, after, 0.5))
        radius = np.hypot(middle[:, 0] - 0.5, middle[:, 1] - 0.5)
        self.assertLess(float(np.abs(radius - 0.4).max()), 1e-6)

    # 外側へ膨らませた多角形: 四角は辺ごとに広がり、円は半径が増える。向きが逆でも外側へ膨らむ
    def test_offset_grows_outward(self):
        square = [(10, 10), (30, 10), (30, 30), (10, 30)]
        self.assertAlmostEqual(contour.area(contour.offset(square, 5)), 900.0, delta=1e-6)
        self.assertAlmostEqual(contour.area(contour.offset(list(reversed(square)), 5)), 900.0,
                               delta=1e-6)
        grown = np.asarray(contour.offset([(p[0] * 40, p[1] * 40) for p in _circle(64)], 3))
        radius = np.hypot(grown[:, 0] - 20, grown[:, 1] - 20)
        self.assertTrue(np.allclose(radius, 16.0 + 3.0, atol=0.05))
        # 0 以下なら何もしない
        self.assertEqual(contour.offset(square, 0), [list(p) for p in square])

    # 相対座標と絶対座標の往復
    def test_relative_round_trip(self):
        rect = (100.0, 50.0, 200.0, 400.0)
        points = [(150.0, 60.0), (290.0, 440.0), (110.0, 300.0)]
        back = contour.to_absolute(contour.to_relative(points, rect), rect)
        self.assertTrue(np.allclose(np.asarray(back), np.asarray(points)))


class BoundaryTraceTest(unittest.TestCase):

    def _assert_traces(self, region):
        boundary = silhouette._trace_boundary(region)
        self.assertEqual(len(boundary), len(set(boundary)), "外周に同じ画素が 2 回出ています")
        points = [(col + 0.5, row + 0.5) for row, col in boundary]
        # 外周の多角形の面積は、塊の画素数に近い (外周の半画素ぶん小さい)
        self.assertGreater(contour.area(points), region.sum() * 0.8)

    def test_rectangle(self):
        region = np.zeros((40, 60), dtype=bool)
        region[5:30, 10:50] = True
        self._assert_traces(region)

    # 始点へ戻っただけで止めると、円で 3 点しかたどらなかった (Jacob の停止条件の確認)
    def test_circle(self):
        yy, xx = np.mgrid[0:80, 0:80]
        self._assert_traces((yy - 40) ** 2 + (xx - 40) ** 2 < 30 ** 2)

    def test_concave_shape(self):
        region = np.zeros((50, 50), dtype=bool)
        region[5:45, 5:15] = True
        region[5:45, 35:45] = True
        region[35:45, 5:45] = True
        self._assert_traces(region)

    # 斜め 1 画素の線のような退化した形でも回り続けないこと
    def test_degenerate_diagonal_stops(self):
        region = np.zeros((10, 10), dtype=bool)
        region[2, 2] = region[3, 3] = region[4, 4] = True
        # 上限 (画素数 x 4 + 8 回) ぶん進んだところで止まる (始点を含めて +1)
        self.assertLessEqual(len(silhouette._trace_boundary(region)), 3 * 4 + 8 + 1)

    # マスク → 輪郭: 枠の外へ伸びた部分は枠で切られ、小さすぎれば None
    def test_mask_to_contour(self):
        cfg = config({})
        mask = np.zeros((90, 160), dtype=bool)
        mask[20:80, 40:80] = True               # 枠 (マスク座標 40〜80, 20〜80) にぴったりの塊
        mask[0:5, 0:5] = True                   # 枠の外の点は拾わない
        scale = 160 / 1920.0
        box = (40 / scale, 20 / scale, 40 / scale, 60 / scale, 0.9)
        shape = silhouette._to_contour(mask, box, scale, 90, 160, cfg["silhouette"])
        self.assertIsNotNone(shape)
        self.assertEqual(len(shape), cfg["silhouette"]["points"])
        self.assertGreater(contour.area(shape), 0.8)

        empty = np.zeros((90, 160), dtype=bool)
        self.assertIsNone(silhouette._to_contour(empty, box, scale, 90, 160, cfg["silhouette"]))


class SilhouetteAvailabilityTest(unittest.TestCase):

    # モデルが見つからなければ、理由付きで使えないと返すこと (矩形へ落とす)
    def test_missing_model(self):
        cfg = config({"blur": {"silhouette": {"model": "models/__missing__.onnx"}}})
        available, reason = silhouette.availability(cfg)
        self.assertFalse(available)
        self.assertIn("四角", reason)

    # 未知の形式を指定しても、設定の読み取りで既定 (mobilesam) へ落ちること
    def test_unknown_format_falls_back(self):
        cfg = config({"blur": {"silhouette": {"format": "unknown"}}})
        self.assertEqual(cfg["silhouette"]["format"], "mobilesam")


if __name__ == "__main__":
    unittest.main()
