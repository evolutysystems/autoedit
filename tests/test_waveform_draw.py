# 波形の描き方 (ver9 resolve9 対策B) と、プレビューのフレームキャッシュ (§7-1) の
# 単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・QImage 経路と従来の QLine 経路が 1px も違わないこと (見え方を変えない保証)
#   ・設定 "lines" / numpy 無し / 細いクリップでは従来経路へ落ちること
#   ・既定値 (draw_mode="image" / cache_frames=64) が届くこと
import copy
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from src.gui.timeline import timeline_view as tv
from src.gui.timeline.timeline_controller import TimelineController
from src.settings.settings_window import DEFAULT_SETTINGS
from src.timeline.builder import timeline_config
from src.timeline.model import (
    BASE_SUBTITLE_TRACK_ID,
    TRACK_SUBTITLE,
    SubtitleClip,
    Timeline,
    Track,
)

_W, _H = 320, 140


def _app():
    return QApplication.instance() or QApplication([])


def _view(settings=None):
    timeline = Timeline(fps=60, width=1920, height=1080)
    timeline.tracks = [
        Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1",
              clips=[SubtitleClip("s1", 1.0, 2.0, text="あ")]),
    ]
    controller = TimelineController(timeline, settings or dict(DEFAULT_SETTINGS))
    return tv.TimelineView(controller)


# 1 本ぶんの波形を描いた画像を返す
def _render(view, values, center=60.0, half=28.0, left=4,
            color=QColor(120, 200, 140, 200), gain=1.0, opacity=1.0):
    image = QImage(_W, _H, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.setOpacity(opacity)
    view._draw_wave_lines(painter, values, left, center, half, color, gain)
    painter.end()
    return image


def _values(count, seed=0):
    # 無音・小音・最大音を混ぜる (丸めの境界と中心線の扱いを突く)
    out = []
    for index in range(count):
        out.append([0.0, 1.0, 0.004, 0.37, 0.91, 0.12][(index + seed) % 6])
    return out


class DrawModeTest(unittest.TestCase):

    def setUp(self):
        self.app = _app()
        if tv._np is None:
            self.skipTest("numpy が無い環境では QImage 経路を使わない")

    def tearDown(self):
        for view in getattr(self, "_views", []):
            view.shutdown()

    def _pair(self, settings_image=None, **render):
        image_view = _view(settings_image)
        lines_settings = copy.deepcopy(dict(DEFAULT_SETTINGS))
        lines_settings.setdefault("timeline", {})
        lines_settings["timeline"] = copy.deepcopy(
            lines_settings.get("timeline", {}))
        lines_settings["timeline"].setdefault("waveform", {})
        lines_settings["timeline"]["waveform"]["draw_mode"] = "lines"
        lines_view = _view(lines_settings)
        self._views = [image_view, lines_view]
        self.assertEqual(image_view._waveform_draw_mode, "image")
        self.assertEqual(lines_view._waveform_draw_mode, "lines")
        return _render(image_view, **render), _render(lines_view, **render)

    # 既定 (image) と従来 (lines) で出る画素が完全に一致する
    def test_image_matches_lines(self):
        for count in (16, 17, 64, 301):
            with self.subTest(count=count):
                a, b = self._pair(values=_values(count))
                self.assertEqual(a, b)

    # 透明度・音量・位置を変えても一致する
    def test_matches_with_opacity_and_gain(self):
        cases = (
            {"gain": 2.0},
            {"gain": 0.5},
            {"opacity": 0.9},
            {"color": QColor(180, 220, 190, 255)},
            {"center": 31.0, "half": 11.5, "left": 0},
        )
        for extra in cases:
            with self.subTest(**extra):
                a, b = self._pair(values=_values(80), **extra)
                self.assertEqual(a, b)

    # 全部無音でも中心線が残り、一致する
    def test_silence_keeps_center_line(self):
        a, b = self._pair(values=[0.0] * 40)
        self.assertEqual(a, b)
        # 中心線の 1px が塗られている (クリップの存在が分かる)
        self.assertNotEqual(a.pixel(10, 60) & 0xFF000000, 0)

    # dB 目盛りでも一致する
    def test_log_scale_matches(self):
        settings = copy.deepcopy(dict(DEFAULT_SETTINGS))
        settings["timeline"] = copy.deepcopy(settings.get("timeline", {}))
        settings["timeline"].setdefault("waveform", {})
        settings["timeline"]["waveform"]["scale"] = "log"
        image_view = _view(settings)
        lines_settings = copy.deepcopy(settings)
        lines_settings["timeline"]["waveform"]["draw_mode"] = "lines"
        lines_view = _view(lines_settings)
        self._views = [image_view, lines_view]
        self.assertEqual(image_view._waveform_scale, "log")
        values = _values(120)
        self.assertEqual(_render(image_view, values), _render(lines_view, values))

    # numpy が無い環境では従来経路へ落ちる (落ちても絵は同じ)
    def test_falls_back_without_numpy(self):
        view = _view()
        self._views = [view]
        values = _values(64)
        expected = _render(view, values)
        with mock.patch.object(tv, "_np", None):
            self.assertEqual(_render(view, values), expected)

    # 列が少ないクリップは QImage を組まない (組み立て費用が勝つため)
    def test_thin_clip_uses_lines(self):
        view = _view()
        self._views = [view]
        with mock.patch.object(view, "_draw_wave_image") as spy:
            _render(view, _values(tv._WAVE_IMAGE_MIN_COLUMNS - 1))
            spy.assert_not_called()
            _render(view, _values(tv._WAVE_IMAGE_MIN_COLUMNS))
            spy.assert_called_once()

    # 呼び出し側の配列を書き換えない (波形データは素材ごとに使い回すため)
    def test_values_are_not_modified(self):
        view = _view()
        self._views = [view]
        values = tv._np.array(_values(64), dtype=tv._np.float64)
        before = values.copy()
        _render(view, values, gain=2.0)
        self.assertTrue(tv._np.array_equal(values, before))


class SettingsTest(unittest.TestCase):

    # 既定は QImage 経路。不正値・未知の値も既定へ倒す
    def test_draw_mode_default(self):
        self.assertEqual(timeline_config({})["waveform"]["draw_mode"], "image")
        for value, expected in (("lines", "lines"), ("LINES", "lines"),
                                (" image ", "image"), ("bogus", "image"),
                                (None, "image")):
            cfg = timeline_config({"timeline": {"waveform": {"draw_mode": value}}})
            self.assertEqual(cfg["waveform"]["draw_mode"], expected, repr(value))

    # 停止中プレビューのキャッシュ枚数 (ver9 resolve9 §7-1)
    def test_cache_frames_default(self):
        self.assertEqual(timeline_config({})["preview"]["cache_frames"], 64)
        self.assertEqual(DEFAULT_SETTINGS["timeline"]["preview"]["cache_frames"], 64)
        # 下げたい環境では設定で下げられる
        cfg = timeline_config({"timeline": {"preview": {"cache_frames": 8}}})
        self.assertEqual(cfg["preview"]["cache_frames"], 8)


if __name__ == "__main__":
    unittest.main()
