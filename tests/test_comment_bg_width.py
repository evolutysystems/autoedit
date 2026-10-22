# コメント背景の幅が文字をはみ出させないことの単体テスト
# (docs/error/20261023/resolve.md)
# 実行: python -m unittest discover -s tests
#
# 背景 (利用者の報告 / 2026-10-23):
#   にくまるフォントでコメント役割のテロップを出すと、背景の黒い箱から文字が
#   はみ出していた。幅をフォントごとに違う固定係数で見積もっていたためである。
#   設定値 full=0.78 は游ゴシック向けの値で、にくまるフォントには 0.926 が必要。
#   さらに half=0.45 は「半角 = 全角の半分」という前提で、にくまるフォントの
#   英数字 (W 0.926em / i 0.366em) には合わなかった。
#
# 重点:
#   ・実 libass 出力で測った文字インクの幅を**下回らない**こと (はみ出さない)
#   ・細い英数字では従来より**狭く**なること (無駄に広い箱も直る)
#   ・縁取りのぶん箱が左右へ広がること
#   ・読めないフォント / 設定 OFF では従来の固定係数へ落ちること
import unittest

from src.modules import comment_decor, font_advance
from src.settings.settings_window import DEFAULT_SETTINGS

# 利用者の環境に合わせた設定 (同梱フォント / font_size 130 / 縁取り 10)
_FONT = "07NikumaruFont"
_SIZE = 130
_LEFT = 240

# 実 libass 出力で測った「文字の左端から文字インク右端まで」(px)。
# 1920x1080 の黒画面へ焼き込み、1px 単位で測った値 (resolve.md §3)。
# 縁取り (outline_width=10) の張り出しを含む。
_INK_WIDTH = {
    "あいうえお": 600,
    "これはコメントです": 1077,
    "wwwww": 502,
    "やばいwww": 659,
    "WWWWW": 549,
    "8888888888": 791,
    "iii": 122,
}

# 修正前の固定係数 (full=0.78 / half=0.45) が出していた幅
_LEGACY_WIDTH = {
    "あいうえお": 508,
    "これはコメントです": 913,
    "wwwww": 293,
    "やばいwww": 480,
    "WWWWW": 293,
    "8888888888": 586,
    "iii": 176,
}


def _cfg(**overrides):
    cfg = dict(DEFAULT_SETTINGS["subtitle"])
    cfg.update({
        "font_family": _FONT,
        "font_size": _SIZE,
        "outline_width": 10,
        "bold": True,
        "comment_margin_l": _LEFT,
        "comment_bg_padding_px": 24,
        "comment_icon_size_px": 150,
        "comment_icon_gap_px": 50,
    })
    cfg.update(overrides)
    return cfg


def _item(text):
    return {"start": 0.0, "end": 5.0, "text": text, "role": "comment", "use": True}


class MeasuredWidthTest(unittest.TestCase):

    def setUp(self):
        comment_decor.reset_warnings()
        font_advance.reset_cache()
        if font_advance.metrics_for(_FONT, _cfg()) is None:
            self.skipTest("同梱フォントを読めない環境")

    # 同梱フォントなら実測で求める
    def test_uses_measured_advances(self):
        extent = comment_decor.text_extent(_item("あいうえお"), _cfg())
        self.assertTrue(extent["measured"])

    # 本題: 実 libass 出力の文字インクを下回らない (= はみ出さない)
    def test_never_narrower_than_the_real_ink(self):
        cfg = _cfg()
        for text, ink in _INK_WIDTH.items():
            with self.subTest(text=text):
                width = comment_decor.text_extent(_item(text), cfg)["width"]
                self.assertGreaterEqual(
                    width, ink,
                    f"{text}: 推定 {width}px が実測 {ink}px より狭い")

    # 広げすぎてもいない (実測 + 1 文字ぶん以内)
    def test_not_wastefully_wide(self):
        cfg = _cfg()
        for text, ink in _INK_WIDTH.items():
            with self.subTest(text=text):
                width = comment_decor.text_extent(_item(text), cfg)["width"]
                self.assertLessEqual(width, ink + _SIZE)

    # 英数字が多い文字列は従来より大きく広がる (不足していたぶん)
    def test_latin_gets_much_wider_than_before(self):
        cfg = _cfg()
        for text in ("wwwww", "WWWWW", "8888888888"):
            with self.subTest(text=text):
                width = comment_decor.text_extent(_item(text), cfg)["width"]
                self.assertGreater(width, _LEGACY_WIDTH[text] * 1.3)

    # 細い英数字では従来より狭くなる (無駄に広い箱も直る)
    def test_narrow_latin_gets_tighter(self):
        width = comment_decor.text_extent(_item("iii"), _cfg())["width"]
        self.assertLess(width, _LEGACY_WIDTH["iii"])

    # 2 行あるときは最長行で決まる
    def test_widest_line_wins(self):
        cfg = _cfg()
        one = comment_decor.text_extent(_item("WWWWW"), cfg)["width"]
        two = comment_decor.text_extent(_item("あ\\NWWWWW"), cfg)
        self.assertEqual(two["width"], one)
        self.assertEqual(two["lines"], 2)

    # 字幕 1 件ごとのフォント指定 (item["font"]) にも従う
    def test_per_item_font_is_used(self):
        cfg = _cfg(font_family="No Such Font ZZZ")
        fallback = comment_decor.text_extent(_item("WWWWW"), cfg)
        self.assertFalse(fallback["measured"])
        item = dict(_item("WWWWW"), font=_FONT)
        self.assertTrue(comment_decor.text_extent(item, cfg)["measured"])

    # 字幕 1 件ごとのサイズ指定にも比例する
    def test_per_item_font_size_scales(self):
        cfg = _cfg()
        base = comment_decor.text_extent(_item("WWWWW"), cfg)["width"]
        half = comment_decor.text_extent(
            dict(_item("WWWWW"), font_size=_SIZE // 2), cfg)["width"]
        self.assertAlmostEqual(half, base / 2.0, delta=2)


class BackgroundBoxTest(unittest.TestCase):

    def setUp(self):
        comment_decor.reset_warnings()
        font_advance.reset_cache()
        if font_advance.metrics_for(_FONT, _cfg()) is None:
            self.skipTest("同梱フォントを読めない環境")

    # 箱の右端が文字インクの右端より右にあること (はみ出さない)
    def test_box_contains_the_ink(self):
        cfg = _cfg()
        for text, ink in _INK_WIDTH.items():
            with self.subTest(text=text):
                box = comment_decor.background_box(_item(text), cfg, 1920, 1080)
                self.assertGreaterEqual(box["x"] + box["w"], _LEFT + ink)

    # 縁取りのぶん左右へ広がる (resolve.md §4 原因C)
    def test_outline_bleeds_into_the_box(self):
        without = comment_decor.background_box(
            _item("あいうえお"),
            _cfg(comment_icon_enabled=False, comment_bg_include_outline=False),
            1920, 1080)
        with_bleed = comment_decor.background_box(
            _item("あいうえお"),
            _cfg(comment_icon_enabled=False, comment_bg_include_outline=True),
            1920, 1080)
        self.assertEqual(with_bleed["x"], without["x"] - 10)
        self.assertEqual(with_bleed["w"], without["w"] + 20)

    # 縁取りを 0 にすれば張り出しも 0
    def test_no_bleed_without_outline(self):
        box = comment_decor.background_box(
            _item("あいうえお"), _cfg(comment_icon_enabled=False, outline_width=0),
            1920, 1080)
        self.assertEqual(box["x"], _LEFT - 24)


class FallbackTest(unittest.TestCase):

    def setUp(self):
        comment_decor.reset_warnings()
        font_advance.reset_cache()

    # 設定で従来の固定係数へ戻せる (互換の逃げ道)
    def test_measure_can_be_turned_off(self):
        cfg = _cfg(comment_bg_measure_font=False)
        for text, legacy in _LEGACY_WIDTH.items():
            with self.subTest(text=text):
                extent = comment_decor.text_extent(_item(text), cfg)
                self.assertFalse(extent["measured"])
                self.assertEqual(extent["width"], legacy)

    # 読めないフォントでも例外にせず固定係数へ落ちる
    def test_unknown_font_falls_back(self):
        extent = comment_decor.text_extent(
            _item("WWWWW"), _cfg(font_family="No Such Font ZZZ"))
        self.assertFalse(extent["measured"])
        self.assertEqual(extent["width"], _LEGACY_WIDTH["WWWWW"])

    # フォント名が空でも落ちない
    def test_empty_font_family(self):
        extent = comment_decor.text_extent(_item("WWWWW"), _cfg(font_family=""))
        self.assertFalse(extent["measured"])


if __name__ == "__main__":
    unittest.main()
