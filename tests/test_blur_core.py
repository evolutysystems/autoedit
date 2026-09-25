# トラッキングぼかしの中核 (src/blur/) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve8.md §8.1):
#   ・設定の読み取りが壊れた値でも既定へ落ちること (§7)
#   ・素材 → キャンバス → 正規化座標の変換が縦動画でもずれないこと (§4-3)
#   ・YOLOX のグリッド復元と NMS が仕様どおり動くこと (追従の助けとして使う)
#   ・モデルが無い環境でも例外にせず、理由を返すこと (§5.13)
# ver5 resolve8 で人物の自動検出による指定・ReID・輪郭を廃止したため、
# クラスタリング / ReID / 囲みの当て込みのテストは削除した。
# 指定の判定は test_blur_plan.py、追従は test_blur_track.py にある。
import unittest

import numpy as np

from src.blur import detector, geometry
from src.blur.config import config


# 素材 1 本ぶんの最小限のメディア (geometry が見るのは width / height だけ)
class _Media:

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.id = "m1"


class ConfigTest(unittest.TestCase):

    # 設定が空でも既定で動くこと
    def test_defaults(self):
        cfg = config({})
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["render"]["shape"], "rect")
        self.assertEqual(cfg["model"]["detector_input"], 416)
        self.assertEqual(cfg["track"]["sample_fps"], 10.0)
        self.assertEqual(cfg["editor"]["step_frames"], 10)

    # 廃止したセクションが残っていても無害であること
    def test_removed_sections_are_ignored(self):
        cfg = config({"blur": {"silhouette": {"points": 64}, "region": {"search_scale": 0.5}}})
        self.assertNotIn("silhouette", cfg)
        self.assertNotIn("region", cfg)

    def test_detector_input_is_multiple_of_32(self):
        self.assertEqual(config({"blur": {"model": {"detector_input": 600}}})
                         ["model"]["detector_input"], 608)


class GeometryTest(unittest.TestCase):

    # 素材とキャンバスの規格が同じなら恒等変換になること
    def test_identity_when_same_size(self):
        transform = geometry.source_to_canvas_transform(_Media(1920, 1080), 1920, 1080)
        self.assertEqual(transform, (1.0, 0.0, 0.0))

    # 横 1920x1080 の素材を縦 1080x1920 のキャンバスへ入れると
    # 幅いっぱいに縮小され、上下へ均等な黒帯が入ること
    def test_vertical_canvas_letterbox(self):
        scale, offset_x, offset_y = geometry.source_to_canvas_transform(
            _Media(1920, 1080), 1080, 1920)
        self.assertAlmostEqual(scale, 1080 / 1920)
        self.assertAlmostEqual(offset_x, 0.0)
        # (1920 - 1080 * 1080/1920) / 2 = 656.25
        self.assertAlmostEqual(offset_y, (1920 - 1080 * (1080 / 1920)) / 2)

        # 素材の中心は、キャンバスでも中心に来る
        rect = geometry.source_rect_to_canvas((960 - 50, 540 - 50, 100, 100),
                                              (scale, offset_x, offset_y))
        self.assertAlmostEqual(rect[0] + rect[2] / 2, 540.0)
        self.assertAlmostEqual(rect[1] + rect[3] / 2, 960.0)

    # 往復変換で元へ戻ること
    def test_round_trip(self):
        transform = geometry.source_to_canvas_transform(_Media(1280, 720), 1920, 1080)
        source = (100.0, 200.0, 300.0, 400.0)
        canvas = geometry.source_rect_to_canvas(source, transform)
        back = geometry.canvas_rect_to_source(canvas, transform)
        for expected, actual in zip(source, back):
            self.assertAlmostEqual(expected, actual, places=6)

    # 正規化座標の往復 (指定の保存形式 / §4-3)
    def test_normalized_rect_round_trip(self):
        rect = (0.25, 0.5, 0.2, 0.1)
        canvas = geometry.normalized_rect_to_canvas(rect, 1920, 1080)
        self.assertEqual(canvas, (480.0, 540.0, 384.0, 108.0))
        back = geometry.canvas_rect_to_normalized(canvas, 1920, 1080)
        for expected, actual in zip(rect, back):
            self.assertAlmostEqual(expected, actual, places=6)

    # 正規化座標 ↔ 素材ピクセル (レターボックスを跨いでも往復すること)
    def test_normalized_to_source_round_trip(self):
        transform = geometry.source_to_canvas_transform(_Media(1920, 1080), 1080, 1920)
        rect = (0.25, 0.4, 0.5, 0.2)
        source = geometry.normalized_rect_to_source(rect, transform, 1080, 1920)
        back = geometry.source_rect_to_normalized(source, transform, 1080, 1920)
        for expected, actual in zip(rect, back):
            self.assertAlmostEqual(expected, actual, places=6)

    # 画面の外へドラッグされた矩形は内側へ収めること
    def test_clamp_normalized_rect(self):
        self.assertEqual(geometry.clamp_normalized_rect((-0.2, 0.9, 0.4, 0.3)),
                         (0.0, 0.7, 0.4, 0.3))

    def test_iou(self):
        self.assertAlmostEqual(geometry.iou((0, 0, 10, 10), (0, 0, 10, 10)), 1.0)
        self.assertAlmostEqual(geometry.iou((0, 0, 10, 10), (20, 20, 5, 5)), 0.0)
        # 半分だけ重なる: 交差 50 / 和 150
        self.assertAlmostEqual(geometry.iou((0, 0, 10, 10), (5, 0, 10, 10)), 50 / 150)


class DetectorPostprocessTest(unittest.TestCase):

    # YOLOX のグリッド復元
    #   x = (out_x + grid_x) * stride / w = exp(out_w) * stride
    def test_yolox_grid_decode(self):
        size = 64                       # 8x8 + 4x4 + 2x2 = 84 格子
        cells = (size // 8) ** 2 + (size // 16) ** 2 + (size // 32) ** 2
        predictions = np.zeros((cells, 85), dtype=np.float32)
        # 先頭の格子 (grid 0,0 / stride 8) に「中心 0.5・大きさ exp(0)=1」を置く
        predictions[0, 0] = 0.5
        predictions[0, 1] = 0.5
        predictions[0, 4] = 1.0         # objectness
        predictions[0, 5] = 1.0         # person クラス

        boxes = detector._decode(predictions, size, "yolox", 0.5)   # noqa: SLF001
        self.assertEqual(len(boxes), 1)
        # 中心 (0.5 + 0) * 8 = 4.0 / 大きさ exp(0) * 8 = 8.0 → 左上 (0, 0)・右下 (8, 8)
        self.assertAlmostEqual(float(boxes[0][0]), 0.0, places=4)
        self.assertAlmostEqual(float(boxes[0][1]), 0.0, places=4)
        self.assertAlmostEqual(float(boxes[0][2]), 8.0, places=4)
        self.assertAlmostEqual(float(boxes[0][3]), 8.0, places=4)

    # person 以外のクラスは捨てること
    def test_non_person_is_dropped(self):
        size = 64
        cells = (size // 8) ** 2 + (size // 16) ** 2 + (size // 32) ** 2
        predictions = np.zeros((cells, 85), dtype=np.float32)
        predictions[0, 4] = 1.0
        predictions[0, 10] = 1.0        # person (index 5) ではないクラス
        self.assertEqual(len(detector._decode(predictions, size, "yolox", 0.5)), 0)  # noqa: SLF001

    # スコアが下限に満たない検出は捨てること
    def test_low_score_is_dropped(self):
        size = 64
        cells = (size // 8) ** 2 + (size // 16) ** 2 + (size // 32) ** 2
        predictions = np.zeros((cells, 85), dtype=np.float32)
        predictions[0, 4] = 0.5
        predictions[0, 5] = 0.5         # obj x cls = 0.25
        self.assertEqual(len(detector._decode(predictions, size, "yolox", 0.4)), 0)  # noqa: SLF001

    # NMS が重なった検出を 1 つに絞ること
    def test_nms(self):
        boxes = np.array([
            [0, 0, 10, 10, 0.9],
            [1, 1, 11, 11, 0.8],        # ほぼ同じ場所 = 消える
            [100, 100, 110, 110, 0.7],  # 離れている = 残る
        ], dtype=np.float32)
        kept = detector._nms(boxes, 0.5)    # noqa: SLF001
        self.assertEqual(len(kept), 2)
        self.assertAlmostEqual(float(kept[0][4]), 0.9)
        self.assertAlmostEqual(float(kept[1][4]), 0.7)

    # YOLOX の前処理: BGR のまま / 0〜255 のまま / 余白は 114
    # ここを間違えると、モデルは動くのに検出が出ないという分かりにくい壊れ方をする。
    def test_yolox_preprocess(self):
        image = np.zeros((100, 200, 3), dtype=np.uint8)
        image[:, :, 0] = 10     # R
        image[:, :, 1] = 20     # G
        image[:, :, 2] = 30     # B

        blob, scale = detector._preprocess(image, 64, 114, "yolox")     # noqa: SLF001
        self.assertEqual(blob.shape, (1, 3, 64, 64))
        self.assertAlmostEqual(scale, 64 / 200)             # 長辺 200 に合わせる

        # 先頭チャンネルは B (BGR へ入れ替わっている)。正規化していないため 30 のまま。
        self.assertAlmostEqual(float(blob[0, 0, 0, 0]), 30.0)
        self.assertAlmostEqual(float(blob[0, 2, 0, 0]), 10.0)
        # 余白 (右下) は 114。BGR へ入れ替えても値は同じ。
        self.assertAlmostEqual(float(blob[0, 0, 63, 63]), 114.0)


class ModelMissingTest(unittest.TestCase):

    # モデルが無い環境でも例外にせず、空の結果を返すこと (§5.13 / §4-5)
    # これが守られないと、モデル未配置のまま追従を走らせただけで落ちる。
    def test_detect_without_model(self):
        cfg = config({"blur": {"model": {"detector": "models/does_not_exist.onnx"}}})
        image = np.zeros((64, 64, 3), dtype=np.uint8)
        self.assertEqual(detector.detect(image, cfg), [])

    # モデルが見つからないことを、理由付きで報告できること (設定画面へ出す文言)
    def test_availability_reports_reason(self):
        from src.blur import models

        # 既定のパスだと、モデルを配置した開発環境で結果が変わるため存在しないパスを指す
        cfg = config({"blur": {"model": {"detector": "models/does_not_exist.onnx"}}})
        available, reason = models.availability(cfg)
        self.assertFalse(available)
        self.assertIn("モデルが見つかりません", reason)
        # 機能そのものは止まらない = 代わりに何をするかを伝える
        self.assertIn("模様の変化", reason)


if __name__ == "__main__":
    unittest.main()
