# ガラスモーフィズムのテーマ (src/gui/theme.py) の単体テスト
# 実行: python -m unittest discover -s tests
# docs/request/ver3/resolve3.md §11 段 1 の完了条件に対応する。
#   * tokens() / mode() / build_qss() が期待どおり動くこと
#   * ライト・ダークの両モードでコントラスト比が基準を満たすこと (§3-4)
#   * OS がライト・ダークどちらでも正しく判定できること (§5.10-1)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtGui import QColor

    from src.gui import theme
    _QT_AVAILABLE = True
except Exception:  # noqa: BLE001 (PySide6 が無い環境ではスキップする)
    _QT_AVAILABLE = False

# 判定基準 (§5.2-1)
#   本文相当の文字 (WCAG AA)        : 4.5:1
#   UI 部品・見出しの識別 (WCAG AA) : 3.0:1
_TEXT_MIN_RATIO = 4.5
_UI_MIN_RATIO = 3.0


# 明暗モードを固定した設定辞書を作る (OS 設定に左右されずに検証するため)
def _settings(mode="dark", **overrides):
    ui = {"theme": "glass", "theme_mode": mode}
    ui.update(overrides)
    return {"ui": ui}


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できないためスキップ")
class ColorUtilTest(unittest.TestCase):

    # "#RRGGBB" と "rgba(r,g,b,a)" の双方を QColor へ変換できる
    def test_parse_color(self):
        solid = theme.parse_color("#DC0810")
        self.assertEqual((solid.red(), solid.green(), solid.blue()), (0xDC, 0x08, 0x10))
        self.assertAlmostEqual(solid.alphaF(), 1.0, places=3)

        translucent = theme.parse_color("rgba(255,255,255,0.07)")
        self.assertEqual(translucent.red(), 255)
        self.assertAlmostEqual(translucent.alphaF(), 0.07, places=2)

    # 解釈できない色は None を返し、呼び出し側が既定へ戻せる (§7)
    def test_parse_color_invalid(self):
        self.assertIsNone(theme.parse_color("#GGG"))
        self.assertIsNone(theme.parse_color(""))
        self.assertIsNone(theme.parse_color(None))

    # 2 層を 1 色へ畳んだ結果が「2 枚重ねて塗った結果」と一致する (§5.8)。
    # Timeline は塗りの回数を減らすためこの畳み込みに依存しているため、
    # 見た目が変わらないことをここで担保する。
    def test_composite_color_matches_two_passes(self):
        theme.invalidate_cache()
        settings = _settings("dark")
        backdrop = QColor("#202028")   # 任意の下地
        for top, bottom in (("timeline.track", "timeline.bg"),
                            ("timeline.track.alt", "timeline.bg"),
                            ("timeline.ruler", "timeline.bg")):
            # 2 枚重ね: 下地 → bottom → top の順に合成する
            two_pass = theme._flatten(
                theme.color(top, settings),
                theme._flatten(theme.color(bottom, settings), backdrop))
            # 畳み込み: 1 色にしてから下地へ重ねる
            one_pass = theme._flatten(
                theme.composite_color(top, bottom, settings), backdrop)
            for channel in ("red", "green", "blue"):
                self.assertAlmostEqual(
                    getattr(two_pass, channel)(), getattr(one_pass, channel)(),
                    delta=1, msg=f"{top}/{channel}")

    # コントラスト比が WCAG の定義どおり (白と黒で 21:1)
    def test_contrast_ratio(self):
        self.assertAlmostEqual(
            theme.contrast_ratio(QColor("#FFFFFF"), QColor("#000000")), 21.0, places=2)
        self.assertAlmostEqual(
            theme.contrast_ratio(QColor("#FFFFFF"), QColor("#FFFFFF")), 1.0, places=2)


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できないためスキップ")
class ModeTest(unittest.TestCase):

    def setUp(self):
        theme.invalidate_cache()

    # 設定で固定した明暗がそのまま返る (OS 設定より優先される / §5.10-1)
    def test_mode_is_configurable(self):
        self.assertEqual(theme.mode(_settings("dark")), "dark")
        self.assertEqual(theme.mode(_settings("light")), "light")

    # "auto" では OS 設定を見る。判定できない環境ではダークへ倒す (§5.10-1 の 3 段目)
    def test_mode_auto_returns_known_value(self):
        self.assertIn(theme.mode(_settings("auto")), ("dark", "light"))

    # ui.theme = "system" でテーマを完全に無効化できる (§7 の切り戻し)
    def test_system_theme_disables(self):
        self.assertFalse(theme.is_enabled({"ui": {"theme": "system"}}))
        self.assertTrue(theme.is_enabled(_settings("dark")))


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できないためスキップ")
class TokenTest(unittest.TestCase):

    def setUp(self):
        theme.invalidate_cache()

    # 明暗で別セットのトークンが返る (R8 / §5.2)
    def test_tokens_differ_by_mode(self):
        dark = theme.tokens(_settings("dark"))
        theme.invalidate_cache()
        light = theme.tokens(_settings("light"))
        self.assertNotEqual(dark["text.primary"], light["text.primary"])
        self.assertNotEqual(dark["bg.from"], light["bg.from"])

    # Timeline のトークンは明暗に追従しない (§3-6-3)
    def test_timeline_tokens_are_always_dark(self):
        dark = theme.tokens(_settings("dark"))
        theme.invalidate_cache()
        light = theme.tokens(_settings("light"))
        for key in ("timeline.track", "timeline.track.alt", "timeline.ruler",
                    "timeline.grid", "timeline.text"):
            self.assertEqual(dark[key], light[key], key)
        # timeline.bg は色が同じで不透明度だけ違う (明るい下地でも黒く見せるため)
        dark_bg = theme.parse_color(dark["timeline.bg"])
        light_bg = theme.parse_color(light["timeline.bg"])
        self.assertEqual(dark_bg.rgb() & 0xFFFFFF, light_bg.rgb() & 0xFFFFFF)
        self.assertGreater(light_bg.alphaF(), dark_bg.alphaF())

    # 既定の差し色が要望どおりのオレンジであること
    def test_default_accent_color(self):
        self.assertEqual(theme.DEFAULT_UI_SETTINGS["accent_color"], "#E8A15C")
        for mode in ("dark", "light"):
            theme.invalidate_cache()
            self.assertEqual(theme.tokens({"ui": {"theme_mode": mode}})["accent"],
                             "#E8A15C")

    # 設定でアクセント色・角丸を上書きできる (ハードコード禁止 / §4-3)
    def test_settings_override_tokens(self):
        tokens = theme.tokens(_settings("dark", accent_color="#3366FF",
                                        corner_radius_px=4, control_radius_px=2))
        self.assertEqual(tokens["accent"], "#3366FF")
        self.assertEqual(tokens["radius.panel"], "4px")
        self.assertEqual(tokens["radius.control"], "2px")

    # 不正な色は既定へ戻り、他のトークンは生きたまま (§7)
    def test_invalid_accent_falls_back(self):
        tokens = theme.tokens(_settings("dark", accent_color="#GGG"))
        self.assertEqual(tokens["accent"], theme.DEFAULT_UI_SETTINGS["accent_color"])
        self.assertEqual(tokens["text.primary"], theme.DARK["text.primary"])

    # 文字背後の不透明度は下限へ切り上げる (可読性を装飾より優先 / §3-4)
    def test_text_backdrop_opacity_floor(self):
        tokens = theme.tokens(_settings(
            "dark", glass={"dark": {"bg": 0.07, "bg_strong": 0.03,
                                    "border": 0.16, "highlight": 0.28}}))
        strong = theme.parse_color(tokens["glass.bg.strong"])
        self.assertGreaterEqual(
            strong.alphaF(),
            theme.DEFAULT_UI_SETTINGS["min_text_backdrop_opacity"] - 1e-6)

    # Timeline 背景の不透明度は明暗ごとに設定できる (§5.2-4 / §3-6-3)
    def test_timeline_backdrop_opacity_is_configurable(self):
        override = {"timeline_backdrop_opacity": {"dark": 0.4, "light": 0.9}}
        self.assertAlmostEqual(
            theme.parse_color(
                theme.tokens(_settings("dark", **override))["timeline.bg"]).alphaF(),
            0.4, places=2)
        theme.invalidate_cache()
        self.assertAlmostEqual(
            theme.parse_color(
                theme.tokens(_settings("light", **override))["timeline.bg"]).alphaF(),
            0.9, places=2)

    # 旧形式 (数値 1 つ) の setting.json も両モード共通値として受け付ける (後方互換)
    def test_timeline_backdrop_opacity_accepts_scalar(self):
        for mode in ("dark", "light"):
            theme.invalidate_cache()
            tokens = theme.tokens(_settings(mode, timeline_backdrop_opacity=0.7))
            self.assertAlmostEqual(
                theme.parse_color(tokens["timeline.bg"]).alphaF(), 0.7, places=2)

    # color() は QColor を返し、未定義名でも描画側を落とさない (§3-3)
    def test_color_lookup(self):
        self.assertEqual(theme.color("timeline.text", _settings("dark")).name().upper(),
                         "#ECECEC")
        self.assertEqual(theme.color("no.such.token", _settings("dark")).alpha(), 0)


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できないためスキップ")
class QssTest(unittest.TestCase):

    def setUp(self):
        theme.invalidate_cache()

    # 生成した QSS にプレースホルダが残らない (未解決トークンが無いこと)
    def test_no_placeholder_remains(self):
        for mode in ("dark", "light"):
            theme.invalidate_cache()
            qss = theme.build_qss(_settings(mode))
            leftovers = theme._PLACEHOLDER_RE.findall(qss)
            self.assertEqual(leftovers, [], f"{mode}: 未解決トークン {leftovers}")

    # 設計で定めた objectName のルールがすべて含まれる (§5.3)
    def test_contains_object_name_rules(self):
        qss = theme.build_qss(_settings("dark"))
        for name in (theme.PANEL, theme.PANEL_STRONG, theme.NOTE, theme.TITLE,
                     theme.PRIMARY_BUTTON, theme.DANGER_BUTTON):
            self.assertIn(f"#{name}", qss, name)

    # CSS のブロック括弧が壊れていない (置換が波括弧を巻き込んでいないこと)
    def test_braces_are_balanced(self):
        qss = theme.build_qss(_settings("light"))
        self.assertEqual(qss.count("{"), qss.count("}"))

    # パレットも両モードで生成できる (QSS が効かない箇所の保険)
    def test_build_palette(self):
        for mode in ("dark", "light"):
            theme.invalidate_cache()
            palette = theme.build_palette(_settings(mode))
            self.assertTrue(palette.color(palette.ColorRole.WindowText).isValid())


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できないためスキップ")
class ContrastTest(unittest.TestCase):
    # §3-4 の制約「本文 4.5:1 以上・見出し 3:1 以上」を両モードで満たすことを検証する。
    # 半透明のガラスは下地 (bg.from) の上に合成した実効色で判定する。

    def setUp(self):
        theme.invalidate_cache()

    # 半透明トークンを下地へ合成した不透明色を返す
    def _surface(self, tokens, token_name):
        return theme._flatten(theme.parse_color(tokens[token_name]),
                              theme.parse_color(tokens["bg.from"]))

    # 本文テキストがガラス面の上で 4.5:1 以上 (両モード)
    def test_primary_text_on_glass(self):
        for mode in ("dark", "light"):
            theme.invalidate_cache()
            tokens = theme.tokens(_settings(mode))
            text = theme.parse_color(tokens["text.primary"])
            for surface_name in ("glass.bg", "glass.bg.strong"):
                ratio = theme.contrast_ratio(text, self._surface(tokens, surface_name))
                self.assertGreaterEqual(
                    ratio, _TEXT_MIN_RATIO,
                    f"{mode}/{surface_name}: 本文 {ratio:.2f}:1")

    # 補足テキストは見出し相当の 3:1 以上 (両モード)
    def test_secondary_text_on_glass(self):
        for mode in ("dark", "light"):
            theme.invalidate_cache()
            tokens = theme.tokens(_settings(mode))
            text = theme.parse_color(tokens["text.secondary"])
            ratio = theme.contrast_ratio(text, self._surface(tokens, "glass.bg.strong"))
            self.assertGreaterEqual(ratio, _UI_MIN_RATIO, f"{mode}: 補足 {ratio:.2f}:1")

    # アクセント塗りの上の文字 (accent.on) が本文基準を満たす。
    # 差し色は設定で変えられるため、白文字を前提にせず自動導出した色で判定する (§3-4)。
    def test_accent_on_is_readable(self):
        for mode in ("dark", "light"):
            theme.invalidate_cache()
            tokens = theme.tokens(_settings(mode))
            on_accent = theme.parse_color(tokens["accent.on"])
            # 塗りに使う 3 色すべての上で読めること
            for key in ("accent", "accent.hover", "accent.pressed"):
                ratio = theme.contrast_ratio(on_accent, theme.parse_color(tokens[key]))
                self.assertGreaterEqual(
                    ratio, _TEXT_MIN_RATIO, f"{mode}/{key}: 塗り上の文字 {ratio:.2f}:1")

    # 線・枠・フォーカスに使うアクセント (accent.line) は背景から識別できる (3:1)
    def test_accent_line_is_visible_on_background(self):
        for mode in ("dark", "light"):
            theme.invalidate_cache()
            tokens = theme.tokens(_settings(mode))
            background = theme.parse_color(tokens["bg.from"])
            for key in ("accent.line", "danger.line"):
                ratio = theme.contrast_ratio(theme.parse_color(tokens[key]), background)
                self.assertGreaterEqual(
                    ratio, _UI_MIN_RATIO, f"{mode}/{key}: 背景との比 {ratio:.2f}:1")

    # 背景と近すぎる差し色を指定しても、線用の色は自動で基準まで寄せられる (§3-4)
    def test_accent_line_adjusts_low_contrast_accent(self):
        # ライト背景 (#F5F4F6) に対しほぼ同化する差し色を与える
        tokens = theme.tokens(_settings("light", accent_color="#FAF7F2"))
        background = theme.parse_color(tokens["bg.from"])
        self.assertLess(theme.contrast_ratio(theme.parse_color(tokens["accent"]),
                                             background), _UI_MIN_RATIO)
        self.assertGreaterEqual(
            theme.contrast_ratio(theme.parse_color(tokens["accent.line"]), background),
            _UI_MIN_RATIO)

    # ダークのグラデーション終端は塗り面のため、載せる文字が読めること
    def test_accent_bright_on_dark_background(self):
        tokens = theme.tokens(_settings("dark"))
        background = theme.parse_color(tokens["bg.from"])
        bright = theme.parse_color(tokens["accent.bright"])
        self.assertGreaterEqual(
            theme.contrast_ratio(bright, background), _UI_MIN_RATIO,
            "グラデーション終端と暗背景")
        self.assertGreaterEqual(
            theme.contrast_ratio(theme.parse_color(tokens["accent.on"]), bright),
            _TEXT_MIN_RATIO, "グラデーション終端の上の文字")

    # Timeline は常にダーク。どのモードでもトラック/ルーラの上でラベルが読める
    # (§3-6-3 / §5.7)。ライトでは下地が明るいぶん背景を濃く敷く必要がある。
    def test_timeline_text_on_track(self):
        for mode in ("dark", "light"):
            theme.invalidate_cache()
            tokens = theme.tokens(_settings(mode))
            # トラック背景 = ウィンドウ背景 → timeline.bg → 各トラック色 の重ね順
            base = theme._flatten(theme.parse_color(tokens["timeline.bg"]),
                                  theme.parse_color(tokens["bg.from"]))
            text = theme.parse_color(tokens["timeline.text"])
            for key in ("timeline.track", "timeline.track.alt", "timeline.ruler"):
                surface = theme._flatten(theme.parse_color(tokens[key]), base)
                ratio = theme.contrast_ratio(text, surface)
                self.assertGreaterEqual(
                    ratio, _TEXT_MIN_RATIO,
                    f"{mode}/{key}: Timeline のラベル {ratio:.2f}:1")


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できないためスキップ")
class SettingsIntegrationTest(unittest.TestCase):
    # setting.json の ui セクションが既定として組み込まれていること (§6)

    def test_default_settings_contains_ui(self):
        from src.settings.settings_window import DEFAULT_SETTINGS
        self.assertIn("ui", DEFAULT_SETTINGS)
        self.assertEqual(DEFAULT_SETTINGS["ui"]["theme"], "glass")
        self.assertEqual(DEFAULT_SETTINGS["ui"]["theme_mode"], "auto")
        # 既定を書き換えても theme 側の定義には影響しない (複製されていること)
        self.assertIsNot(DEFAULT_SETTINGS["ui"], theme.DEFAULT_UI_SETTINGS)

    # 欠落した入れ子キーが補完される (利用者が setting.json で調整できるように)
    def test_nested_defaults_are_filled(self):
        from src.settings import settings_window
        merged = {"ui": {"theme": "glass", "glass": {"dark": {"bg": 0.09}}}}
        changed = settings_window._fill_ui_nested_defaults(merged)
        self.assertTrue(changed)
        self.assertEqual(merged["ui"]["glass"]["dark"]["bg"], 0.09)  # 既存値は温存
        self.assertIn("bg_strong", merged["ui"]["glass"]["dark"])    # 欠落は補完
        self.assertIn("light", merged["ui"]["glass"])
        # 既定の実体を汚していないこと
        self.assertEqual(theme.DEFAULT_UI_SETTINGS["glass"]["dark"]["bg"], 0.07)


if __name__ == "__main__":
    unittest.main()
