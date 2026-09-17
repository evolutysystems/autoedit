# トラッキングぼかしの中核 (src/blur/) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve2.md §6 の完了条件):
#   ・設定の読み取りが壊れた値でも既定へ落ちること (§7 / R11)
#   ・素材 → キャンバスの座標変換が縦動画でもずれないこと (§2.5 / Phase 3)
#   ・指定の判定が「明示 > 主役 > 既定方針」の順になること (§5.2.2 / R3)
#   ・囲みの当て込みが中心判定と重なり判定の両方で効くこと (§5.6.3)
#   ・YOLOX のグリッド復元と NMS が仕様どおり動くこと (§3.7.1)
#   ・人物 ID の採番が決定的で、主役が正しく選ばれること (§3.3 / §8-7)
import unittest

import numpy as np

from src.archive.timeline_builder import ORIGIN_ARCHIVE_INDEX
from src.blur import decisions as blur_decisions
from src.blur import detector, geometry, region_tracker, tracker
from src.blur.config import config
from src.timeline.model import Clip


# 素材 1 本ぶんの最小限のメディア (geometry が見るのは width / height だけ)
class _Media:

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.id = "m1"


class ConfigTest(unittest.TestCase):

    # 設定が空でも既定で動くこと (R11)
    def test_defaults(self):
        cfg = config({})
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["default_policy"], "blur_others")
        self.assertEqual(cfg["model"]["detector_input"], 416)
        self.assertEqual(cfg["model"]["reid_input"], [128, 256])

    # 壊れた値・範囲外の値は既定または範囲内へ落ちること (画面を落とさない)
    def test_broken_values_fall_back(self):
        cfg = config({"blur": {
            "default_policy": "なにか変な値",
            "model": {"detector_input": "abc", "detector_score": 99, "reid_input": [1]},
            "render": {"mode": "unknown", "strength": 1000, "mask_scale": 0},
            "analysis": {"sample_fps": -5},
        }})
        self.assertEqual(cfg["default_policy"], "blur_others")
        self.assertEqual(cfg["model"]["detector_input"], 416)
        self.assertLessEqual(cfg["model"]["detector_score"], 0.99)
        self.assertEqual(cfg["model"]["reid_input"], [128, 256])
        self.assertEqual(cfg["render"]["mode"], "gaussian")
        self.assertEqual(cfg["render"]["strength"], 100)
        self.assertGreater(cfg["render"]["mask_scale"], 0.0)
        self.assertGreaterEqual(cfg["analysis"]["sample_fps"], 0.5)

    # 入力の一辺は 32 の倍数へそろうこと (検出器の格子と合わせるため)
    def test_detector_input_is_multiple_of_32(self):
        self.assertEqual(config({"blur": {"model": {"detector_input": 600}}})
                         ["model"]["detector_input"], 608)


class GeometryTest(unittest.TestCase):

    # 素材とキャンバスの規格が同じなら恒等変換になること
    def test_identity_when_same_size(self):
        transform = geometry.source_to_canvas_transform(_Media(1920, 1080), 1920, 1080)
        self.assertEqual(transform, (1.0, 0.0, 0.0))

    # 横 1920x1080 の素材を縦 1080x1920 のキャンバスへ入れると
    # 幅いっぱいに縮小され、上下へ均等な黒帯が入ること (§2.5)
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

    # 正規化座標の往復 (囲みパスの保存形式 / §5.2.2)
    def test_normalized_path_round_trip(self):
        path = [(0.25, 0.5), (0.75, 0.5), (0.75, 0.9)]
        canvas = geometry.normalized_path_to_canvas(path, 1920, 1080)
        self.assertEqual(canvas[0], (480.0, 540.0))
        back = geometry.canvas_path_to_normalized(canvas, 1920, 1080)
        for (ex, ey), (ax, ay) in zip(path, back):
            self.assertAlmostEqual(ex, ax)
            self.assertAlmostEqual(ey, ay)

    def test_iou(self):
        self.assertAlmostEqual(geometry.iou((0, 0, 10, 10), (0, 0, 10, 10)), 1.0)
        self.assertAlmostEqual(geometry.iou((0, 0, 10, 10), (20, 20, 5, 5)), 0.0)
        # 半分だけ重なる: 交差 50 / 和 150
        self.assertAlmostEqual(geometry.iou((0, 0, 10, 10), (5, 0, 10, 10)), 50 / 150)

    def test_containment(self):
        # 小さい枠が大きい枠に収まっていれば 1.0 (IoU は 0.25 と低い)
        self.assertAlmostEqual(geometry.containment((0, 0, 20, 20), (5, 5, 10, 10)), 1.0)
        self.assertAlmostEqual(geometry.containment((0, 0, 10, 10), (20, 20, 5, 5)), 0.0)
        # 小さい方 (10x10) の半分が重なる
        self.assertAlmostEqual(geometry.containment((0, 0, 10, 10), (5, 0, 30, 30)), 0.5)

    def test_point_in_polygon(self):
        square = [(0, 0), (10, 0), (10, 10), (0, 10)]
        self.assertTrue(geometry.point_in_polygon((5, 5), square))
        self.assertFalse(geometry.point_in_polygon((15, 5), square))

    # 矩形の被覆率 (囲みの当て込みで使う)
    def test_rect_coverage(self):
        square = [(0, 0), (10, 0), (10, 10), (0, 10)]
        self.assertAlmostEqual(geometry.rect_coverage((0, 0, 10, 10), square), 1.0)
        self.assertAlmostEqual(geometry.rect_coverage((20, 20, 5, 5), square), 0.0)
        # 右半分がはみ出す = おおよそ半分
        self.assertAlmostEqual(geometry.rect_coverage((5, 0, 10, 10), square), 0.5, places=1)


class DecisionsTest(unittest.TestCase):

    def setUp(self):
        self.cfg = config({})

    # 明示指定が最優先 (主役でもぼかす)
    def test_explicit_wins(self):
        state = {"identities": {"p1": "blur"}, "regions": [], "default_policy": ""}
        self.assertTrue(
            blur_decisions.should_blur_identity("p1", state, self.cfg, main_id="p1"))

    # 主役は既定でぼかさない (R3)
    def test_main_identity_is_kept(self):
        state = {"identities": {}, "regions": [], "default_policy": "blur_others"}
        self.assertFalse(
            blur_decisions.should_blur_identity("p1", state, self.cfg, main_id="p1"))
        # 主役以外は既定方針 (blur_others) に従ってぼかす
        self.assertTrue(
            blur_decisions.should_blur_identity("p2", state, self.cfg, main_id="p1"))

    # manual_only なら囲ったものだけぼかす
    def test_manual_only_policy(self):
        state = {"identities": {}, "regions": [], "default_policy": "manual_only"}
        self.assertFalse(
            blur_decisions.should_blur_identity("p2", state, self.cfg, main_id="p1"))

    # 囲みの中心に入った人物が拾われること (§5.6.3-3)
    def test_identities_in_path_by_center(self):
        polygon = [(0, 0), (100, 0), (100, 100), (0, 100)]
        boxes = [
            {"identity": "p1", "rect": (40, 40, 20, 20)},     # 中心が内側
            {"identity": "p2", "rect": (500, 500, 20, 20)},   # 完全に外
        ]
        self.assertEqual(
            blur_decisions.identities_in_path(polygon, boxes, 0.5), ["p1"])

    # 中心が外でも、重なりが hit_ratio 以上なら拾われること
    def test_identities_in_path_by_coverage(self):
        polygon = [(0, 0), (100, 0), (100, 100), (0, 100)]
        # 中心 (105, 50) は囲みの外。重なりは 20/50 = 4 割。
        boxes = [{"identity": "p3", "rect": (80, 40, 50, 20)}]
        self.assertEqual(
            blur_decisions.identities_in_path(polygon, boxes, 0.3), ["p3"])
        # しきい値が重なりを上回れば拾わない
        self.assertEqual(
            blur_decisions.identities_in_path(polygon, boxes, 0.5), [])

    # 指定の書き換えは元の辞書を壊さないこと (Undo のため)
    def test_with_identity_is_pure(self):
        state = blur_decisions.load(_FakeTimeline({}))
        updated = blur_decisions.with_identity(state, "p1", "blur")
        self.assertEqual(state["identities"], {})
        self.assertEqual(updated["identities"], {"p1": "blur"})

    # 領域 ID は既存と重複しないこと
    def test_next_region_id(self):
        state = {"regions": [{"id": "r1"}, {"id": "r2"}]}
        self.assertEqual(blur_decisions.next_region_id(state), "r3")

    # クリップ用: 1 本の素材を無音カットで分けた区間は、それぞれ 1 セクションに数えること
    def test_count_sections_for_silence_cut(self):
        timeline = _FakeTimeline({}, [
            Clip("c1", "m1", 0.0, 0.3, source_in=10.9, source_out=11.2),
            Clip("c2", "m1", 0.3, 0.4, source_in=11.9, source_out=12.3),
            Clip("c3", "m1", 0.7, 4.0, source_in=13.8, source_out=17.8),
        ])
        tracks = [{"media_id": "m1", "start_sec": 10.9, "end_sec": 12.3},
                  {"media_id": "m1", "start_sec": 13.8, "end_sec": 17.6}]
        self.assertEqual(blur_decisions.count_sections(timeline, tracks), 3)
        # 映っていない区間は数えない
        self.assertEqual(blur_decisions.count_sections(timeline, tracks[1:]), 1)

    # アーカイブ用: 複数のベースクリップに分かれた clip{N} は 1 セクションに数えること
    def test_count_sections_for_archive(self):
        def origin(index):
            return {"type": "silence_cut", ORIGIN_ARCHIVE_INDEX: index}
        timeline = _FakeTimeline({}, [
            Clip("c1", "m1", 0.0, 5.0, source_in=0.0, source_out=5.0, origin=origin(1)),
            Clip("c2", "m1", 5.0, 5.0, source_in=8.0, source_out=13.0, origin=origin(1)),
            Clip("c3", "m2", 10.0, 5.0, source_in=0.0, source_out=5.0, origin=origin(2)),
        ])
        tracks = [{"media_id": "m1", "start_sec": 0.0, "end_sec": 12.0},
                  {"media_id": "m2", "start_sec": 1.0, "end_sec": 4.0}]
        self.assertEqual(blur_decisions.count_sections(timeline, tracks), 2)


class _FakeTimeline:

    def __init__(self, source, clips=()):
        self.source = dict(source)
        self._clips = list(clips)

    def base_clips(self):
        return list(self._clips)


class DetectorPostprocessTest(unittest.TestCase):

    # YOLOX のグリッド復元 (§3.7.1)
    #   x = (out_x + grid_x) * stride / w = exp(out_w) * stride
    def test_yolox_grid_decode(self):
        size = 64                       # 8x8 + 4x4 + 2x2 = 84 格子
        cells = (size // 8) ** 2 + (size // 16) ** 2 + (size // 32) ** 2
        predictions = np.zeros((cells, 85), dtype=np.float32)
        # 先頭の格子 (grid 0,0 / stride 8) に「中心 0.5・大きさ exp(0)=1」を置く
        predictions[0, 0] = 0.5
        predictions[0, 1] = 0.5
        predictions[0, 2] = 0.0
        predictions[0, 3] = 0.0
        predictions[0, 4] = 1.0         # objectness
        predictions[0, 5] = 1.0         # person クラス

        boxes = detector._decode(predictions, size, "yolox", 0.5)
        self.assertEqual(len(boxes), 1)
        # 中心 (0.5 + 0) * 8 = 4.0 / 大きさ exp(0) * 8 = 8.0 → 左上 (0, 0)・右下 (8, 8)
        self.assertAlmostEqual(float(boxes[0][0]), 0.0, places=4)
        self.assertAlmostEqual(float(boxes[0][1]), 0.0, places=4)
        self.assertAlmostEqual(float(boxes[0][2]), 8.0, places=4)
        self.assertAlmostEqual(float(boxes[0][3]), 8.0, places=4)

    # person 以外のクラスは捨てること (§3.1)
    def test_non_person_is_dropped(self):
        size = 64
        cells = (size // 8) ** 2 + (size // 16) ** 2 + (size // 32) ** 2
        predictions = np.zeros((cells, 85), dtype=np.float32)
        predictions[0, 4] = 1.0
        predictions[0, 10] = 1.0        # person (index 5) ではないクラス
        self.assertEqual(len(detector._decode(predictions, size, "yolox", 0.5)), 0)

    # スコアが下限に満たない検出は捨てること
    def test_low_score_is_dropped(self):
        size = 64
        cells = (size // 8) ** 2 + (size // 16) ** 2 + (size // 32) ** 2
        predictions = np.zeros((cells, 85), dtype=np.float32)
        predictions[0, 4] = 0.5
        predictions[0, 5] = 0.5         # obj x cls = 0.25
        self.assertEqual(len(detector._decode(predictions, size, "yolox", 0.4)), 0)

    # NMS が重なった検出を 1 つに絞ること
    def test_nms(self):
        boxes = np.array([
            [0, 0, 10, 10, 0.9],
            [1, 1, 11, 11, 0.8],        # ほぼ同じ場所 = 消える
            [100, 100, 110, 110, 0.7],  # 離れている = 残る
        ], dtype=np.float32)
        kept = detector._nms(boxes, 0.5)
        self.assertEqual(len(kept), 2)
        self.assertAlmostEqual(float(kept[0][4]), 0.9)
        self.assertAlmostEqual(float(kept[1][4]), 0.7)


class TrackerTest(unittest.TestCase):

    def setUp(self):
        self.cfg = config({"blur": {"analysis": {"sample_fps": 5.0, "min_track_sec": 0.0}}})

    # 重なる矩形が 1 本の tracklet へつながること
    def test_iou_tracking(self):
        track = tracker.Tracker(self.cfg, "m1")
        for index in range(5):
            track.update(index * 0.2, [(100 + index * 2, 100, 50, 120, 0.9)])
        finished = track.finish()
        self.assertEqual(len(finished), 1)
        self.assertEqual(len(finished[0].samples), 5)

    # 離れた場所の検出は別の tracklet になること
    def test_separate_tracklets(self):
        track = tracker.Tracker(self.cfg, "m1")
        for index in range(4):
            track.update(index * 0.2, [
                (100, 100, 50, 120, 0.9),
                (900, 100, 50, 120, 0.9),
            ])
        self.assertEqual(len(track.finish()), 2)

    # 短すぎる tracklet は誤検出として捨てること (min_track_sec)
    def test_short_tracklet_is_dropped(self):
        cfg = config({"blur": {"analysis": {"sample_fps": 5.0, "min_track_sec": 1.0}}})
        track = tracker.Tracker(cfg, "m1")
        track.update(0.0, [(100, 100, 50, 120, 0.9)])
        track.update(0.2, [(100, 100, 50, 120, 0.9)])
        self.assertEqual(track.finish(), [])

    # 合計秒数が最大の人物が主役になり、ID が登場順で決まること (R3 / §8-7)
    def test_cluster_picks_main(self):
        long_track = tracker.Tracklet("t1", "m1")
        short_track = tracker.Tracklet("t2", "m1")
        # 見た目が違う 2 人 (直交するベクトル)
        for index in range(11):
            long_track.add(index * 1.0, (0, 0, 10, 10, 0.9), np.array([1.0, 0.0], np.float32))
        for index in range(3):
            short_track.add(index * 1.0, (0, 0, 10, 10, 0.9), np.array([0.0, 1.0], np.float32))

        identities = tracker.cluster([short_track, long_track], self.cfg)
        self.assertEqual(len(identities), 2)
        self.assertEqual(identities[0]["id"], "p1")
        self.assertTrue(identities[0]["main"])
        # 長く映っている方が主役 (10 秒 > 2 秒)
        self.assertIn(long_track, identities[0]["tracks"])
        self.assertEqual(long_track.identity, "p1")
        self.assertFalse(identities[1]["main"])

    # 同じ見た目の tracklet は、離れて出てきても 1 人にまとまること (R6 の根拠)
    def test_cluster_merges_same_appearance(self):
        first = tracker.Tracklet("t1", "m1")
        second = tracker.Tracklet("t2", "m2")        # 別の素材 (別セクション)
        for index in range(5):
            first.add(index * 1.0, (0, 0, 10, 10, 0.9), np.array([1.0, 0.0], np.float32))
        for index in range(5):
            second.add(100 + index * 1.0, (0, 0, 10, 10, 0.9),
                       np.array([0.99, 0.14], np.float32))

        identities = tracker.cluster([first, second], self.cfg)
        self.assertEqual(len(identities), 1)
        self.assertEqual(first.identity, second.identity)

    # 同じ素材で時間を置いて出てきた同じ見た目も 1 人にまとまること
    def test_cluster_merges_same_media_later(self):
        first = tracker.Tracklet("t1", "m1")
        second = tracker.Tracklet("t2", "m1")
        for index in range(5):
            first.add(index * 0.2, (0, 0, 10, 10, 0.9), np.array([1.0, 0.0], np.float32))
        for index in range(5):
            second.add(3.0 + index * 0.2, (0, 0, 10, 10, 0.9),
                       np.array([0.99, 0.14], np.float32))

        identities = tracker.cluster([first, second], self.cfg)
        self.assertEqual(len(identities), 1)

    # 見た目が近くても、同じフレームに並んで映る 2 人は別人になること
    def test_cluster_keeps_people_seen_together_apart(self):
        left = tracker.Tracklet("t1", "m1")
        right = tracker.Tracklet("t2", "m1")
        for index in range(10):
            sec = index * 0.2
            left.add(sec, (0, 0, 10, 10, 0.9), np.array([1.0, 0.0], np.float32))
            if index >= 2:
                right.add(sec, (500, 0, 10, 10, 0.9), np.array([0.99, 0.14], np.float32))

        identities = tracker.cluster([left, right], self.cfg)
        self.assertEqual(len(identities), 2)
        self.assertNotEqual(left.identity, right.identity)
        self.assertEqual([i["main"] for i in identities], [True, False])

    # 1 人に全身と上半身の枠が重ねて出た 2 本は、同じ時刻でも 1 人にまとまること
    def test_cluster_merges_duplicate_boxes_of_one_person(self):
        body = tracker.Tracklet("t1", "m1")
        upper = tracker.Tracklet("t2", "m1")
        for index in range(10):
            sec = index * 0.2
            body.add(sec, (0, 400, 540, 1100, 0.9), np.array([1.0, 0.0], np.float32))
            if 3 <= index <= 5:
                # 乗り換えの瞬間 (index 5) は枠がずれて重なりが下がる
                box = (0, 700, 300, 500, 0.8) if index < 5 else (0, 1300, 500, 600, 0.8)
                upper.add(sec, box, np.array([0.98, 0.2], np.float32))

        identities = tracker.cluster([body, upper], self.cfg)
        self.assertEqual(len(identities), 1)
        self.assertEqual(body.identity, upper.identity)

    # 一番近い群と同時に映っていたら、次に近い群へ入ること
    def test_cluster_falls_back_to_next_group(self):
        left = tracker.Tracklet("t1", "m1")
        right = tracker.Tracklet("t2", "m1")
        later = tracker.Tracklet("t3", "m1")
        for index in range(10):
            left.add(index * 0.2, (0, 0, 10, 10, 0.9), np.array([1.0, 0.0], np.float32))
            right.add(index * 0.2, (500, 0, 10, 10, 0.9), np.array([0.9, 0.44], np.float32))
        # left より right に近い見た目だが、right とは同じ時刻に映っている
        for index in range(5):
            sec = 1.0 + index * 0.2
            right.add(sec + 1.0, (500, 0, 10, 10, 0.9), np.array([0.9, 0.44], np.float32))
            later.add(sec + 1.0, (0, 0, 10, 10, 0.9), np.array([0.92, 0.39], np.float32))

        identities = tracker.cluster([left, right, later], self.cfg)
        self.assertEqual(len(identities), 2)
        self.assertEqual(later.identity, left.identity)

    # 手で統合すると 1 人にまとまること (§5.6.6)
    def test_apply_merges(self):
        first = tracker.Tracklet("t1", "m1")
        second = tracker.Tracklet("t2", "m1")
        for index in range(5):
            first.add(index * 1.0, (0, 0, 10, 10, 0.9), np.array([1.0, 0.0], np.float32))
        for index in range(3):
            second.add(50 + index * 1.0, (0, 0, 10, 10, 0.9),
                       np.array([0.0, 1.0], np.float32))

        identities = tracker.cluster([first, second], self.cfg)
        self.assertEqual(len(identities), 2)
        merged = tracker.apply_merges(identities, [["p1", "p2"]])
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0]["main"])


if __name__ == "__main__":
    unittest.main()


class ModelMissingTest(unittest.TestCase):

    # モデルが無い環境でも例外にせず、空の結果を返すこと (§8-6 / §4-5)
    # これが守られないと、モデル未配置のまま Timeline 画面を開いただけで落ちる。
    def test_detect_without_model(self):
        cfg = config({"blur": {"model": {"detector": "models/does_not_exist.onnx"}}})
        image = np.zeros((64, 64, 3), dtype=np.uint8)
        self.assertEqual(detector.detect(image, cfg), [])

    def test_embed_without_model(self):
        from src.blur import reid

        cfg = config({"blur": {"model": {"reid": "models/does_not_exist.onnx"}}})
        image = np.zeros((64, 64, 3), dtype=np.uint8)
        vectors = reid.embed(image, [(0, 0, 10, 10, 0.9)], cfg)
        self.assertEqual(vectors.shape, (0, 512))

    # モデルが見つからないことを、理由付きで報告できること (設定画面へ出す文言)
    def test_availability_reports_reason(self):
        from src.blur import models

        # 既定のパスだと、モデルを配置した開発環境で結果が変わるため存在しないパスを指す
        cfg = config({"blur": {"model": {"detector": "models/does_not_exist.onnx",
                                         "reid": "models/does_not_exist.onnx"}}})
        available, reason = models.availability(cfg)
        self.assertFalse(available)
        self.assertIn("モデルが見つかりません", reason)


class RegionCorrelationTest(unittest.TestCase):

    def setUp(self):
        self.threshold = config({})["region"]["match_psr"]
        rng = np.random.default_rng(7)
        # 滑らかな模様 (実写に近い低周波成分を持たせる)
        noise = rng.random((120, 90)).astype(np.float32)
        self.scene = np.kron(noise, np.ones((4, 4), np.float32)) * 255.0

    # パンで平行移動した絵から、ずれを正しく求めて一致とみなすこと
    def test_shift_is_recovered(self):
        moved = np.roll(np.roll(self.scene, 12, axis=0), -20, axis=1)
        dx, dy, _peak, psr = region_tracker.phase_correlate(self.scene, moved)
        self.assertEqual((dx, dy), (20.0, -12.0))
        self.assertGreaterEqual(psr, self.threshold)

    # 人が大きく動いてピークが低くなっても、同じ場面なら一致とみなすこと
    # (ピークの高さで判定していたときは 0.35 を下回り、囲んだ直後しか追従できなかった)
    def test_changed_scene_still_matches(self):
        rng = np.random.default_rng(8)
        changed = self.scene.copy()
        # 画面の 1/4 が入れ替わり、全体にノイズが乗る (実素材ではピーク 0.07 前後だった)
        changed[100:340, 60:240] = rng.random((240, 180)).astype(np.float32) * 255.0
        changed += rng.normal(0.0, 20.0, changed.shape).astype(np.float32)
        dx, dy, peak, psr = region_tracker.phase_correlate(self.scene, changed)
        self.assertEqual((dx, dy), (0.0, 0.0))
        self.assertLess(peak, 0.35)
        self.assertGreaterEqual(psr, self.threshold)

    # 無関係な絵は一致とみなさないこと
    def test_unrelated_scene_does_not_match(self):
        rng = np.random.default_rng(9)
        other = np.kron(rng.random((120, 90)).astype(np.float32),
                        np.ones((4, 4), np.float32)) * 255.0
        _dx, _dy, _peak, psr = region_tracker.phase_correlate(self.scene, other)
        self.assertLess(psr, self.threshold)


class PreprocessTest(unittest.TestCase):

    # YOLOX の前処理: BGR のまま / 0〜255 のまま / 余白は 114 (§3.7.1)
    # ここを間違えると、モデルは動くのに検出が出ないという分かりにくい壊れ方をする。
    def test_yolox_preprocess(self):
        image = np.zeros((100, 200, 3), dtype=np.uint8)
        image[:, :, 0] = 10     # R
        image[:, :, 1] = 20     # G
        image[:, :, 2] = 30     # B

        blob, scale = detector._preprocess(image, 64, 114, "yolox")
        self.assertEqual(blob.shape, (1, 3, 64, 64))
        self.assertAlmostEqual(scale, 64 / 200)             # 長辺 200 に合わせる

        # 先頭チャンネルは B (BGR へ入れ替わっている)。正規化していないため 30 のまま。
        self.assertAlmostEqual(float(blob[0, 0, 0, 0]), 30.0)
        self.assertAlmostEqual(float(blob[0, 2, 0, 0]), 10.0)
        # 余白 (右下) は 114。BGR へ入れ替えても値は同じ。
        self.assertAlmostEqual(float(blob[0, 0, 63, 63]), 114.0)

    # OSNet の前処理: RGB / 0〜1 へ割ってから ImageNet の mean・std で正規化 (§3.7.2)
    def test_osnet_preprocess(self):
        from src.blur import reid

        image = np.zeros((100, 200, 3), dtype=np.uint8)
        image[:, :, 0] = 255    # R
        patch = reid._crop(image, (0, 0, 200, 100), 128, 256, "osnet")
        self.assertEqual(patch.shape, (3, 256, 128))        # 縦 256 x 横 128
        # R = 1.0 → (1.0 - 0.485) / 0.229
        self.assertAlmostEqual(float(patch[0, 0, 0]), (1.0 - 0.485) / 0.229, places=4)
        self.assertAlmostEqual(float(patch[1, 0, 0]), (0.0 - 0.456) / 0.224, places=4)

    # L2 正規化した後は、同じ向きのベクトルどうしの距離が 0 になること
    def test_normalize_and_distance(self):
        from src.blur import reid

        vectors = reid.normalize(np.array([[3.0, 4.0]], dtype=np.float32))
        self.assertAlmostEqual(float(np.linalg.norm(vectors[0])), 1.0, places=6)
        self.assertAlmostEqual(reid.distance(vectors[0], vectors[0]), 0.0, places=6)
