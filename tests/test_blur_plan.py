# 「何をぼかす / 守る / 使わないか」の判定 (src/blur/plan.py) とマスクの 2 層合成の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve3.md §3.4 / §3.5 / §5.4 / §8.1):
#   ・役割の表 (主役・明示・manual_only・削除した枠・分けた人物・指定に無い領域) のとおりになること
#   ・削除した領域の古い追従トラックが、画面にも出力にも出ないこと (resolve3 §2.3 (a))
#   ・「ぼかす」と「ぼかさない」が重なった部分はぼけないこと。フェザーが守る側へ染みないこと
import os
import unittest

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.blur import contour, mask_builder
from src.blur import decisions as blur_decisions
from src.blur.config import config
from src.blur.plan import ROLE_BLUR, ROLE_KEEP, BlurPlan
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
        Clip("c1", "m1", 0.0, 10.0, 0.0, 10.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
                  clips=[AudioClip("a1", "c1")])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1")
    return Timeline(fps=60, width=1920, height=1080,
                    source={"media_id": "m1", "input_path": __file__},
                    media_pool=media, tracks=[video, audio, subtitle])


def _samples(x, y, w, h, start=0.0, end=8.0, step=0.2):
    count = int(round((end - start) / step)) + 1
    return [{"t": round(start + i * step, 3), "x": x, "y": y, "w": w, "h": h, "score": 0.9}
            for i in range(count)]


# 解析結果: 主役 p1 (8 秒) / p2 (4 秒) / 領域 r1 の古い追従 (指定には無い)
def _build_analysis():
    return {
        "schema": 2, "fingerprint": "fp", "sample_fps": 5.0,
        "identities": [
            {"id": "p1", "total_sec": 8.0, "main": True, "tracks": ["t1"]},
            {"id": "p2", "total_sec": 4.0, "main": False, "tracks": ["t2"]},
        ],
        "tracks": [
            {"id": "t1", "identity": "p1", "media_id": "m1", "kind": "person",
             "samples": _samples(200, 100, 400, 800)},
            {"id": "t2", "identity": "p2", "media_id": "m1", "kind": "person",
             "samples": _samples(500, 100, 400, 800, 2.0, 6.0)},
            {"id": "r1_m1_0", "identity": "r1", "media_id": "m1", "kind": "region",
             "shape_hash": "old", "samples": _samples(1500, 100, 200, 200)},
        ],
    }


class RoleTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build_timeline()
        self.analysis = _build_analysis()
        self.cfg = config({"blur": {"enabled": True, "render": {"shape": "rounded"}}})

    def _plan(self, state=None):
        return BlurPlan(self.timeline, self.analysis, state or blur_decisions._empty(), self.cfg)

    def _role(self, plan, track_id):
        return plan.entry_of(track_id)["role"]

    # blur_others: 主役は守り、それ以外はぼかす
    def test_blur_others(self):
        plan = self._plan()
        self.assertEqual(plan.main_id, "p1")
        self.assertEqual(self._role(plan, "t1"), ROLE_KEEP)
        self.assertEqual(self._role(plan, "t2"), ROLE_BLUR)

    # manual_only で未指定の人物は「対象外」(守る対象でもない / §3.4)
    def test_manual_only_unmarked_is_unused(self):
        state = blur_decisions._empty()
        state["default_policy"] = "manual_only"
        plan = self._plan(state)
        self.assertIsNone(self._role(plan, "t2"))
        # 主役は manual_only でも暗黙に守る
        self.assertEqual(self._role(plan, "t1"), ROLE_KEEP)

    # 明示指定が最優先
    def test_explicit(self):
        state = blur_decisions.with_identity(blur_decisions._empty(), "p1", blur_decisions.BLUR)
        state = blur_decisions.with_identity(state, "p2", blur_decisions.KEEP)
        plan = self._plan(state)
        self.assertEqual(self._role(plan, "t1"), ROLE_BLUR)
        self.assertEqual(self._role(plan, "t2"), ROLE_KEEP)

    # 削除した枠は使わず、主役の数え方からも外れる (主役が入れ替わる)
    def test_excluded_track(self):
        anchor = blur_decisions.track_anchor(self.analysis["tracks"][0])
        state = blur_decisions.with_excluded(blur_decisions._empty(), anchor)
        plan = self._plan(state)
        self.assertIsNone(self._role(plan, "t1"))
        self.assertTrue(plan.entry_of("t1")["excluded"])
        self.assertEqual(plan.main_id, "p2")
        self.assertEqual(len(plan.excluded_entries()), 1)

    # アンカーは解析し直して tracklet ID が変わっても当たる
    def test_anchor_survives_renumbering(self):
        anchor = blur_decisions.track_anchor(self.analysis["tracks"][1])
        self.analysis["tracks"][1]["id"] = "t99"
        state = blur_decisions.with_excluded(blur_decisions._empty(), anchor)
        self.assertTrue(self._plan(state).entry_of("t99")["excluded"])

    # 別の人物にした枠は新しい人物 ID (s1) になり、既存の人物 ID は変わらない
    def test_split_track(self):
        anchor = blur_decisions.track_anchor(self.analysis["tracks"][1])
        state = blur_decisions.with_split(blur_decisions._empty(), anchor)
        plan = self._plan(state)
        self.assertEqual(plan.identity_of("t2"), "s1")
        self.assertEqual([i["id"] for i in plan.identities], ["p1", "p2", "s1"])
        self.assertEqual(self._role(plan, "t2"), ROLE_BLUR)
        # 分けた人物へ指定できる
        state = blur_decisions.with_identity(state, "s1", blur_decisions.KEEP)
        self.assertEqual(self._role(self._plan(state), "t2"), ROLE_KEEP)

    # 指定に無い領域の古いトラックは、画面にも出力にも出ない (§2.3 (a))
    def test_orphan_region_is_hidden(self):
        plan = self._plan()
        shapes = plan.shapes_at("m1", 4.0, include_unused=True)
        self.assertNotIn("r1_m1_0", [s["key"] for s in shapes])

    # 指定と形の指紋が違う古いトラックは使わない (§5.7 (2))
    def test_stale_region_track_is_ignored(self):
        region = {"id": "r1", "kind": "place", "mode": "blur", "media_id": "m1",
                  "anchor_sec": 1.0, "path": [[0.8, 0.1], [0.9, 0.1], [0.9, 0.2]]}
        state = blur_decisions.with_region(blur_decisions._empty(), region)
        plan = self._plan(state)
        self.assertIsNone(plan.entry_of("r1_m1_0")["role"])
        self.assertEqual([r["id"] for r in plan.untracked_regions()], ["r1"])

        # 指紋が合えば使う。ぼかさない領域は守る形になる
        self.analysis["tracks"][2]["shape_hash"] = blur_decisions.region_shape_hash(region)
        state = blur_decisions.with_region_mode(state, "r1", blur_decisions.KEEP)
        self.analysis["tracks"][2]["shape_hash"] = blur_decisions.region_shape_hash(
            state["regions"][0])
        self.assertEqual(self._plan(state).entry_of("r1_m1_0")["role"], ROLE_KEEP)

    # 追従できなかった印 (サンプルが空) は「追従できていない」と分かる
    def test_untracked_marker(self):
        region = {"id": "r2", "kind": "object", "mode": "blur", "media_id": "m1",
                  "anchor_sec": 1.0, "path": [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]]}
        state = blur_decisions.with_region(blur_decisions._empty(), region)
        self.analysis["tracks"].append({
            "id": "r2_none", "identity": "r2", "kind": "manual", "media_id": "m1",
            "shape_hash": blur_decisions.region_shape_hash(region), "samples": []})
        plan = self._plan(state)
        self.assertIn("r2", [r["id"] for r in plan.untracked_regions()])
        self.assertNotIn("r2", [s["identity"] for s in plan.shapes_at("m1", 1.0)])

    # 前後へ伸ばすのはぼかす形だけ (守る形は伸ばさない)
    def test_pad_only_for_blur(self):
        plan = self._plan()
        pad = self.cfg["render"]["pad_sec"]
        roles = {s["identity"]: s["role"] for s in plan.shapes_at("m1", 6.0 + pad / 2)}
        self.assertEqual(roles.get("p2"), ROLE_BLUR)
        roles = {s["identity"] for s in plan.shapes_at("m1", 8.0 + pad / 2)}
        self.assertNotIn("p1", roles)


class SilhouetteSelectionTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build_timeline()
        self.cfg = config({"blur": {"enabled": True, "render": {"shape": "silhouette"}}})
        square = contour.encode(contour.normalize(
            [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)], 64))
        samples = _samples(500, 100, 400, 800, 0.0, 4.0)
        samples[0]["sil"] = square          # 0.0 秒にだけ輪郭がある
        self.analysis = {"schema": 2, "sample_fps": 5.0,
                         "identities": [{"id": "p1", "total_sec": 4.0, "main": True}],
                         "tracks": [{"id": "t1", "identity": "p1", "media_id": "m1",
                                     "kind": "person", "samples": samples}]}

    def _silhouette(self, sec):
        plan = BlurPlan(self.timeline, self.analysis, blur_decisions._empty(), self.cfg)
        return plan.shapes_at("m1", sec)[0]["silhouette"]

    # 輪郭のある時刻の近くでは輪郭を使い、max_gap_sec を超えたら矩形へ落とす
    def test_gap_falls_back_to_rect(self):
        self.assertIsNotNone(self._silhouette(0.5))
        self.assertIsNone(self._silhouette(self.cfg["silhouette"]["max_gap_sec"] + 0.5))

    # 小さすぎる輪郭 (取り損ね) は使わない
    def test_small_silhouette_is_ignored(self):
        tiny = contour.encode(contour.normalize(
            [(0.45, 0.45), (0.55, 0.45), (0.55, 0.55), (0.45, 0.55)], 64))
        self.analysis["tracks"][0]["samples"][0]["sil"] = tiny
        self.assertIsNone(self._silhouette(0.0))


class MotionMarginTest(unittest.TestCase):

    # 1 秒で横へ move_px 動く人物 (0〜4 秒 / 0.2 秒ごと) と、動かない主役
    def _analysis(self, move_px, with_silhouette=True):
        square = contour.encode(contour.normalize(
            [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)], 64))
        moving = [{"t": round(i * 0.2, 3), "x": 500 + move_px * i * 0.2, "y": 100,
                   "w": 300, "h": 800, "score": 0.9} for i in range(21)]
        still = _samples(1400, 100, 300, 800, 0.0, 4.0)
        for sample in moving + still:
            if with_silhouette:
                sample["sil"] = square
        return {"schema": 2, "sample_fps": 5.0,
                "identities": [{"id": "p1", "total_sec": 4.0, "main": True},
                               {"id": "p2", "total_sec": 3.9, "main": False}],
                "tracks": [{"id": "t1", "identity": "p1", "media_id": "m1", "kind": "person",
                            "samples": still},
                           {"id": "t2", "identity": "p2", "media_id": "m1", "kind": "person",
                            "samples": moving}]}

    def _shapes(self, analysis, **blur_cfg):
        cfg = config({"blur": dict({"enabled": True}, **blur_cfg)})
        plan = BlurPlan(_build_timeline(), analysis, blur_decisions._empty(), cfg)
        return {s["identity"]: s for s in plan.shapes_at("m1", 2.0)}

    # 動かない人物にも、人物の幅に比例した余白が付く
    def test_static_margin_scales_with_person(self):
        shapes = self._shapes(self._analysis(0))
        self.assertAlmostEqual(shapes["p2"]["motion"], 0.0)
        # 人物の幅 300 x 0.15 = 45 (画面幅 1920 x 0.01 = 19.2 より大きい)
        self.assertAlmostEqual(shapes["p2"]["grow"], 45.0)

    # 激しく動くほど余白が広がり、上限 (枠の長い辺 x 0.5) で止まる
    def test_margin_grows_with_motion(self):
        slow = self._shapes(self._analysis(100))["p2"]
        fast = self._shapes(self._analysis(400))["p2"]
        self.assertGreater(slow["grow"], 45.0)
        self.assertGreater(fast["grow"], slow["grow"])
        huge = self._shapes(self._analysis(5000))["p2"]
        self.assertLessEqual(huge["grow"], 800 * 0.5 + 1e-6)

    # 動いた量が人物の幅の fast_motion_box_ratio を超えたら、輪郭をやめて四角でぼかす
    def test_fast_motion_falls_back_to_rect(self):
        # 0.3 秒で 100 * 0.3 = 30px < 300 * 0.15 = 45px → 輪郭のまま
        self.assertIsNotNone(self._shapes(self._analysis(100))["p2"]["silhouette"])
        # 0.3 秒で 400 * 0.3 = 120px > 45px → 四角
        self.assertIsNone(self._shapes(self._analysis(400))["p2"]["silhouette"])
        # 0 にすれば切り替えない
        shapes = self._shapes(self._analysis(400), silhouette={"fast_motion_box_ratio": 0.0})
        self.assertIsNotNone(shapes["p2"]["silhouette"])

    # 守る人物には、既定では余白を付けない (keep_motion_margin で動いた量ぶんだけ付けられる)
    def test_keep_margin_is_opt_in(self):
        analysis = self._analysis(0)
        # 主役 (守る) を動かす
        for index, sample in enumerate(analysis["tracks"][0]["samples"]):
            sample["x"] = 1400 - 300 * index * 0.2
        self.assertEqual(self._shapes(analysis)["p1"]["grow"], 0.0)
        shapes = self._shapes(analysis, render={"keep_motion_margin": True})
        self.assertGreater(shapes["p1"]["grow"], 0.0)

    # 余白はマスクにも効く: 動いている人物ほど白い面積が広い
    def test_mask_area_grows_with_motion(self):
        cfg = config({"blur": {"enabled": True,
                               "silhouette": {"fast_motion_box_ratio": 0.0}}})
        timeline = _build_timeline()
        areas = []
        for move in (0, 100):
            mask = mask_builder.frame_mask(timeline, self._analysis(move),
                                           blur_decisions._empty(), cfg, 2.0)
            if mask is None:
                self.skipTest("PIL が使えないためマスクを作れません")
            areas.append(int((np.asarray(mask) > 128).sum()))
        self.assertGreater(areas[1], areas[0])


class KeepPriorityMaskTest(unittest.TestCase):

    # ぼかす枠 (p2) と守る枠 (主役 p1) が重なった部分はぼけない (Q2)
    def test_overlap_is_not_blurred(self):
        timeline = _build_timeline()
        analysis = _build_analysis()
        cfg = config({"blur": {"enabled": True, "render": {"shape": "rect", "margin_ratio": 0.0}}})
        mask = mask_builder.frame_mask(timeline, analysis, blur_decisions._empty(), cfg, 4.0)
        if mask is None:
            self.skipTest("PIL が使えないためマスクを作れません")
        pixels = np.asarray(mask)
        width, height = mask_builder.mask_size(timeline, cfg)
        scale = width / 1920.0
        row = int(500 * scale)
        # p1 は x=200〜600 / p2 は x=500〜900。重なり (500〜600) は守る = 0
        self.assertEqual(int(pixels[row, int(550 * scale)]), 0)
        # p2 だけの部分 (700〜900) はぼかす
        self.assertGreater(int(pixels[row, int(800 * scale)]), 200)
        # 守る形の境界のすぐ内側へ、フェザーが染みていない
        self.assertEqual(int(pixels[row, int(598 * scale)]), 0)

    # 守る形が無いフレームは、従来どおりぼかす層だけになる
    def test_no_keep_same_as_blur_layer(self):
        timeline = _build_timeline()
        analysis = _build_analysis()
        state = blur_decisions.with_identity(blur_decisions._empty(), "p1", blur_decisions.BLUR)
        cfg = config({"blur": {"enabled": True, "render": {"shape": "rect"}}})
        mask = mask_builder.frame_mask(timeline, analysis, state, cfg, 4.0)
        if mask is None:
            self.skipTest("PIL が使えないためマスクを作れません")
        pixels = np.asarray(mask)
        scale = pixels.shape[1] / 1920.0
        self.assertGreater(int(pixels[int(500 * scale), int(550 * scale)]), 200)


if __name__ == "__main__":
    unittest.main()
