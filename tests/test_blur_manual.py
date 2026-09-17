# 枠の削除・追加まわり (ver5 resolve3 §2.3 / §2.4 / §2.5 / §3.6 / §5.6〜§5.8) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・削除した領域の ID を使い回さず、再追加で古い追従トラックに化けないこと (§2.4 (a))
#   ・手で足した物を、その物の動きに合わせて追いかけること。見失ったら打ち切ること (§3.6)
#   ・削除・復帰・分割・領域の指定変更が Undo で戻せること (§5.6)
#   ・並んで座った 2 人を 1 人にまとめないこと。全身と上半身の重複枠は 1 人のまま (§5.8)
#   ・解析が「同じ人物にする」を当て込み、追加した枠の追従を作り直すこと (§2.5 (b)(c))
import os
import unittest
from unittest import mock

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.blur import analyzer, manual_tracker, region_tracker, tracker
from src.blur import decisions as blur_decisions
from src.blur.config import config
from src.timeline import commands
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
        Clip("c1", "m1", 0.0, 10.0, 0.0, 10.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
                  clips=[AudioClip("a1", "c1")])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1")
    return Timeline(fps=30, width=640, height=360,
                    source={"media_id": "m1", "input_path": __file__},
                    media_pool=media, tracks=[video, audio, subtitle])


# 黒地に白い四角 (模様入り) がある絵。位相相関が効くよう、四角の中に縞を入れる
def _frame(x, y, size=60, width=640, height=360):
    image = np.zeros((height, width, 3), dtype=np.uint8)
    patch = np.full((size, size, 3), 220, dtype=np.uint8)
    patch[::6, :, :] = 40
    patch[:, ::9, :] = 120
    image[y:y + size, x:x + size] = patch
    return image


class RegionIdTest(unittest.TestCase):

    # 削除しても番号を戻さず、解析結果に残った ID も避ける (§2.4 (a))
    def test_no_reuse_after_delete(self):
        state = blur_decisions._empty()
        first = blur_decisions.next_region_id(state)
        state = blur_decisions.with_region(state, {"id": first, "path": [[0, 0], [1, 0], [1, 1]]})
        state = blur_decisions.without_region(state, first)
        self.assertNotEqual(blur_decisions.next_region_id(state), first)

        # Undo で通し番号が戻っても、解析結果に残ったトラックの ID は使わない
        analysis = {"tracks": [{"identity": "r1", "kind": "region"},
                               {"identity": "r4", "kind": "manual"}]}
        self.assertEqual(blur_decisions.next_region_id(blur_decisions._empty(), analysis), "r5")

    # 形の指紋が違えば、同じ ID の古いトラックを捨てて作り直す (§5.7 (2))
    def test_missing_regions_uses_shape_hash(self):
        region = {"id": "r1", "kind": "place", "media_id": "m1", "anchor_sec": 1.0,
                  "path": [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]]}
        analysis = {"tracks": [{"id": "r1_m1_0", "identity": "r1", "kind": "region",
                                "shape_hash": "old", "samples": [{"t": 0}]}]}
        self.assertEqual(region_tracker.missing_regions(analysis, [region], "region"), [region])

        region_tracker.replace_tracks(analysis, region, [])
        # 古いトラックは消え、追従できなかった印 (空のサンプル) だけが残る
        self.assertEqual(len(analysis["tracks"]), 1)
        self.assertEqual(analysis["tracks"][0]["samples"], [])
        self.assertEqual(region_tracker.missing_regions(analysis, [region], "region"), [])

    # v1 の指定 (kind が無い / excluded が無い) をそのまま読めること (resolve3 Q7)
    def test_version1_decisions_load(self):
        timeline = _build_timeline()
        timeline.source["blur"] = {"version": 1, "identities": {"p2": "blur"},
                                   "regions": [{"id": "r1", "mode": "blur", "path": []}]}
        state = blur_decisions.load(timeline)
        self.assertEqual(blur_decisions.region_kind(state["regions"][0]), "place")
        self.assertEqual(state["excluded"], [])
        self.assertEqual(state["splits"], [])


class ManualFollowTest(unittest.TestCase):

    def setUp(self):
        self.cfg = config({"blur": {"enabled": True, "manual": {"match_psr": 5.0},
                                    "region": {"hold_sec": 0.4}}})

    # 検出器が拾えない物でも、切り出した範囲の位相相関で動きを追いかける
    def test_follows_moving_patch_by_correlation(self):
        follower = manual_tracker._Follower(self.cfg, {})
        frames = [(0.2 * (i + 1), _frame(100 + 8 * (i + 1), 120)) for i in range(5)]
        with mock.patch.object(manual_tracker.detector, "detect", return_value=[]):
            samples = follower.follow(iter(frames), _frame(100, 120), (100, 120, 60, 60),
                                      None, lambda _sec: None)
        self.assertEqual(len(samples), 5)
        self.assertAlmostEqual(samples[-1]["x"], 140, delta=3)
        self.assertAlmostEqual(samples[-1]["y"], 120, delta=3)

    # 物が消えたら hold_sec を超えたところで打ち切る
    def test_stops_when_lost(self):
        follower = manual_tracker._Follower(self.cfg, {})
        blank = np.zeros((360, 640, 3), dtype=np.uint8)
        frames = [(0.2 * (i + 1), blank) for i in range(10)]
        consumed = []

        def _iter():
            for item in frames:
                consumed.append(item[0])
                yield item

        with mock.patch.object(manual_tracker.detector, "detect", return_value=[]):
            samples = follower.follow(_iter(), _frame(100, 120), (100, 120, 60, 60),
                                      None, lambda _sec: None)
        self.assertEqual(samples, [])
        self.assertLess(len(consumed), len(frames), "見失った後もデコードし続けています")

    # 検出枠があれば、前の位置と一番重なる枠へ乗り換える
    def test_switches_to_best_detection(self):
        follower = manual_tracker._Follower(self.cfg, {})
        frames = [(0.2, _frame(110, 120))]
        # 切り出し範囲の中の座標で返る (範囲の左上は 100-60=40, 120-60=60)
        with mock.patch.object(manual_tracker.detector, "detect",
                               return_value=[(70.0, 60.0, 60.0, 60.0, 0.5),
                                             (300.0, 200.0, 40.0, 40.0, 0.9)]):
            samples = follower.follow(iter(frames), _frame(100, 120), (100, 120, 60, 60),
                                      None, lambda _sec: None)
        self.assertAlmostEqual(samples[0]["x"], 110.0)
        self.assertAlmostEqual(samples[0]["y"], 120.0)

    # 固定にした枠は、アンカーの前後 fixed_span_sec にだけ置く
    def test_fixed_object_span(self):
        timeline = _build_timeline()
        region = {"id": "r1", "kind": "object", "mode": "blur", "media_id": "m1",
                  "anchor_sec": 5.0, "follow": "fixed",
                  "path": [[0.1, 0.1], [0.3, 0.1], [0.3, 0.3], [0.1, 0.3]]}
        tracks = manual_tracker.track_object(timeline, {}, region, self.cfg)
        self.assertEqual(len(tracks), 1)
        span = self.cfg["manual"]["fixed_span_sec"]
        self.assertAlmostEqual(tracks[0]["samples"][0]["t"], 5.0 - span)
        self.assertAlmostEqual(tracks[0]["samples"][-1]["t"], 5.0 + span)
        self.assertEqual(tracks[0]["kind"], "manual")
        self.assertTrue(tracks[0].get("outline"))


class BlurCommandUndoTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build_timeline()
        self.stack = commands.CommandStack()
        self.anchor = {"media_id": "m1", "t": 2.0, "rect": [10.0, 20.0, 100.0, 200.0]}

    def _state(self):
        return blur_decisions.load(self.timeline)

    def test_exclude_restore_undo(self):
        self.assertTrue(self.stack.push(self.timeline, commands.ExcludeBlurTracks([self.anchor])))
        self.assertEqual(len(self._state()["excluded"]), 1)
        # 同じ枠をもう一度削除しても履歴を汚さない
        self.assertFalse(self.stack.push(self.timeline, commands.ExcludeBlurTracks([self.anchor])))
        self.assertTrue(self.stack.push(self.timeline, commands.RestoreBlurTrack(self.anchor)))
        self.assertEqual(self._state()["excluded"], [])
        self.stack.undo(self.timeline)
        self.assertEqual(len(self._state()["excluded"]), 1)

    def test_split_unsplit_undo(self):
        self.assertTrue(self.stack.push(self.timeline, commands.SplitBlurTrack(self.anchor)))
        self.assertEqual(self._state()["splits"][0]["as"], "s1")
        self.assertTrue(self.stack.push(self.timeline, commands.UnsplitBlurTrack(self.anchor)))
        self.assertEqual(self._state()["splits"], [])
        self.stack.undo(self.timeline)
        self.assertEqual(len(self._state()["splits"]), 1)

    def test_region_mode_and_follow(self):
        region = {"id": "r1", "kind": "object", "mode": "blur", "media_id": "m1",
                  "anchor_sec": 1.0, "path": [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]]}
        self.stack.push(self.timeline, commands.AddBlurRegion(region))
        self.assertEqual(self._state()["region_seq"], 1)
        self.assertTrue(self.stack.push(self.timeline, commands.SetBlurRegionMode("r1", "keep")))
        self.assertTrue(self.stack.push(self.timeline, commands.SetBlurRegionFollow("r1", "fixed")))
        region_now = self._state()["regions"][0]
        self.assertEqual((region_now["mode"], region_now["follow"]), ("keep", "fixed"))
        # 存在しない領域の変更は履歴を汚さない
        self.assertFalse(self.stack.push(self.timeline, commands.SetBlurRegionMode("r9", "keep")))
        self.stack.undo(self.timeline)
        self.stack.undo(self.timeline)
        self.assertEqual(self._state()["regions"][0]["mode"], "blur")


class SameBoxTest(unittest.TestCase):

    def setUp(self):
        self.cfg = config({"blur": {"analysis": {"sample_fps": 5.0, "min_track_sec": 0.0}}})

    # 依頼者の素材で 1 人にまとめられていた 2 人 (t1 / t4) の枠 (§2.5 (a))
    def test_side_by_side_people_stay_apart(self):
        left = tracker.Tracklet("t1", "m1")
        right = tracker.Tracklet("t4", "m1")
        for index in range(10):
            sec = index * 0.2
            left.add(sec, (0, 196, 811, 877, 0.9), np.array([1.0, 0.0], np.float32))
            right.add(sec, (452, 221, 460, 844, 0.9), np.array([0.99, 0.14], np.float32))
        identities = tracker.cluster([left, right], self.cfg)
        self.assertEqual(len(identities), 2)

    # 判定の条件は setting.json で変えられる (旧来の 0.5 / 中心を見ない に戻せる)
    def test_threshold_is_configurable(self):
        loose = (0.5, 99.0)
        self.assertTrue(tracker._same_box((0, 196, 811, 877), (452, 221, 460, 844), loose))
        self.assertFalse(tracker._same_box((0, 196, 811, 877), (452, 221, 460, 844), (0.85, 0.5)))


class SilhouetteCarryTest(unittest.TestCase):

    # 枠の形が大きく変わった (かがむなど) ら、前の輪郭を持ち越さない (resolve3 §3.3.3)
    def test_similar_size(self):
        self.assertTrue(analyzer._similar_size((0, 0, 500, 900), (40, 10, 560, 850)))
        # 実素材で立っている人がかがんだとき: 幅 484 → 622 (+29%)
        self.assertFalse(analyzer._similar_size((1213, 56, 484, 960), (992, 66, 622, 755)))


class AnalyzerIntegrationTest(unittest.TestCase):

    # 「同じ人物にする」の指定をクラスタリングへ渡す (§2.5 (b))
    def test_merges_are_passed_to_cluster(self):
        timeline = _build_timeline()
        state = blur_decisions.with_merge(blur_decisions._empty(), "p1", "p2")
        blur_decisions.store(timeline, state)
        settings = {"blur": {"enabled": True, "render": {"shape": "rounded"}}}

        with mock.patch.object(analyzer, "_analyze_span", return_value=True), \
                mock.patch.object(analyzer, "cluster", return_value=[]) as cluster, \
                mock.patch.object(analyzer.store, "load", return_value=None):
            analyzer.analyze(timeline, settings, None)
        self.assertEqual(cluster.call_args.kwargs.get("merges"), [["p1", "p2"]])

    # キャッシュを使うときも、追加した枠の追従が無ければ作り直す (§2.5 (c))
    def test_cached_analysis_rebuilds_added_tracks(self):
        timeline = _build_timeline()
        region = {"id": "r1", "kind": "place", "mode": "blur", "media_id": "m1",
                  "anchor_sec": 1.0, "path": [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]]}
        blur_decisions.store(timeline, blur_decisions.with_region(blur_decisions._empty(), region))
        cached = {"schema": 2, "tracks": [], "identities": []}
        settings = {"blur": {"enabled": True, "render": {"shape": "rounded"}}}

        with mock.patch.object(analyzer.store, "load", return_value=cached), \
                mock.patch.object(region_tracker, "build_region_tracks", return_value=1) as place, \
                mock.patch.object(manual_tracker, "build_manual_tracks", return_value=0) as manual, \
                mock.patch.object(analyzer.store, "save") as save:
            result = analyzer.analyze(timeline, settings, "cache.blur.json")
        self.assertIs(result, cached)
        self.assertTrue(place.called and manual.called)
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
