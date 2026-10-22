# 字幕プレビューのメトリクス (ver8 resolve8 §5-§7) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・フォントの縦メトリクス (unitsPerEm / usWinAscent / usWinDescent) が読めること
#   ・ASS Fontsize から「libass と同じ字の大きさ」へ換算できること
#   ・行送りが固定比 (x1.2) ではなく実メトリクスで積まれること
#   ・字間 (ASS Spacing) がプレビューへ反映されること
#   ・"em" で従来動作へ戻せること / 位置の基準点はモードで変わらないこと
import copy
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFont, QFontDatabase        # noqa: E402
from PySide6.QtWidgets import QApplication            # noqa: E402

from src.gui.timeline import font_metrics             # noqa: E402
from src.gui.timeline.preview_items import SubtitleOverlayItem   # noqa: E402
from src.modules import subtitle_generator            # noqa: E402
from src.settings.settings_window import (            # noqa: E402
    DEFAULT_SETTINGS,
    SETTINGS_DIR,
)
from src.timeline.model import SubtitleClip, Transform   # noqa: E402

_CANVAS = (1920, 1080)
# 同梱フォント (設計書 §2-1 の実測対象)。usWinAscent+usWinDescent = 1080 で
# unitsPerEm = 1000 のため、libass と em 基準で 1.08 倍の差が出る。
_BUNDLED_FONT = os.path.join(SETTINGS_DIR, "fonts", "07にくまるフォント.otf")
_FONT_SIZE = 130


def setUpModule():
    if QApplication.instance() is None:
        setUpModule.app = QApplication([])


# 同梱フォントを Qt へ登録し、その書体名を返す (登録できなければ None)
def _register_bundled_font():
    if not os.path.isfile(_BUNDLED_FONT):
        return None
    font_id = QFontDatabase.addApplicationFont(_BUNDLED_FONT)
    if font_id < 0:
        return None
    families = QFontDatabase.applicationFontFamilies(font_id)
    return families[0] if families else None


def _subtitle_cfg(family, spacing=0):
    cfg = copy.deepcopy(DEFAULT_SETTINGS["subtitle"])
    cfg["font_family"] = family
    cfg["font_size"] = _FONT_SIZE
    cfg["spacing"] = spacing
    return cfg


def _item(family, text="あいう", mode=font_metrics.MODE_LIBASS, spacing=0,
          role="streamer", transform=None):
    cfg = _subtitle_cfg(family, spacing=spacing)
    profile = subtitle_generator.build_font_profile(cfg)
    clip = SubtitleClip("s1", 0.0, 2.0, text=text, role=role,
                        transform=transform or Transform.from_position_dict(None))
    return SubtitleOverlayItem(clip, cfg, profile, _CANVAS, metrics_mode=mode)


class FontMetricsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.family = _register_bundled_font()

    def setUp(self):
        if not self.family:
            self.skipTest("同梱フォントが無いためスキップします")
        self.font = QFont(self.family)
        self.font.setPixelSize(_FONT_SIZE)

    # T1: 縦メトリクスを読めること (同梱フォントの実測値)
    def test_vertical_metrics_of_bundled_font(self):
        metrics = font_metrics.vertical_metrics(self.font)
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics["upem"], 1000)
        self.assertEqual(metrics["win_height"], 1080)      # 860 + 220
        self.assertEqual(metrics["hhea_height"], 1000)     # 860 - (-140) + 0

    # T2: Fontsize 130 → libass と同じ字の大きさは 120 px (130 x 1000/1080)
    def test_libass_pixel_size(self):
        self.assertEqual(
            font_metrics.libass_pixel_size(self.font, _FONT_SIZE), 120)

    # "em" では換算しない (従来動作)
    def test_em_mode_keeps_the_size(self):
        self.assertEqual(
            font_metrics.libass_pixel_size(self.font, _FONT_SIZE,
                                           font_metrics.MODE_EM),
            _FONT_SIZE)

    # T3: メトリクスが読めないときは指定サイズのまま (落ちない)
    def test_falls_back_when_metrics_are_unreadable(self):
        original = font_metrics._read_metrics
        font_metrics._CACHE.clear()
        font_metrics._read_metrics = lambda _font: None
        try:
            self.assertEqual(
                font_metrics.libass_pixel_size(self.font, _FONT_SIZE), _FONT_SIZE)
            self.assertAlmostEqual(
                font_metrics.line_advance_px(self.font, _FONT_SIZE, ratio=1.2),
                _FONT_SIZE * 1.2, places=6)
        finally:
            font_metrics._read_metrics = original
            font_metrics._CACHE.clear()

    # 行送りは usWinAscent+usWinDescent 基準 = Fontsize (em 基準の x1.2 ではない)
    def test_line_advance(self):
        self.assertAlmostEqual(
            font_metrics.line_advance_px(self.font, _FONT_SIZE),
            float(_FONT_SIZE), places=6)
        self.assertAlmostEqual(
            font_metrics.line_advance_px(self.font, _FONT_SIZE,
                                         font_metrics.MODE_EM, 1.2),
            _FONT_SIZE * 1.2, places=6)

    # 不正な値でも例外にしない
    def test_invalid_sizes(self):
        self.assertEqual(font_metrics.libass_pixel_size(self.font, 0), 0.0)
        self.assertEqual(font_metrics.libass_pixel_size(self.font, None), 0.0)
        self.assertEqual(font_metrics.line_advance_px(self.font, "x"), 0.0)


class SubtitleItemMetricsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.family = _register_bundled_font()

    def setUp(self):
        if not self.family:
            self.skipTest("同梱フォントが無いためスキップします")

    # T4: "em" では従来どおり Fontsize をそのまま pixelSize にする
    def test_em_mode_pixel_size(self):
        item = _item(self.family, mode=font_metrics.MODE_EM)
        self.assertEqual(item.font().pixelSize(), _FONT_SIZE)

    # T5: "libass" では字が小さくなり、文字の矩形も同じ比で縮む
    def test_libass_mode_shrinks_the_glyphs(self):
        libass_item = _item(self.family)
        em_item = _item(self.family, mode=font_metrics.MODE_EM)
        self.assertLess(libass_item.font().pixelSize(), _FONT_SIZE)
        ratio = (em_item.text_rect().width() / libass_item.text_rect().width())
        # usWinAscent+usWinDescent / upem = 1.08 (誤差は丸めとヒンティングぶん)
        self.assertGreater(ratio, 1.06)
        self.assertLess(ratio, 1.10)

    # T6: 行送り。"libass" は Fontsize ぶん (libass の ink と一致した基準) /
    # "em" は Fontsize x 1.2 (従来)
    def test_line_height(self):
        libass_item = _item(self.family, text="あいう\nかきく\nさしす")
        self.assertAlmostEqual(libass_item.text_rect().height(),
                               _FONT_SIZE * 3, places=3)
        em_item = _item(self.family, text="あいう\nかきく\nさしす",
                        mode=font_metrics.MODE_EM)
        self.assertAlmostEqual(em_item.text_rect().height(),
                               _FONT_SIZE * 1.2 * 3, places=3)

    # T7: 字間 (ASS Spacing) が幅へ反映される
    def test_spacing_widens_the_text(self):
        text = "あいう"
        base = _item(self.family, text=text)
        spaced = _item(self.family, text=text, spacing=10)
        delta = spaced.text_rect().width() - base.text_rect().width()
        # 文字数 x 10 を目安に、末尾へ 1 つ付くかどうかのぶれを許容する
        self.assertGreater(delta, len(text) * 10 - 10 - 1e-6)
        self.assertLess(delta, len(text) * 10 + 10 + 1e-6)

    # 字間は "em" (従来動作) では反映しない
    def test_spacing_is_not_applied_in_em_mode(self):
        text = "あいう"
        base = _item(self.family, text=text, mode=font_metrics.MODE_EM)
        spaced = _item(self.family, text=text, spacing=10,
                       mode=font_metrics.MODE_EM)
        self.assertAlmostEqual(spaced.text_rect().width(),
                               base.text_rect().width(), places=3)

    # T8: 位置の基準点は \pos の変換式どおりで、モードによって変わらない
    # (保存される正規化座標の意味が変わらない = 既存プロジェクトがずれない)
    def test_anchor_point_is_mode_independent(self):
        transform = Transform.from_position_dict({"x": 0.25, "y": 0.25})
        for mode in (font_metrics.MODE_LIBASS, font_metrics.MODE_EM):
            item = _item(self.family, mode=mode, transform=transform)
            point = item.anchor_scene_point()
            self.assertAlmostEqual(point.x(), 1200.0, places=3)   # (0.25+1)*1920/2
            self.assertAlmostEqual(point.y(), 405.0, places=3)    # (1-0.25)*1080/2

    # コメントは文字の左中央が基準 (ver3 resolve11 §3 D-4)。これもモード非依存。
    def test_comment_anchor_point_is_mode_independent(self):
        transform = Transform.from_position_dict({"x": -0.5, "y": 0.0})
        for mode in (font_metrics.MODE_LIBASS, font_metrics.MODE_EM):
            item = _item(self.family, mode=mode, role="comment",
                         transform=transform)
            point = item.anchor_scene_point()
            self.assertAlmostEqual(point.x(), 480.0, places=3)    # (-0.5+1)*1920/2
            self.assertAlmostEqual(point.y(), 540.0, places=3)


if __name__ == "__main__":
    unittest.main()
