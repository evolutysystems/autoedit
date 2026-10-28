# ドラッグ中の描画と、編集後の絵の取り直しの間引き (docs/request/ver9/resolve7.md)
# 実行: python -m unittest discover -s tests
#
# 方針: 「掴んでいる間は軽く、離したら元どおり」
#
# 重点:
#   ・ドラッグ中は波形を描かないこと (対策A)。描画時間の半分以上が波形で、
#     掴んでいる間はマウスを動かすたびに画面全体を描き直すため。
#   ・**離したら必ず描くこと。** ここが戻らないと「波形が消えた」不具合になる。
#   ・設定で元の挙動へ戻せること (hide_while_dragging=false)。
#   ・編集後の絵の取り直しが即時ではなく、再生ヘッド移動と同じ経路で
#     まとめられること (対策D)。1 回ごとにフレームのデコードが走らないため。
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    from src.gui.timeline import preview_panel, timeline_view
    from src.gui.timeline.timeline_controller import TimelineController
    from src.timeline.builder import timeline_config
    from src.timeline.model import (
        BASE_AUDIO_TRACK_ID,
        BASE_VIDEO_TRACK_ID,
        TRACK_AUDIO,
        TRACK_VIDEO,
        AudioClip,
        Clip,
        MediaRef,
        Timeline,
        Track,
    )
    _QT_AVAILABLE = True
except ImportError:  # pragma: no cover (PySide6 が無い環境)
    _QT_AVAILABLE = False

_SETTINGS = {
    "ffmpeg": {"output_width": 1920, "output_height": 1080, "output_fps": 60},
    "subtitle": {},
    "vertical": {},
}


def _timeline():
    media = MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60.0, True)
    timeline = Timeline(fps=60, width=1920, height=1080,
                        source={"media_id": "m1", "input_path": __file__},
                        media_pool=[media])
    video = Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, name="Video 1", is_base=True)
    audio = Track(BASE_AUDIO_TRACK_ID, TRACK_AUDIO, 1, name="Audio 1",
                  link_track=BASE_VIDEO_TRACK_ID)
    video.clips.append(Clip("c1", "m1", 0.0, 10.0, 0.0, source_out=10.0))
    audio.clips.append(AudioClip("a1", "c1"))
    timeline.tracks = [video, audio]
    return timeline


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が無い環境ではスキップ")
class WaveformWhileDraggingTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.timeline = _timeline()
        self.controller = TimelineController(self.timeline, _SETTINGS)
        self.view = timeline_view.TimelineView(self.controller)
        self.view.resize(1200, 500)
        self.addCleanup(self.view.shutdown)

    # 波形を引きに行ったかどうかで判定する (実際の描画はしない)
    def _asked_for_peaks(self):
        audio = self.timeline.tracks[1].clips[0]
        rect = timeline_view.QRect(0, 0, 600, 60)
        with mock.patch.object(self.view._waveform, "peaks_for",
                               return_value=None) as asked:
            self.view._paint_waveform(None, audio, rect,
                                      timeline_view._COLOR_AUDIO)
        return asked.called

    # 掴んでいないときは従来どおり波形を引く
    def test_waveform_is_drawn_when_idle(self):
        self.view._drag = timeline_view._DRAG_NONE
        self.assertTrue(self._asked_for_peaks())

    # 本題: クリップの移動・トリム中は引きに行かない (対策A)
    def test_waveform_is_skipped_while_dragging(self):
        for drag in (timeline_view._DRAG_MOVE,
                     timeline_view._DRAG_TRIM_LEFT,
                     timeline_view._DRAG_TRIM_RIGHT):
            self.view._drag = drag
            self.assertFalse(self._asked_for_peaks(), f"drag={drag}")

    # 再生ヘッドのスクラブ中も省く (ここもマウス移動ごとに描き直す)
    def test_waveform_is_skipped_while_scrubbing(self):
        self.view._drag = timeline_view._DRAG_PLAYHEAD
        self.assertFalse(self._asked_for_peaks())

    # **離したら必ず戻る。** 戻らないと「波形が消えた」不具合になる
    def test_waveform_returns_after_the_drag(self):
        self.view._drag = timeline_view._DRAG_MOVE
        self.assertFalse(self._asked_for_peaks())
        self.view._drag = timeline_view._DRAG_NONE
        self.assertTrue(self._asked_for_peaks())

    # 設定で従来の挙動へ戻せること (切り戻し用)
    def test_setting_can_keep_the_waveform(self):
        self.view._waveform_hide_while_dragging = False
        self.view._drag = timeline_view._DRAG_MOVE
        self.assertTrue(self._asked_for_peaks())

    # 既定は「省く」
    def test_default_is_to_hide(self):
        self.assertTrue(timeline_config({})["waveform"]["hide_while_dragging"])
        self.assertTrue(self.view._waveform_hide_while_dragging)


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が無い環境ではスキップ")
class RefreshDebounceTest(unittest.TestCase):
    """編集後の絵の取り直しをまとめること (対策D)"""

    # 本題: 編集後は即時ではなく、再生ヘッド移動と同じ間引きの経路を通る。
    # 即時だと編集 1 回ごとにフレームのデコード (実測 32〜44ms) が走る。
    def test_timeline_change_goes_through_the_debounce(self):
        panel = mock.Mock(spec_set=[
            "_audio_source", "_proxy", "_update_time_label", "_schedule_refresh",
            "_refresh_all",
        ])
        panel._proxy = None
        preview_panel.PreviewPanel._on_timeline_changed(panel)
        panel._schedule_refresh.assert_called_once_with()
        panel._refresh_all.assert_not_called()

    # 時刻表示だけは即時にする (反映が遅れた感じを出さない)
    def test_time_label_is_updated_at_once(self):
        panel = mock.Mock(spec_set=[
            "_audio_source", "_proxy", "_update_time_label", "_schedule_refresh",
        ])
        panel._proxy = None
        preview_panel.PreviewPanel._on_timeline_changed(panel)
        panel._update_time_label.assert_called_once_with()

    # 音声とプロキシの無効化は従来どおり行う (ここを飛ばすと古い音・古い絵が残る)
    def test_audio_and_proxy_are_invalidated(self):
        panel = mock.Mock(spec_set=[
            "_audio_source", "_proxy", "_update_time_label", "_schedule_refresh",
        ])
        preview_panel.PreviewPanel._on_timeline_changed(panel)
        panel._audio_source.invalidate.assert_called_once_with()
        panel._proxy.invalidate.assert_called_once_with()

    # 間引きの幅は設定から取る (既定 100ms / 対策E)
    def test_debounce_default_is_100ms(self):
        self.assertEqual(timeline_config({})["preview"]["update_debounce_ms"], 100)


if __name__ == "__main__":
    unittest.main()
