# マスクの重ね塗り (src/blur/mask_builder.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve8.md §3.2 / §5.8 / §8.1 C):
#   ・指定が無ければ 1 バイトも塗らないこと (R14 の費用ゼロ経路)
#   ・全面ぼかしは真っ白、その上の「ボカさない」が穴を開けること (R6)
#   ・穴の中をさらに「ボカす」で塗り直せること (R6-3)
#   ・並び順の後のものが勝つこと (§3.2)
#   ・span の外は真っ黒であること (R11)
#   ・塗り方 (四角 / 角丸 / 楕円) と縁のなじませが効くこと
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from src.blur import contour, mask_builder
from src.blur import decisions as blur_decisions
from src.blur.config import config
from src.blur.plan import BlurPlan
from src.timeline.model import (
    ORIGIN_SILENCE_CUT,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    AudioClip,
    Clip,
    MediaRef,
    Timeline,
    Track,
)


def _build_timeline():
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 5.0, 0.0, 5.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
                  clips=[AudioClip("a1", "c1")])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1")
    return Timeline(fps=60, width=1920, height=1080,
                    source={"media_id": "m1", "input_path": __file__},
                    media_pool=media, tracks=[video, audio, subtitle])


class MaskPaintTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build_timeline()
        self.clip = self.timeline.base_clips()[0]
        self.span = blur_decisions.span_for(self.timeline, self.clip)

    def _cfg(self, **render):
        section = {"enabled": True, "render": dict({"feather_ratio": 0.0}, **render)}
        return config({"blur": section})

    def _painter(self, decisions, cfg=None):
        cfg = cfg or self._cfg()
        plan = BlurPlan(self.timeline, None, decisions, cfg)
        return mask_builder._MaskPainter(   # noqa: SLF001 (同じ層の道具をテストする)
            plan, self.timeline, cfg, *mask_builder.mask_size(self.timeline, cfg))

    def _mask(self, decisions, sec=1.0, cfg=None):
        return np.asarray(self._painter(decisions, cfg).paint("m1", sec))

    def _empty(self):
        return blur_decisions._empty()      # noqa: SLF001

    def _with_frame(self, decisions=None):
        return blur_decisions.with_spec(
            decisions or self._empty(),
            blur_decisions.make_frame_spec("b1", "m1", self.span), to_bottom=True)

    def _with_area(self, decisions, spec_id, mode, rect):
        return blur_decisions.with_spec(decisions, blur_decisions.make_area_spec(
            spec_id, mode, "m1", self.span, 1.0, rect))

    # 正規化座標 (0〜1) の点のマスク上の値
    def _at(self, mask, x, y):
        height, width = mask.shape[:2]
        return int(mask[min(int(y * height), height - 1), min(int(x * width), width - 1)])

    # C1: 指定が無ければ真っ黒
    def test_no_specs_is_black(self):
        self.assertEqual(self._mask(self._empty()).max(), 0)

    # C2: 全面ぼかしは真っ白
    def test_frame_is_white(self):
        mask = self._mask(self._with_frame())
        self.assertEqual(mask.min(), 255)

    # C3: 全面 + ボカさない = 穴が開く
    def test_keep_punches_a_hole(self):
        decisions = self._with_area(self._with_frame(), "b2", blur_decisions.KEEP,
                                    (0.3, 0.3, 0.2, 0.2))
        mask = self._mask(decisions)
        self.assertEqual(self._at(mask, 0.4, 0.4), 0, "穴が開いていません")
        self.assertEqual(self._at(mask, 0.1, 0.1), 255, "穴の外までぼかしが消えています")

    # C4: 穴の中をさらにボカすで塗り直せる (R6-3)
    def test_blur_inside_a_hole(self):
        decisions = self._with_area(self._with_frame(), "b2", blur_decisions.KEEP,
                                    (0.3, 0.3, 0.2, 0.2))
        decisions = self._with_area(decisions, "b3", blur_decisions.BLUR,
                                    (0.35, 0.35, 0.05, 0.05))
        mask = self._mask(decisions)
        self.assertEqual(self._at(mask, 0.37, 0.37), 255, "穴の中を塗り直せていません")
        self.assertEqual(self._at(mask, 0.45, 0.45), 0, "穴が埋まってしまっています")

    # C5: 並び順の後のものが勝つ
    def test_order_decides_the_winner(self):
        decisions = self._with_area(self._with_frame(), "b2", blur_decisions.KEEP,
                                    (0.3, 0.3, 0.2, 0.2))
        decisions = self._with_area(decisions, "b3", blur_decisions.BLUR,
                                    (0.3, 0.3, 0.2, 0.2))
        self.assertEqual(self._at(self._mask(decisions), 0.4, 0.4), 255)
        # 入れ替えると「ボカさない」が上に来る
        swapped = blur_decisions.with_spec_moved(decisions, "b2", +1)
        self.assertEqual(self._at(self._mask(swapped), 0.4, 0.4), 0)

    # C6: span の外は真っ黒
    def test_outside_the_span_is_black(self):
        decisions = self._with_area(self._with_frame(), "b2", blur_decisions.BLUR,
                                    (0.3, 0.3, 0.2, 0.2))
        self.assertEqual(self._mask(decisions, sec=6.0).max(), 0)

    # C7: 塗り方で角の塗られ方が変わる
    def test_shape_changes_the_corners(self):
        decisions = self._with_area(self._empty(), "b1", blur_decisions.BLUR,
                                    (0.3, 0.3, 0.4, 0.4))
        rect = self._mask(decisions, cfg=self._cfg(shape="rect"))
        rounded = self._mask(decisions, cfg=self._cfg(shape="rounded"))
        ellipse = self._mask(decisions, cfg=self._cfg(shape="ellipse"))
        # 左上の角 (矩形なら塗られる / 角丸・楕円なら塗られない)
        self.assertEqual(self._at(rect, 0.31, 0.31), 255)
        self.assertEqual(self._at(rounded, 0.31, 0.31), 0)
        self.assertEqual(self._at(ellipse, 0.31, 0.31), 0)
        # 中心はどれも塗られる
        for mask in (rect, rounded, ellipse):
            self.assertEqual(self._at(mask, 0.5, 0.5), 255)

    # C8: v4 以前から引き継いだ自由な囲みは多角形で塗る
    def test_outline_is_painted_as_a_polygon(self):
        spec = blur_decisions.make_area_spec(
            "b1", blur_decisions.BLUR, "m1", self.span, 1.0, (0.2, 0.2, 0.4, 0.4),
            outline=contour.encode([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]]))
        decisions = blur_decisions.with_spec(self._empty(), spec)
        mask = self._mask(decisions)
        # 左上の三角形の内側は塗られ、右下は塗られない
        self.assertEqual(self._at(mask, 0.25, 0.25), 255)
        self.assertEqual(self._at(mask, 0.55, 0.55), 0)

    # C9: 縁のなじませは形の外側でなだらかに落ちる
    def test_feather_falls_outside(self):
        decisions = self._with_area(self._empty(), "b1", blur_decisions.BLUR,
                                    (0.3, 0.3, 0.4, 0.4))
        sharp = self._mask(decisions, cfg=self._cfg(feather_ratio=0.0))
        soft = self._mask(decisions, cfg=self._cfg(feather_ratio=0.02))
        self.assertEqual(self._at(sharp, 0.29, 0.5), 0)
        self.assertGreater(self._at(soft, 0.29, 0.5), 0, "縁がなじんでいません")
        self.assertLess(self._at(soft, 0.29, 0.5), 255)

    # C10: キーフレームが 1 点だけでも、その位置には塗れること (知らせは出さない)
    def test_single_key_is_still_painted(self):
        decisions = self._with_area(self._empty(), "b1", blur_decisions.BLUR,
                                    (0.3, 0.3, 0.2, 0.2))
        plan = BlurPlan(self.timeline, None, decisions, self._cfg())
        self.assertEqual(mask_builder._unpaintable_blur_specs(   # noqa: SLF001
            plan, decisions), 0)
        self.assertEqual(self._at(self._mask(decisions), 0.4, 0.4), 255)

    # 設定で余白を足せること (既定は 0 = 囲んだとおり)
    def test_margin_ratio(self):
        decisions = self._with_area(self._empty(), "b1", blur_decisions.BLUR,
                                    (0.3, 0.3, 0.2, 0.2))
        tight = self._mask(decisions, cfg=self._cfg(margin_ratio=0.0))
        loose = self._mask(decisions, cfg=self._cfg(margin_ratio=0.05))
        self.assertEqual(self._at(tight, 0.27, 0.4), 0)
        self.assertEqual(self._at(loose, 0.27, 0.4), 255)

    # 指定が効いていないフレームは同じ絵を使い回すこと (費用ゼロ経路)
    def test_blank_frame_is_reused(self):
        painter = self._painter(self._with_frame())
        self.assertIs(painter.paint("m1", 6.0), painter.blank)
        self.assertIs(painter.paint("m2", 1.0), painter.blank)


if __name__ == "__main__":
    unittest.main()
