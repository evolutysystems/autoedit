# 囲みの形 (src/blur/contour.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve8.md §3.6):
#   ・枠に対する相対座標の往復で形が崩れないこと
#   ・量子化 (base64 / 1 点 2 バイト) の往復で誤差が 1/255 以内であること
#   ・壊れた文字列でも落ちないこと
# ver5 resolve8 で身体の輪郭 (silhouette) を廃止し、新しい囲みは矩形だけになったため、
# ここを使うのは「v4 以前の自由な囲みを引き継いだ指定」の表示・塗りだけである。
import math
import unittest

import numpy as np

from src.blur import contour


def _circle(count, radius=0.4):
    points = []
    for index in range(count):
        theta = 2.0 * math.pi * index / count
        points.append((0.5 + radius * math.cos(theta), 0.5 + radius * math.sin(theta)))
    return points


class ContourTest(unittest.TestCase):

    # 量子化の往復で誤差が 1 段 (1/255) 以内であること
    def test_encode_round_trip(self):
        shape = _circle(64)
        decoded = contour.decode(contour.encode(shape))
        self.assertEqual(len(decoded), 64)
        error = np.abs(np.asarray(decoded) - np.asarray(shape)).max()
        self.assertLessEqual(error, 1.0 / 255 + 1e-9)

    # 壊れた文字列は None になること (プロジェクトの破損で落とさない)
    def test_decode_broken(self):
        self.assertIsNone(contour.decode("###"))
        self.assertIsNone(contour.decode(""))
        self.assertIsNone(contour.decode(contour.encode([[0.1, 0.1]])))   # 点が少なすぎる

    # 枠に対する相対座標の往復で元へ戻ること
    def test_relative_round_trip(self):
        rect = (100.0, 50.0, 200.0, 400.0)
        points = [(150.0, 60.0), (290.0, 440.0), (110.0, 300.0)]
        back = contour.to_absolute(contour.to_relative(points, rect), rect)
        self.assertTrue(np.allclose(np.asarray(back), np.asarray(points)))

    # 枠の外へはみ出た点は枠へ寄せること
    def test_relative_clamps(self):
        relative = contour.to_relative([(-50.0, 500.0)], (0.0, 0.0, 100.0, 100.0))
        self.assertEqual(relative, [[0.0, 1.0]])

    # 枠を動かす・大きさを変えると、形も一緒に付いてくること (移動・拡大縮小の土台)
    def test_shape_follows_the_rect(self):
        relative = contour.to_relative([(10.0, 10.0), (30.0, 10.0), (20.0, 30.0)],
                                       (0.0, 0.0, 40.0, 40.0))
        moved = contour.to_absolute(relative, (100.0, 200.0, 80.0, 80.0))
        self.assertTrue(np.allclose(np.asarray(moved),
                                    [[120.0, 220.0], [160.0, 220.0], [140.0, 260.0]]))


if __name__ == "__main__":
    unittest.main()
