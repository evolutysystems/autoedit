# 縦動画の切り抜き指定 (src/timeline/crop.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve9.md §8.1 A):
#   ・置き場所の計算が mode どおりであること
#   ・指定が往復すること / 旧プロジェクトを壊さないこと (R12)
#   ・噛み合わない指定は「切り抜き無し」に倒れること (§4-4)
#   ・FFmpeg のフィルタ文字列が 3 分岐それぞれで期待どおりであること
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.timeline import crop, project_io
from src.timeline.model import MediaRef, Timeline

_CANVAS = (1080, 1920)


def _media(width=1920, height=1080, media_id="m1"):
    return MediaRef(media_id, "video", __file__, 100.0, width, height, 60, True)


def _timeline(media=None):
    media = media or _media()
    return Timeline(fps=60, width=1920, height=1080,
                    source={"media_id": media.id, "input_path": __file__},
                    media_pool=[media])


class GeometryTest(unittest.TestCase):

    # 分割は上 1/3・下 2/3 でキャンバスをぴったり埋める (R5)
    def test_split_dest(self):
        rects = crop.dest_rects(crop.MODE_SPLIT, *_CANVAS)

        self.assertEqual(rects, [(0, 0, 1080, 640), (0, 640, 1080, 1280)])
        self.assertEqual(rects[0][3] + rects[1][3], 1920, "上下の合計がキャンバス高と違います")

    def test_single_has_no_fixed_dest(self):
        self.assertEqual(crop.dest_rects(crop.MODE_SINGLE, *_CANVAS), [])

    # 全体は収まるよう拡縮して中央へ置く (R4)
    def test_fit_single_square(self):
        self.assertEqual(crop.fit_single((420, 0, 1080, 1080), *_CANVAS), (0, 420, 1080, 1080))

    # 縦長の枠でもキャンバスからはみ出さないこと
    def test_fit_single_tall(self):
        x, y, width, height = crop.fit_single((0, 0, 300, 1000), *_CANVAS)

        self.assertLessEqual(width, 1080)
        self.assertLessEqual(height, 1920)
        self.assertEqual(x, (1080 - width) // 2)
        self.assertEqual(y, (1920 - height) // 2)

    # 分割の枠は比率が決まっている (27:16 と 27:32)
    def test_split_aspect(self):
        self.assertAlmostEqual(crop.aspect(crop.MODE_SPLIT, 0, *_CANVAS), 1080 / 640.0)
        self.assertAlmostEqual(crop.aspect(crop.MODE_SPLIT, 1, *_CANVAS), 1080 / 1280.0)
        self.assertIsNone(crop.aspect(crop.MODE_SINGLE, 0, *_CANVAS))

    # 分割の下枠は 1920x1080 のソースでは幅 911 までしか取れない (plan.md の制約)
    def test_split_bottom_is_limited_by_the_source_height(self):
        width, height = crop.max_src_size(crop.MODE_SPLIT, 1, 1080, 1920, 1920, 1080)

        self.assertLessEqual(height, 1080)
        self.assertLessEqual(width, 912)
        self.assertGreater(width, 900)

    # 拡大率の上限から最小サイズが決まる (R16)
    def test_min_src_size_follows_max_scale(self):
        self.assertEqual(crop.min_src_size(crop.MODE_SPLIT, 1, 1080, 1920, 2.0), (540, 640))
        self.assertEqual(crop.min_src_size(crop.MODE_SPLIT, 1, 1080, 1920, 1.0), (1080, 1280))


class StoreTest(unittest.TestCase):

    def test_round_trip(self):
        media = _media()
        timeline = _timeline(media)
        layout = crop.make_layout(crop.MODE_SPLIT, [(0, 0, 1080, 640), (500, 0, 910, 1078)],
                                  media, *_CANVAS)
        crop.store(timeline, layout)

        restored = project_io.from_json(project_io.to_json(timeline))
        loaded = crop.load(restored)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["mode"], crop.MODE_SPLIT)
        self.assertEqual(loaded["frames"][0]["src"], [0, 0, 1080, 640])
        self.assertEqual(loaded["frames"][1]["dest"], [0, 640, 1080, 1280])

    # 旧プロジェクト (crop キー無し) は「切り抜き無し」
    def test_missing_key(self):
        self.assertIsNone(crop.load(_timeline()))

    def test_unknown_version_is_ignored(self):
        timeline = _timeline()
        crop.store(timeline, {"version": 99, "mode": crop.MODE_SINGLE, "frames": []})

        self.assertIsNone(crop.load(timeline))

    def test_store_none_removes(self):
        media = _media()
        timeline = _timeline(media)
        crop.store(timeline, crop.make_layout(crop.MODE_SINGLE, [(0, 0, 100, 100)],
                                              media, *_CANVAS))
        crop.store(timeline, None)

        self.assertIsNone(crop.load(timeline))
        self.assertNotIn(crop.KEY, timeline.source)


class ValidationTest(unittest.TestCase):

    def setUp(self):
        self.media = _media()
        self.layout = crop.make_layout(
            crop.MODE_SINGLE, [(420, 0, 1080, 1080)], self.media, *_CANVAS)

    def test_valid(self):
        self.assertTrue(crop.is_valid(self.layout, self.media, *_CANVAS))

    def test_frame_count_must_match_the_mode(self):
        broken = dict(self.layout, mode=crop.MODE_SPLIT)
        self.assertFalse(crop.is_valid(broken, self.media, *_CANVAS))

    def test_out_of_bounds(self):
        broken = dict(self.layout, frames=[{"src": [1500, 0, 1080, 1080], "dest": [0, 0, 2, 2]}])
        self.assertFalse(crop.is_valid(broken, self.media, *_CANVAS))

    # 素材を差し替えて寸法が変わったら当てられない
    def test_source_size_changed(self):
        self.assertFalse(crop.is_valid(self.layout, _media(1280, 720), *_CANVAS))

    # 別の素材のクリップには当てない
    def test_other_media(self):
        self.assertFalse(crop.is_valid(self.layout, _media(media_id="m2"), *_CANVAS))

    def test_none(self):
        self.assertFalse(crop.is_valid(None, self.media, *_CANVAS))


class FilterChainTest(unittest.TestCase):

    def setUp(self):
        self.media = _media()
        self.cfg = crop.config({})

    def _single(self, background):
        layout = crop.make_layout(crop.MODE_SINGLE, [(420, 0, 1080, 1080)], self.media,
                                  *_CANVAS, background=background)
        return crop.filter_chain(layout, 1080, 1920, 60, dict(self.cfg, background=background))

    # 全体 (黒): 切り抜き → 収める → 余白
    def test_single_black(self):
        chain = self._single(crop.BG_BLACK)

        self.assertTrue(chain.startswith("crop=1080:1080:420:0,"))
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=decrease", chain)
        self.assertIn("pad=1080:1920:(ow-iw)/2:(oh-ih)/2", chain)
        self.assertNotIn("boxblur", chain)
        self.assertTrue(chain.endswith("setsar=1,fps=60,format=yuv420p"))

    # 全体 (ぼかし): 入力を split して背景と前景を作る (入力本数は増えない)
    def test_single_blur(self):
        chain = self._single(crop.BG_BLUR)

        self.assertTrue(chain.startswith("split=2[cbg][cfg];"))
        self.assertIn("boxblur=20:2", chain)
        self.assertIn("force_original_aspect_ratio=increase", chain)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", chain)

    # 分割: 上下を作って vstack で積む
    def test_split(self):
        layout = crop.make_layout(crop.MODE_SPLIT, [(0, 0, 1080, 640), (500, 0, 910, 1078)],
                                  self.media, *_CANVAS)
        chain = crop.filter_chain(layout, 1080, 1920, 60, self.cfg)

        self.assertIn("crop=1080:640:0:0,scale=1080:640", chain)
        self.assertIn("crop=910:1078:500:0,scale=1080:1280", chain)
        self.assertIn("vstack=inputs=2", chain)

    # crop の引数は整数であること (FFmpeg が小数を受け付けない)
    def test_integers(self):
        layout = crop.make_layout(crop.MODE_SINGLE, [(10.4, 20.6, 100.5, 200.5)],
                                  self.media, *_CANVAS)
        chain = crop.filter_chain(layout, 1080, 1920, 60, self.cfg)

        self.assertIn("crop=100:200:10:21", chain)


class ConfigTest(unittest.TestCase):

    def test_defaults(self):
        cfg = crop.config({})

        self.assertEqual(cfg["background"], crop.BG_BLUR)
        self.assertEqual(cfg["max_scale"], 2.0)
        self.assertTrue(cfg["close_gaps"])

    def test_unknown_background_falls_back(self):
        cfg = crop.config({"vertical": {"crop": {"background": "虹色"}}})
        self.assertEqual(cfg["background"], crop.BG_BLUR)

    def test_max_scale_is_clamped(self):
        cfg = crop.config({"vertical": {"crop": {"max_scale": 0.1}}})
        self.assertEqual(cfg["max_scale"], 1.0)


class PreviewRectsTest(unittest.TestCase):

    # 画面と出力で同じ配置になること (§4-3)
    def test_matches_the_layout(self):
        media = _media()
        layout = crop.make_layout(crop.MODE_SPLIT, [(0, 0, 1080, 640), (500, 0, 910, 1078)],
                                  media, *_CANVAS)
        rects = crop.preview_rects(layout, *_CANVAS)

        self.assertEqual([r["dest"] for r in rects], [(0, 0, 1080, 640), (0, 640, 1080, 1280)])
        self.assertEqual(rects[1]["src"], (500, 0, 910, 1078))


if __name__ == "__main__":
    unittest.main()
