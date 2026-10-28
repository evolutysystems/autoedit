# 字幕プレビューの縁・影の描き方 (ver9 resolve11 §5) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・既定が "pen" = QPainterPath.united() を呼ばないこと (これが 1 秒の原因)
#   ・設定で "union" (従来の描き方) へ戻せること
#   ・縁と影の形を作り置きして、paint のたびに作り直さないこと
#   ・縁が 0px でも落ちないこと
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath
    from PySide6.QtWidgets import QApplication, QGraphicsScene

    from src.gui.timeline import preview_items as pi
    _QT_AVAILABLE = True
except Exception:  # noqa: BLE001 (PySide6 が無い環境ではスキップする)
    _QT_AVAILABLE = False

from src.modules import subtitle_generator
from src.settings.settings_window import DEFAULT_SETTINGS
from src.timeline.builder import timeline_config
from src.timeline.model import SubtitleClip

_CANVAS = (1920, 1080)
# ASS の改行 (\N)。ソースへ直接書くと unicode エスケープと衝突するため組み立てる
_BR = chr(92) + "N"


def _app():
    return QApplication.instance() or QApplication([])


def _eff_cfg():
    return subtitle_generator.build_effective_subtitle_cfg(
        DEFAULT_SETTINGS["subtitle"], DEFAULT_SETTINGS["vertical"],
        {"is_portrait": False, "width": _CANVAS[0], "height": _CANVAS[1]})


class OutlineModeConfigTest(unittest.TestCase):

    # 既定は "pen" (パス演算をしない描き方)
    def test_default_is_pen(self):
        cfg = timeline_config({})["preview"]
        self.assertEqual(cfg["subtitle_outline_mode"], "pen")

    # 設定で従来の描き方へ戻せる
    def test_union_can_be_selected(self):
        cfg = timeline_config(
            {"timeline": {"preview": {"subtitle_outline_mode": "union"}}})["preview"]
        self.assertEqual(cfg["subtitle_outline_mode"], "union")

    # 書き間違い・未知の値は既定へ寄せる
    def test_unknown_value_falls_back(self):
        cfg = timeline_config(
            {"timeline": {"preview": {"subtitle_outline_mode": "nope"}}})["preview"]
        self.assertEqual(cfg["subtitle_outline_mode"], "pen")

    # setting.json と DEFAULT_SETTINGS に既定が入っている
    def test_settings_default(self):
        self.assertEqual(
            DEFAULT_SETTINGS["timeline"]["preview"]["subtitle_outline_mode"], "pen")


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できないためスキップ")
class OutlinePaintTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _app()
        cls.eff = _eff_cfg()
        cls.profile = subtitle_generator.build_font_profile(cls.eff)

    def setUp(self):
        self._scenes = []

    def _item(self, mode, text="違う違う違う、別にタダで" + _BR + "あげるとは言ってない"):
        clip = SubtitleClip("s1", 0.0, 2.0, text=text, role="streamer")
        return pi.SubtitleOverlayItem(clip, self.eff, self.profile, _CANVAS,
                                      outline_mode=mode)

    # 描画してみる (例外を出さない / シーンへ載せて実際に塗る)
    def _render(self, item):
        scene = QGraphicsScene()
        self._scenes.append(scene)     # 参照を残さないとアイテムごと破棄される
        scene.setSceneRect(0, 0, *_CANVAS)
        scene.addItem(item)
        image = QImage(_CANVAS[0], _CANVAS[1], QImage.Format_ARGB32)
        image.fill(QColor(32, 48, 72))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        scene.render(painter, QRectF(0, 0, *_CANVAS), QRectF(0, 0, *_CANVAS))
        painter.end()
        return image

    # 既定 ("pen") では united() を一度も呼ばない (ここが 1 秒の原因だった)
    def test_pen_mode_never_calls_united(self):
        calls = []
        original = QPainterPath.united

        def counted(self, other):
            calls.append(1)
            return original(self, other)

        QPainterPath.united = counted
        try:
            item = self._item(pi.OUTLINE_PEN)
            self._render(item)
            self._render(item)          # 2 回描いても呼ばれない
        finally:
            QPainterPath.united = original
        self.assertEqual(calls, [], "pen モードで united() が呼ばれています")

    # "union" では従来どおり united() を使う (比較用の退避口が生きている)
    def test_union_mode_uses_united(self):
        calls = []
        original = QPainterPath.united

        def counted(self, other):
            calls.append(1)
            return original(self, other)

        QPainterPath.united = counted
        try:
            item = self._item(pi.OUTLINE_UNION)
            self._render(item)
            first = len(calls)
            self._render(item)          # 2 回目は作り置きを使う
        finally:
            QPainterPath.united = original
        self.assertEqual(first, 1, "union モードで united() が呼ばれていません")
        self.assertEqual(len(calls), 1, "paint のたびに united() を呼んでいます")

    # 縁と影の形は 1 回だけ作って使い回す
    def test_outline_paths_are_cached(self):
        item = self._item(pi.OUTLINE_UNION)
        first = item._outline_paths()           # noqa: SLF001 (単体テスト)
        self.assertIs(first, item._outline_paths())   # noqa: SLF001

    # 縁 0px でも落ちない (影は字の形そのまま)
    def test_no_outline(self):
        for mode in (pi.OUTLINE_PEN, pi.OUTLINE_UNION):
            item = self._item(mode)
            item._outline_width = 0                  # noqa: SLF001
            item._outline_cache = None               # noqa: SLF001
            self._render(item)                       # 例外が出なければよい
            shape, silhouette = item._outline_paths()  # noqa: SLF001
            self.assertIsNone(shape)
            self.assertIsNotNone(silhouette)

    # 同じ描き方なら毎回まったく同じ絵になる (比較テストの土台)
    def test_render_is_deterministic(self):
        a = self._render(self._item(pi.OUTLINE_PEN))
        b = self._render(self._item(pi.OUTLINE_PEN))
        self.assertEqual(a.constBits(), b.constBits())


if __name__ == "__main__":
    unittest.main()
