# 囲みの追従 (src/blur/tracker.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve8.md §3.4 / §5.5 / §8.1 D):
#   ・区切りはキーフレームと span の端で決まること
#   ・**大きさは変えず、位置だけを追うこと** (§3.3)
#   ・検出モデルが無い環境でも位相相関だけで追えること
#   ・見失ったら status=lost と見失った時刻を残すこと
#   ・キーの**中心**が変われば指紋も変わり、大きさだけ変えても変わらないこと (§5.10.5)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from src.blur import correlate, decisions as blur_decisions, store, tracker
from src.blur.config import config
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
    media = [MediaRef("m1", "video", __file__, 100.0, 640, 360, 30, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 4.0, 0.0, 4.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
                  clips=[AudioClip("a1", "c1")])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1")
    return Timeline(fps=30, width=640, height=360,
                    source={"media_id": "m1", "input_path": __file__},
                    media_pool=media, tracks=[video, audio, subtitle])


# 黒地に白い四角 (模様入り) がある絵。位相相関が効くよう、四角の中に縞を入れる。
# 背景を無地にするのは、動かない模様が相関を支配しないようにするため (tracker の注記)。
def _frame(x, y, size=60, width=640, height=360):
    image = np.zeros((height, width, 3), dtype=np.uint8)
    patch = np.full((size, size, 3), 220, dtype=np.uint8)
    patch[::6, :, :] = 40
    patch[:, ::9, :] = 120
    image[y:y + size, x:x + size] = patch
    return image


class SegmentTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build_timeline()
        self.cfg = config({"blur": {"enabled": True, "track": {"sample_fps": 10.0}}})
        self.span = blur_decisions.span_for(self.timeline, self.timeline.base_clips()[0])

    def _spec(self, times=(1.0,), rect=(0.3, 0.3, 0.1, 0.2)):
        decisions = blur_decisions.with_spec(
            blur_decisions._empty(),                  # noqa: SLF001
            blur_decisions.make_area_spec("b1", blur_decisions.BLUR, "m1", self.span,
                                          times[0], rect))
        for index, t in enumerate(times[1:], 1):
            moved = (rect[0] + 0.05 * index, rect[1], rect[2], rect[3])
            decisions = blur_decisions.with_key(decisions, "b1", t, moved)
        return decisions["specs"][0]

    # D5: キー 3 件 + span で 4 区切り (後ろ向き 1 + 前向き 3)
    def test_segments_for(self):
        segments = tracker.segments_for(self._spec((1.0, 2.0, 3.0)), 10.0)
        self.assertEqual([(s["dir"], s["start"], s["end"]) for s in segments],
                         [("back", 0.0, 1.0), ("fwd", 1.0, 2.0), ("fwd", 2.0, 3.0),
                          ("fwd", 3.0, 4.0)])

    # D6: 短すぎる区切りは作らないこと
    def test_short_segment_is_skipped(self):
        # キーが span の端にあるので前向き 1 本だけになる
        segments = tracker.segments_for(self._spec((0.0,)), 10.0)
        self.assertEqual([(s["dir"], s["start"], s["end"]) for s in segments],
                         [("fwd", 0.0, 4.0)])

    # 「動かさない」指定は追わないこと
    def test_fixed_has_no_segments(self):
        spec = dict(self._spec(), follow=blur_decisions.FOLLOW_FIXED)
        self.assertEqual(tracker.segments_for(spec, 10.0), [])

    # D7: キーの中心を動かすと指紋が変わること
    def test_moving_a_key_changes_the_hash(self):
        before = tracker.segments_for(self._spec((1.0, 2.0)), 10.0)
        after = tracker.segments_for(
            self._spec((1.0, 2.0), rect=(0.4, 0.3, 0.1, 0.2)), 10.0)
        self.assertNotEqual([s["hash"] for s in before], [s["hash"] for s in after])

    # D8: 大きさだけ変えても指紋は変わらないこと (追い直さない)
    def test_resizing_keeps_the_hash(self):
        spec = self._spec((1.0, 2.0), rect=(0.3, 0.3, 0.1, 0.2))
        # 中心を保ったまま 2 倍に広げる
        wide = self._spec((1.0, 2.0), rect=(0.25, 0.2, 0.2, 0.4))
        self.assertEqual([s["hash"] for s in tracker.segments_for(spec, 10.0)],
                         [s["hash"] for s in tracker.segments_for(wide, 10.0)])


class FollowTest(unittest.TestCase):
    """追従の本体 (検出モデルが無い環境 = 位相相関だけ / D1〜D4 / D10)"""

    def setUp(self):
        self.cfg = config({"blur": {"enabled": True,
                                    "model": {"detector": "models/does_not_exist.onnx"},
                                    "track": {"sample_fps": 5.0, "match_psr": 5.0,
                                              "hold_sec": 0.4}}})

    # D1 / D3 / D4: パッチを追い、大きさは変えないこと (検出モデルは無い)
    def test_follows_a_moving_patch(self):
        follower = tracker._Follower(self.cfg)      # noqa: SLF001
        frames = [(0.2 * (index + 1), _frame(100 + 8 * (index + 1), 120)) for index in range(5)]
        samples, lost = follower.follow(iter(frames), (100.0, 120.0, 60.0, 60.0), None,
                                        lambda _sec: None, _frame(100, 120))
        self.assertEqual(len(samples), 5)
        self.assertIsNone(lost)
        self.assertAlmostEqual(samples[-1]["x"], 140, delta=3)
        self.assertAlmostEqual(samples[-1]["y"], 120, delta=3)
        # 大きさは初期値のまま (位置だけを追う / §3.3)
        self.assertEqual({(s["w"], s["h"]) for s in samples}, {(60.0, 60.0)})

    # D2: 物が消えたら hold_sec を超えた所で打ち切り、見失った時刻を残すこと
    def test_lost_when_the_patch_disappears(self):
        follower = tracker._Follower(self.cfg)      # noqa: SLF001
        blank = np.zeros((360, 640, 3), dtype=np.uint8)
        frames = [(0.2 * (index + 1), blank) for index in range(10)]
        samples, lost = follower.follow(iter(frames), (100.0, 120.0, 60.0, 60.0), None,
                                        lambda _sec: None, _frame(100, 120))
        self.assertEqual(samples, [])
        self.assertIsNotNone(lost, "見失ったことを報せていません")
        self.assertAlmostEqual(lost, 0.2, delta=1e-6)

    # D10: 途中でキャンセルすると追えた所までが残ること
    def test_cancel_keeps_what_was_found(self):
        follower = tracker._Follower(self.cfg)      # noqa: SLF001
        frames = [(0.2 * (index + 1), _frame(100 + 8 * (index + 1), 120)) for index in range(10)]
        state = {"count": 0}

        def cancel():
            state["count"] += 1
            return state["count"] > 3

        samples, _lost = follower.follow(iter(frames), (100.0, 120.0, 60.0, 60.0), cancel,
                                         lambda _sec: None, _frame(100, 120))
        self.assertLessEqual(len(samples), 3)
        self.assertGreater(len(samples), 0)

    # 位相相関そのもの: 平行移動が求まること
    def test_phase_correlate(self):
        before = correlate.to_gray(_frame(100, 120))
        after = correlate.to_gray(_frame(115, 120))
        dx, dy, _peak, psr = correlate.phase_correlate(before, after)
        self.assertGreater(psr, 5.0)
        self.assertAlmostEqual(dx, -15.0, delta=3.0)
        self.assertAlmostEqual(dy, 0.0, delta=3.0)


class EnsureTracksTest(unittest.TestCase):
    """D9: 指紋の合う区切りは追い直さないこと"""

    def setUp(self):
        self.timeline = _build_timeline()
        self.cfg = config({"blur": {"enabled": True, "track": {"sample_fps": 10.0}}})
        self.span = blur_decisions.span_for(self.timeline, self.timeline.base_clips()[0])
        self.decisions = blur_decisions.with_spec(
            blur_decisions._empty(),                  # noqa: SLF001
            blur_decisions.make_area_spec("b1", blur_decisions.BLUR, "m1", self.span,
                                          1.0, (0.3, 0.3, 0.1, 0.2)))
        self.tracks = store.new_tracks(self.timeline, self.cfg, ["m1"])

    # 追従結果を入れておけば、もう一度呼んでも 0 件になること
    def test_existing_segments_are_not_retracked(self):
        wanted = tracker.segments_for(self.decisions["specs"][0],
                                      self.cfg["track"]["sample_fps"])
        for segment in wanted:
            store.put_segment(self.tracks, segment, {
                "hash": segment["hash"], "dir": segment["dir"], "start": segment["start"],
                "end": segment["end"], "status": "ok", "lost_sec": None,
                "samples": [{"t": segment["start"], "cx": 0.35, "cy": 0.4, "score": 1.0}]})
        count = tracker.ensure_tracks(self.timeline, {}, self.tracks, self.decisions, self.cfg)
        self.assertEqual(count, 0)

    # 指定を消したら追従結果も捨てられること (E5)
    def test_dropped_spec_is_cleaned(self):
        segment = tracker.segments_for(self.decisions["specs"][0], 10.0)[0]
        store.put_segment(self.tracks, segment, {
            "hash": segment["hash"], "dir": segment["dir"], "start": segment["start"],
            "end": segment["end"], "status": "ok", "lost_sec": None, "samples": []})
        empty = blur_decisions.without_spec(self.decisions, "b1")
        store.drop_unused(self.tracks, empty, [])
        self.assertEqual(self.tracks["tracks"], {})


if __name__ == "__main__":
    unittest.main()
