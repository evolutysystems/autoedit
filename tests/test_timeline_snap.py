# 吸着 (TimelineController.snap_sec / snap_playhead_sec) の単体テスト
# 実行: python -m unittest discover -s tests
# resolve2 §5.2: 編集点への吸着を 1 か所へ集約し、クリップドラッグと再生ヘッドで共有する。
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QCoreApplication
    from src.gui.timeline.timeline_controller import TimelineController
    _QT_AVAILABLE = True
except Exception:  # noqa: BLE001 (PySide6 が無い環境ではスキップする)
    _QT_AVAILABLE = False

from src.timeline.model import (
    ORIGIN_SILENCE_CUT,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    AudioClip,
    Clip,
    MediaRef,
    SubtitleClip,
    Timeline,
    Track,
)

# 編集点は 0 / 10 / 20 / 30 秒に立つ
_EDIT_POINTS = (0.0, 10.0, 20.0, 30.0)


def _build_timeline():
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 10.0, 0.0, 10.0, origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c2", "m1", 10.0, 10.0, 20.0, 30.0, origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c3", "m1", 20.0, 10.0, 50.0, 60.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=[
        AudioClip("a1", "c1"), AudioClip("a2", "c2"), AudioClip("a3", "c3"),
    ])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1", clips=[
        SubtitleClip("s1", 3.0, 2.0, "字幕"),
    ])
    return Timeline(fps=60, source={"media_id": "m1", "duration_sec": 100.0},
                    media_pool=media, tracks=[video, audio, subtitle])


# 設定: 1 秒 = 10px、吸着閾値 8px → 0.8 秒以内なら吸着する
def _settings(**overrides):
    timeline = {
        "enabled": True,
        "snap_enabled": True,
        "snap_playhead": True,
        "snap_threshold_px": 8,
        "default_zoom_px_per_sec": 10,
    }
    timeline.update(overrides)
    return {"timeline": timeline}


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できないためスキップ")
class _ControllerCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # QObject の生成にアプリケーションインスタンスを用意する (GUI は出さない)
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def _controller(self, **overrides):
        controller = TimelineController(_build_timeline(), _settings(**overrides))
        controller.set_zoom(10.0)       # 1 秒 = 10px → 閾値 0.8 秒
        return controller


class SnapTargetTest(_ControllerCase):

    # 編集点・先頭・全長・再生ヘッドが候補に入る
    def test_targets_include_edit_points(self):
        controller = self._controller()
        targets = set(controller.snap_targets())
        for point in _EDIT_POINTS:
            self.assertIn(point, targets, f"{point} 秒の編集点が候補に無い")

    # 音声トラックは候補に含めない (V1 と同じ値が重複するだけ)
    def test_audio_track_is_excluded(self):
        controller = self._controller()
        # A1 を消しても候補は変わらない
        before = sorted(controller.snap_targets())
        controller.timeline.tracks = [
            t for t in controller.timeline.tracks if not t.is_audio()]
        self.assertEqual(sorted(controller.snap_targets()), before)

    # ドラッグ中のクリップ自身の端は候補から外れる
    def test_exclude_id_removes_own_edges(self):
        controller = self._controller()
        # c2 の端 (10.0 / 20.0) は隣接する c1 / c3 の端でもあるため、
        # c2 だけを残したトラックにして自分の端が消えることを確かめる
        controller.timeline.base_video_track().clips = [
            controller.timeline.clip_by_id("c2")]
        targets = controller.snap_targets(exclude_id="c2", include_playhead=False)
        self.assertNotIn(10.0, targets, "自分自身の開始位置へ吸着してはいけない")
        # 全長 (= c2 の終端 20.0) はタイムラインの端として候補に残る
        self.assertIn(20.0, targets)

    # 再生ヘッドを候補から外せる (再生ヘッド自身を動かすとき)
    def test_playhead_can_be_excluded(self):
        controller = self._controller()
        controller.set_playhead(7.0)
        self.assertIn(7.0, controller.snap_targets(include_playhead=True))
        self.assertNotIn(7.0, controller.snap_targets(include_playhead=False))


class SnapSecTest(_ControllerCase):

    # 閾値内なら編集点へ吸着する
    def test_snaps_within_threshold(self):
        controller = self._controller()
        self.assertAlmostEqual(controller.snap_sec(10.5), 10.0)
        self.assertAlmostEqual(controller.snap_sec(9.5), 10.0)

    # 閾値の外なら元の値のまま
    def test_does_not_snap_outside_threshold(self):
        controller = self._controller()
        self.assertAlmostEqual(controller.snap_sec(15.0), 15.0)

    # 拡大すると閾値 (px 基準) が短くなり、吸着しにくくなる
    def test_threshold_follows_zoom(self):
        controller = self._controller()
        controller.set_zoom(100.0)      # 1 秒 = 100px → 閾値 0.08 秒
        self.assertAlmostEqual(controller.snap_sec(10.5), 10.5)
        self.assertAlmostEqual(controller.snap_sec(10.05), 10.0)

    # 設定で吸着を切れる
    def test_snap_can_be_disabled(self):
        controller = self._controller(snap_enabled=False)
        self.assertAlmostEqual(controller.snap_sec(10.5), 10.5)

    # 最も近い候補へ吸着する
    def test_snaps_to_nearest(self):
        controller = self._controller()
        self.assertAlmostEqual(controller.snap_sec(19.6), 20.0)
        self.assertAlmostEqual(controller.snap_sec(20.4), 20.0)


class SnapPlayheadTest(_ControllerCase):
    """resolve2 §5.2-2 / R1: 再生ヘッドも編集点へ吸着する"""

    # 再生ヘッド用の吸着が効く
    def test_playhead_snaps_to_edit_point(self):
        controller = self._controller()
        self.assertAlmostEqual(controller.snap_playhead_sec(10.4), 10.0)

    # 再生ヘッド自身へは吸着しない (動かなくなるのを防ぐ)
    def test_playhead_does_not_snap_to_itself(self):
        controller = self._controller()
        controller.set_playhead(15.0)
        # 15.2 は再生ヘッド (15.0) の近くだが、自分自身は候補から外れているため動かない
        self.assertAlmostEqual(controller.snap_playhead_sec(15.2), 15.2)

    # snap_playhead=false で再生ヘッドの吸着だけを切れる
    def test_playhead_snap_can_be_disabled_alone(self):
        controller = self._controller(snap_playhead=False)
        self.assertAlmostEqual(controller.snap_playhead_sec(10.4), 10.4)
        # クリップドラッグ側の吸着は生きたまま
        self.assertAlmostEqual(controller.snap_sec(10.4), 10.0)


class ClipAtPlayheadTest(_ControllerCase):
    """resolve2 §5.3-3 / 回答 Q3: A / D の対象クリップの決め方"""

    # 選択が無ければ V1 で再生ヘッドを含むクリップ
    def test_falls_back_to_base_track(self):
        controller = self._controller()
        controller.set_playhead(15.0)
        self.assertEqual(controller.clip_at_playhead().id, "c2")

    # 選択中クリップが再生ヘッドを含んでいればそれを優先する
    def test_selected_clip_wins(self):
        controller = self._controller()
        controller.set_playhead(4.0)
        controller.select(["s1"])       # 字幕 s1 は [3.0, 5.0]
        self.assertEqual(controller.clip_at_playhead().id, "s1")

    # 選択中クリップが再生ヘッドを含まなければ V1 へフォールバックする
    def test_selection_outside_playhead_falls_back(self):
        controller = self._controller()
        controller.set_playhead(15.0)
        controller.select(["s1"])
        self.assertEqual(controller.clip_at_playhead().id, "c2")

    # 再生ヘッド上にクリップが無ければ None
    def test_returns_none_when_empty(self):
        controller = self._controller()
        controller.timeline.base_video_track().clips = []
        controller.set_playhead(15.0)
        self.assertIsNone(controller.clip_at_playhead())


if __name__ == "__main__":
    unittest.main()
