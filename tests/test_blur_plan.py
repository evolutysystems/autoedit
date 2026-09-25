# キーフレーム + 追従の合成 (src/blur/plan.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve8.md §5.4 / §8.1 B):
#   ・**キーフレームの時刻ではキーの矩形になること** (人の指示が追従より強い / §4-7)
#   ・追従の誤差が区切り全体へ配られ、次のキーフレームで飛ばないこと
#   ・大きさはキーフレームの線形補間で決まること (追従は位置だけを追う / §3.3)
#   ・span の外では 1 フレームも効かないこと (R11)
#   ・見失った先は「ボカす = 保持 / ボカさない = 打ち切り」になること (§4-8)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.blur import decisions as blur_decisions
from src.blur import tracker
from src.blur.config import config
from src.blur.plan import STATUS_FAILED, STATUS_LOST, BlurPlan
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


# ベースクリップ 1 本 (0〜5 秒)。素材の時刻もそのまま 0〜5 秒。
def _build_timeline(width=1920, height=1080, media_size=(1920, 1080)):
    media = [MediaRef("m1", "video", __file__, 100.0, media_size[0], media_size[1], 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 5.0, 0.0, 5.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
                  clips=[AudioClip("a1", "c1")])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1")
    return Timeline(fps=60, width=width, height=height,
                    source={"media_id": "m1", "input_path": __file__},
                    media_pool=media, tracks=[video, audio, subtitle])


# 追従結果の入れ物を 1 区切りぶん作る
def _tracks(spec, segment, samples, status="ok", lost_sec=None):
    return {"schema": 5, "tracks": {str(spec["id"]): {"media_id": str(spec["media_id"]),
            "segments": [{"hash": segment["hash"], "dir": segment["dir"],
                          "start": segment["start"], "end": segment["end"],
                          "status": status, "lost_sec": lost_sec, "samples": samples}]}}}


def _center(rect):
    return (round(rect[0] + rect[2] / 2.0, 4), round(rect[1] + rect[3] / 2.0, 4))


class RectAtTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build_timeline()
        self.cfg = config({"blur": {"enabled": True}})
        self.clip = self.timeline.base_clips()[0]
        self.span = blur_decisions.span_for(self.timeline, self.clip)
        # キー 2 点: 1.0 秒 (中心 0.35) → 3.0 秒 (中心 0.55)
        decisions = blur_decisions.with_spec(
            blur_decisions._empty(),                  # noqa: SLF001 (テストのため内部を使う)
            blur_decisions.make_area_spec("b1", blur_decisions.BLUR, "m1", self.span,
                                          1.0, (0.3, 0.2, 0.1, 0.3)))
        self.decisions = blur_decisions.with_key(decisions, "b1", 3.0, (0.5, 0.2, 0.1, 0.3))
        self.spec = self.decisions["specs"][0]
        segments = tracker.segments_for(self.spec, self.cfg["track"]["sample_fps"])
        self.middle = next(s for s in segments
                           if s["dir"] == "fwd" and abs(s["start"] - 1.0) < 1e-6)
        self.head = next(s for s in segments if s["dir"] == "back")

    # 追従サンプル (1.0〜3.0 秒 / 終端で error ぶんずれる)
    def _samples(self, error=0.0, upto=3.0):
        samples = []
        steps = int(round((upto - 1.0) / 0.1))
        for index in range(steps + 1):
            t = round(1.0 + index * 0.1, 3)
            ratio = index / max(steps, 1)
            samples.append({"t": t, "cx": 0.35 + (0.20 - error) * ratio, "cy": 0.35,
                            "score": 1.0})
        return samples

    def _plan(self, tracks=None, decisions=None):
        return BlurPlan(self.timeline, tracks, decisions or self.decisions, self.cfg)

    # B1: キーフレームの時刻は、追従があってもキーの矩形になること
    def test_key_time_uses_the_key(self):
        plan = self._plan(_tracks(self.spec, self.middle, self._samples(error=0.02)))
        self.assertEqual(_center(plan.rect_at(self.spec, 1.0)), (0.35, 0.35))
        self.assertEqual(_center(plan.rect_at(self.spec, 3.0)), (0.55, 0.35))

    # B2: 追従が無くてもキーフレームの時刻はキーの矩形になること
    def test_key_time_without_tracking(self):
        plan = self._plan()
        self.assertEqual(_center(plan.rect_at(self.spec, 1.0)), (0.35, 0.35))
        self.assertEqual(_center(plan.rect_at(self.spec, 3.0)), (0.55, 0.35))

    # B3: 追従が無ければ間は線形補間になること
    def test_linear_without_tracking(self):
        plan = self._plan()
        self.assertEqual(_center(plan.rect_at(self.spec, 2.0)), (0.45, 0.35))

    # B4: 追従の誤差は区切り全体へ配られ、次のキーで飛ばないこと
    def test_error_is_spread(self):
        plan = self._plan(_tracks(self.spec, self.middle, self._samples(error=0.02)))
        # 追従だけなら 2.0 秒で 0.44。誤差 0.02 の半分を足して 0.45 になる
        self.assertEqual(_center(plan.rect_at(self.spec, 2.0)), (0.45, 0.35))
        # 直前のコマでもキーの位置へ十分近い (飛ばない)
        self.assertAlmostEqual(_center(plan.rect_at(self.spec, 2.98))[0], 0.55, places=2)

    # B5: 大きさはキーの線形補間。位置は追従に従うこと
    def test_size_is_interpolated(self):
        decisions = blur_decisions.with_key(self.decisions, "b1", 3.0, (0.5, 0.2, 0.2, 0.3))
        spec = decisions["specs"][0]
        plan = self._plan(_tracks(spec, self.middle, self._samples()), decisions)
        self.assertAlmostEqual(plan.rect_at(spec, 2.0)[2], 0.15, places=4)
        self.assertAlmostEqual(plan.rect_at(spec, 1.0)[2], 0.1, places=4)
        self.assertAlmostEqual(plan.rect_at(spec, 3.0)[2], 0.2, places=4)

    # B6: span の外では位置を返さないこと
    def test_outside_the_span(self):
        plan = self._plan()
        self.assertIsNone(plan.rect_at(self.spec, 5.5))
        self.assertIsNone(plan.rect_at(self.spec, -0.5))

    # B7: 見失った先 / ボカす = 最後に追えた位置を保持すること
    def test_lost_blur_holds(self):
        # 末尾の区切り (3.0〜5.0 秒) で 4.0 秒に見失う
        tail = next(s for s in tracker.segments_for(self.spec, 10.0)
                    if s["dir"] == "fwd" and abs(s["start"] - 3.0) < 1e-6)
        samples = [{"t": 3.0 + i * 0.1, "cx": 0.55 + i * 0.01, "cy": 0.35, "score": 1.0}
                   for i in range(11)]
        plan = self._plan(_tracks(self.spec, tail, samples, status="lost", lost_sec=4.0))
        held = plan.rect_at(self.spec, 4.8)
        self.assertIsNotNone(held)
        self.assertEqual(_center(held)[0], round(samples[-1]["cx"], 4))

    # B8: 見失った先 / ボカさない = 穴を閉じること (位置を返さない)
    def test_lost_keep_disappears(self):
        decisions = blur_decisions.with_spec_mode(self.decisions, "b1", blur_decisions.KEEP)
        spec = decisions["specs"][0]
        tail = next(s for s in tracker.segments_for(spec, 10.0)
                    if s["dir"] == "fwd" and abs(s["start"] - 3.0) < 1e-6)
        samples = [{"t": 3.0 + i * 0.1, "cx": 0.55, "cy": 0.35, "score": 1.0} for i in range(11)]
        plan = self._plan(_tracks(spec, tail, samples, status="lost", lost_sec=4.0), decisions)
        self.assertIsNone(plan.rect_at(spec, 4.8))

    # B9: 見失っても次のキーフレームがあれば、そこへ向かって結ぶこと
    def test_lost_walks_to_the_next_key(self):
        # 2.0 秒 (中心 0.45) までしか追えなかった場合
        samples = [{"t": round(1.0 + i * 0.1, 3), "cx": 0.35 + 0.10 * (i / 10.0),
                    "cy": 0.35, "score": 1.0} for i in range(11)]
        plan = self._plan(_tracks(self.spec, self.middle, samples,
                                  status="lost", lost_sec=2.0))
        # 2.0 秒の位置 (0.45) と 3.0 秒のキー (0.55) の中間
        self.assertAlmostEqual(_center(plan.rect_at(self.spec, 2.5))[0], 0.50, places=2)

    # B10: 「動かさない」なら追従結果を使わないこと
    def test_fixed_ignores_tracking(self):
        decisions = blur_decisions.with_spec_follow(self.decisions, "b1",
                                                    blur_decisions.FOLLOW_FIXED)
        spec = decisions["specs"][0]
        samples = [{"t": round(1.0 + i * 0.1, 3), "cx": 0.35, "cy": 0.80, "score": 1.0}
                   for i in range(21)]
        plan = self._plan(_tracks(spec, self.middle, samples), decisions)
        # 追従を使っていたら中心の y が 0.80 になる
        self.assertEqual(_center(plan.rect_at(spec, 2.0)), (0.45, 0.35))

    # 先頭の区切り (後ろ向き) は追従の位置をそのまま使うこと
    def test_backward_segment(self):
        samples = [{"t": round(i * 0.1, 3), "cx": 0.20 + i * 0.015, "cy": 0.35, "score": 1.0}
                   for i in range(11)]
        plan = self._plan(_tracks(self.spec, self.head, samples))
        self.assertEqual(_center(plan.rect_at(self.spec, 0.5))[0], round(samples[5]["cx"], 4))


class LayersTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build_timeline()
        self.cfg = config({"blur": {"enabled": True}})
        self.clip = self.timeline.base_clips()[0]
        span = blur_decisions.span_for(self.timeline, self.clip)
        decisions = blur_decisions.with_spec(
            blur_decisions._empty(),                  # noqa: SLF001
            blur_decisions.make_frame_spec("b1", "m1", span), to_bottom=True)
        decisions = blur_decisions.with_spec(decisions, blur_decisions.make_area_spec(
            "b2", blur_decisions.KEEP, "m1", span, 1.0, (0.3, 0.2, 0.1, 0.3)))
        decisions = blur_decisions.with_spec(decisions, blur_decisions.make_area_spec(
            "b3", blur_decisions.BLUR, "m1", span, 1.0, (0.32, 0.22, 0.03, 0.05)))
        self.decisions = decisions
        self.plan = BlurPlan(self.timeline, None, decisions, self.cfg)

    # B11: 重ね順のまま返ること
    def test_layer_order(self):
        layers = self.plan.layers_at("m1", 1.0)
        self.assertEqual([layer["spec"]["id"] for layer in layers], ["b1", "b2", "b3"])
        self.assertEqual([layer["mode"] for layer in layers], ["blur", "keep", "blur"])

    # span の外では 1 件も返らないこと
    def test_outside_the_span(self):
        self.assertEqual(self.plan.layers_at("m1", 6.0), [])
        self.assertEqual(self.plan.layers_at("m2", 1.0), [])

    # 画面全体の形はキャンバス全体になること
    def test_frame_shape(self):
        shape = self.plan.layers_at("m1", 1.0)[0]["shape"]
        self.assertEqual(shape["kind"], blur_decisions.KIND_FRAME)
        self.assertEqual(shape["rect"], (0.0, 0.0, 1920.0, 1080.0))

    # 既定の呼び名と説明
    def test_labels(self):
        specs = self.decisions["specs"]
        self.assertEqual(self.plan.label_of(specs[0]), "画面全体をぼかす")
        self.assertEqual(self.plan.label_of(specs[1]), "ボカさない 1")
        self.assertEqual(self.plan.label_of(specs[2]), "ボカす 2")
        self.assertIn("キー 1 点", self.plan.describe(specs[1]))

    # マスクの要否
    def test_has_blur(self):
        self.assertTrue(self.plan.has_blur())
        keep_only = blur_decisions.without_spec(
            blur_decisions.without_spec(self.decisions, "b1"), "b3")
        self.assertFalse(BlurPlan(self.timeline, None, keep_only, self.cfg).has_blur())

    # 追従の具合を伝えること
    def test_status(self):
        spec = self.decisions["specs"][1]
        segment = tracker.segments_for(spec, 10.0)[0]
        lost = _tracks(spec, segment, [{"t": 0.5, "cx": 0.35, "cy": 0.35, "score": 1.0}],
                       status="lost", lost_sec=0.5)
        self.assertEqual(BlurPlan(self.timeline, lost, self.decisions, self.cfg)
                         .status_of(spec)["status"], STATUS_LOST)
        failed = _tracks(spec, segment, [], status="failed")
        self.assertEqual(BlurPlan(self.timeline, failed, self.decisions, self.cfg)
                         .status_of(spec)["status"], STATUS_FAILED)
        self.assertIsNone(self.plan.status_of(self.decisions["specs"][0])["status"])


class LetterboxTest(unittest.TestCase):
    """B12: レターボックスのある素材でも正規化座標 → キャンバス px が合うこと"""

    def test_portrait_canvas(self):
        timeline = _build_timeline(width=1080, height=1920, media_size=(1920, 1080))
        cfg = config({"blur": {"enabled": True}})
        span = blur_decisions.span_for(timeline, timeline.base_clips()[0])
        decisions = blur_decisions.with_spec(
            blur_decisions._empty(),                  # noqa: SLF001
            blur_decisions.make_area_spec("b1", blur_decisions.BLUR, "m1", span,
                                          1.0, (0.25, 0.4, 0.5, 0.2)))
        plan = BlurPlan(timeline, None, decisions, cfg)
        rect = plan.canvas_rect_at(decisions["specs"][0], 1.0)
        self.assertEqual([round(v) for v in rect], [270, 768, 540, 384])


if __name__ == "__main__":
    unittest.main()
