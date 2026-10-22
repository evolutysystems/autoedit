# フォントファイルから送り幅を読む (src/modules/font_advance.py) の単体テスト
# (docs/error/20261023/resolve.md §5 案A)
# 実行: python -m unittest discover -s tests
#
# 重点:
#   ・同梱の 07にくまるフォントから upem / usWinAscent+usWinDescent / 送り幅を読むこと
#   ・英数字がプロポーショナル (W 0.926em / i 0.366em) に読めること
#     = 「半角は全角の半分」という従来の前提が成り立たないことの裏付け
#   ・英語名でも日本語名でも引けること
#   ・読めないもの (壊れたファイル / 無い書体 / cmap に無い文字) で落ちないこと
#   ・太字の指定に合う書体を選ぶこと
import os
import tempfile
import unittest

from src.modules import font_advance

_FONT_NAME = "07NikumaruFont"
_FONT_NAME_JA = "07にくまるフォント"
_CFG = {"custom_fonts_dir": "fonts"}

# 同梱フォントの実測値 (QRawFont と一致することを確認済み / resolve.md §5)
_EXPECTED_EM = {
    "あ": 1.000, "漢": 1.000, "ー": 1.000, "！": 1.000,
    "W": 0.926, "w": 0.846, "A": 0.706, "1": 0.660, "8": 0.660,
    " ": 0.500, "!": 0.446, "i": 0.366,
}


class SearchDirsTest(unittest.TestCase):

    # custom_fonts_dir が最初 (libass へ fontsdir で渡しているのと同じ場所)
    def test_custom_fonts_dir_comes_first(self):
        dirs = font_advance.search_dirs(_CFG)
        self.assertTrue(dirs[0].endswith(os.path.join("settings", "fonts")))

    # font_file_dirs を足せる
    def test_extra_dirs_are_included(self):
        extra = os.path.abspath(os.path.join("x", "my_fonts"))
        dirs = font_advance.search_dirs(dict(_CFG, font_file_dirs=[extra]))
        self.assertIn(extra, dirs)

    # 同じフォルダを二度見ない
    def test_duplicates_are_dropped(self):
        same = font_advance.search_dirs(_CFG)[0]
        dirs = font_advance.search_dirs(dict(_CFG, font_file_dirs=[same, same]))
        keys = [os.path.normcase(os.path.abspath(p)) for p in dirs]
        self.assertEqual(len(keys), len(set(keys)))

    # 絶対パスの custom_fonts_dir はそのまま使う
    def test_absolute_custom_dir(self):
        absolute = os.path.abspath(os.path.join("y", "fonts"))
        dirs = font_advance.search_dirs({"custom_fonts_dir": absolute})
        self.assertEqual(dirs[0], absolute)


class BundledFontTest(unittest.TestCase):

    def setUp(self):
        font_advance.reset_cache()
        self.metrics = font_advance.metrics_for(_FONT_NAME, _CFG)
        if self.metrics is None:
            self.skipTest("同梱フォントを読めない環境")

    # 縦メトリクス。libass の縮尺はここから決まる (resolve.md §3-1)
    def test_vertical_metrics(self):
        self.assertEqual(self.metrics.upem, 1000)
        self.assertEqual(self.metrics.win_height, 1080)
        self.assertAlmostEqual(self.metrics.scale, 1000 / 1080, places=5)

    # 本題: 文字ごとの送り幅。英数字はプロポーショナル
    def test_advance_per_character(self):
        for char, expected in _EXPECTED_EM.items():
            with self.subTest(char=char):
                self.assertAlmostEqual(self.metrics.advance_em(char), expected,
                                       places=3)

    # 「半角 = 全角の半分」は成り立たない (従来の固定係数 0.45 の前提)
    def test_latin_is_not_half_width(self):
        self.assertGreater(self.metrics.advance_em("W"), 0.9)
        self.assertLess(self.metrics.advance_em("i"), 0.4)

    # 文字列の合計と px 換算
    def test_text_width(self):
        self.assertAlmostEqual(self.metrics.text_em("あいうえお"), 5.0, places=3)
        self.assertAlmostEqual(self.metrics.text_width("あいうえお", 130),
                               5.0 * (1000 / 1080) * 130, places=2)
        self.assertEqual(self.metrics.text_width("", 130), 0.0)

    # cmap に無い文字は全角として数える (安全側 = 狭く見積もらない)
    def test_unknown_character_counts_as_full_width(self):
        self.assertAlmostEqual(self.metrics.advance_em("\U0001F600"), 1.0, places=3)

    # 日本語の書体名でも引ける
    def test_japanese_family_name(self):
        other = font_advance.metrics_for(_FONT_NAME_JA, _CFG)
        self.assertIsNotNone(other)
        self.assertEqual(other.path, self.metrics.path)

    # 大文字小文字・空白の違いを無視する
    def test_name_matching_ignores_case_and_spaces(self):
        self.assertIsNotNone(font_advance.metrics_for(" 07nikumarufont ", _CFG))

    # 太字の書体を持たないフォントは、太字指定でも同じ書体を返す
    # (libass も標準の書体を太らせて使い、送り幅は変わらない)
    def test_bold_falls_back_to_the_only_face(self):
        bold = font_advance.metrics_for(_FONT_NAME, _CFG, bold=True)
        self.assertIsNotNone(bold)
        self.assertEqual(bold.path, self.metrics.path)
        self.assertAlmostEqual(bold.advance_em("W"),
                               self.metrics.advance_em("W"), places=3)


class UnreadableTest(unittest.TestCase):

    def setUp(self):
        font_advance.reset_cache()

    def test_unknown_family(self):
        self.assertIsNone(font_advance.metrics_for("No Such Font ZZZ", _CFG))

    def test_empty_family(self):
        self.assertIsNone(font_advance.metrics_for("", _CFG))
        self.assertIsNone(font_advance.metrics_for(None, _CFG))

    def test_missing_file(self):
        self.assertIsNone(font_advance.load(
            os.path.join(tempfile.gettempdir(), "no_such_font_zzz.ttf")))

    # 中身がフォントでないファイルでも例外にしない
    def test_broken_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "broken.ttf")
            with open(path, "wb") as f:
                f.write(b"not a font at all" * 10)
            self.assertIsNone(font_advance.load(path))

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "empty.otf")
            open(path, "wb").close()
            self.assertIsNone(font_advance.load(path))

    # フォルダが無くても落ちない
    def test_missing_dir(self):
        folder = os.path.join(tempfile.gettempdir(), "no_such_fonts_dir_zzz")
        self.assertIsNone(font_advance.metrics_for(
            _FONT_NAME, {"custom_fonts_dir": folder, "font_file_dirs": [folder]}))

    # 覚えた内容を捨てられる (他のテストへ漏らさないため)
    def test_reset_cache(self):
        font_advance.metrics_for(_FONT_NAME, _CFG)
        font_advance.reset_cache()
        self.assertEqual(font_advance._CACHE, {})
        self.assertEqual(font_advance._INDEX, {})


class SystemFontTest(unittest.TestCase):
    # OS のフォントは環境によって無いため、無ければ skip する

    def setUp(self):
        font_advance.reset_cache()

    def _metrics(self, family, bold=False):
        metrics = font_advance.metrics_for(family, _CFG, bold=bold)
        if metrics is None:
            self.skipTest(f"{family} が無い環境")
        return metrics

    # TTC (1 ファイルに複数の書体) から目的の書体を選べること
    def test_reads_ttc(self):
        metrics = self._metrics("Meiryo")
        self.assertEqual(metrics.upem, 2048)
        self.assertEqual(metrics.win_height, 3072)
        self.assertTrue(metrics.path.lower().endswith(".ttc"))

    # 等幅フォントでは半角がちょうど半分になること (固定係数 0.5 が正しい唯一の場合)
    def test_monospaced_font_is_half_width(self):
        metrics = self._metrics("MS Gothic")
        self.assertAlmostEqual(metrics.advance_em("あ"), 1.0, places=3)
        self.assertAlmostEqual(metrics.advance_em("W"), 0.5, places=3)
        self.assertAlmostEqual(metrics.scale, 1.0, places=5)

    # 太字の書体を持つフォントでは、太字指定で太字側を選ぶこと
    def test_bold_picks_the_bold_face(self):
        regular = self._metrics("Meiryo", bold=False)
        bold = self._metrics("Meiryo", bold=True)
        self.assertNotEqual(os.path.basename(regular.path),
                            os.path.basename(bold.path))
        self.assertGreater(bold.advance_em("W"), regular.advance_em("W"))

    # ウェイト別の書体名 (Light など) ではなく標準ウェイトを選ぶこと。
    # ファイル名順で拾うと Yu Gothic UI に Light (W 0.895em) が当たり、
    # 標準 (0.934em) より狭く見積もってしまう。
    def test_regular_weight_is_preferred(self):
        regular = self._metrics("Yu Gothic UI", bold=False)
        light = font_advance.metrics_for("Yu Gothic UI Light", _CFG)
        if light is None:
            self.skipTest("Yu Gothic UI Light が無い環境")
        self.assertNotEqual(os.path.basename(regular.path).lower(),
                            "yugothl.ttc")
        self.assertGreater(regular.advance_em("W"), light.advance_em("W"))


if __name__ == "__main__":
    unittest.main()
