# Timeline 編集画面の取り扱い説明 (src/gui/timeline/help_dialog.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver9/resolve19.md §6 の受け入れ手順を自動化):
#   ・「？」ボタンが「元に戻す」の左に出ること (R1 / R2)
#   ・押すと説明の窓が開き、設定の順に画像が並ぶこと (R3 / R4)
#   ・スクロールできること・窓を縮めても画像が読める幅を保つこと (R5)
#   ・画像が見つからないときボタンを出さず、一部欠けても開けること (§3.4 / §3.5)
#   ・timeline_config が help を通し、壊れた値でも既定へ落ちること (§3.7)
#   ・画像が src/ に実在し、両方の .spec に同梱指定があること (§3.6)
import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QHBoxLayout,
        QPushButton,
        QScrollArea,
        QWidget,
    )

    from src.gui.timeline import help_dialog
    _QT_AVAILABLE = True
except Exception:  # noqa: BLE001 (PySide6 が無い環境ではスキップする)
    _QT_AVAILABLE = False

from src.timeline import builder

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src"

# 既定の説明画像 (設計 §3.7 / .spec の datas と同じ名前)
_DEFAULT_IMAGES = ["t1.jpg", "t2.jpg"]


def _app():
    return QApplication.instance() or QApplication([])


# 検査用の help 設定 (既定へ寄せたうえで、必要な値だけ差し替える)
def _help_cfg(**over):
    cfg = builder.timeline_config({})["help"]
    cfg.update(over)
    return cfg


# 画面を組まずに「？」を置ける最小のボタン行
def _button_row(parent):
    row = QHBoxLayout(parent)
    return row


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が無い環境ではスキップ")
class TestHelpButton(unittest.TestCase):

    # install_button は与えられた行の末尾へ置く。
    # 呼び出し側が「元に戻す」より先に呼ぶため、結果として undo の左に並ぶ (R1 / R2)
    def test_button_comes_before_undo(self):
        _app()
        host = QWidget()
        row = _button_row(host)
        button = help_dialog.install_button(row, _help_cfg(), lambda: None, parent=host)
        self.assertIsNotNone(button)
        undo = QPushButton("元に戻す", host)
        row.addWidget(undo)
        self.assertEqual(row.indexOf(button), 0)
        self.assertLess(row.indexOf(button), row.indexOf(undo))

    # ラベルは全角の「？」1 文字で、幅は設定どおり (§3.2)
    def test_button_label_and_width(self):
        _app()
        host = QWidget()
        button = help_dialog.install_button(
            _button_row(host), _help_cfg(button_width_px=48),
            lambda: None, parent=host)
        self.assertEqual(button.text(), "？")
        self.assertEqual(button.width(), 48)
        # Enter の取りこぼしで開かないようにする
        self.assertFalse(button.autoDefault())

    # Timeline 編集画面は「元に戻す」を足す前に install_button を呼ぶ (R2)。
    # 並びはここでしか決まらないため、呼び出しの前後関係を検査する。
    def test_editor_dialog_calls_before_undo(self):
        source = (_SRC / "gui" / "timeline" / "timeline_editor_dialog.py").read_text(
            encoding="utf-8")
        install_at = source.index("help_dialog.install_button(")
        undo_at = source.index("button_row.addWidget(self.undo_button)")
        self.assertLess(install_at, undo_at)

    # 説明を出せないときはボタンを置かない (§3.4)
    def test_button_absent_when_disabled(self):
        _app()
        host = QWidget()
        row = _button_row(host)
        self.assertIsNone(help_dialog.install_button(
            row, _help_cfg(enabled=False), lambda: None, parent=host))
        self.assertEqual(row.count(), 0)

    def test_button_absent_without_images(self):
        _app()
        host = QWidget()
        row = _button_row(host)
        self.assertIsNone(help_dialog.install_button(
            row, _help_cfg(images=[]), lambda: None, parent=host))
        self.assertEqual(row.count(), 0)


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が無い環境ではスキップ")
class TestResolveImagePath(unittest.TestCase):

    # 相対名は src/ の下を指す (§3.4)
    def test_relative_name(self):
        path = help_dialog.resolve_image_path("t1.jpg")
        self.assertIsNotNone(path)
        self.assertEqual(Path(path), _SRC / "t1.jpg")

    # 絶対パスはそのまま使う
    def test_absolute_path(self):
        target = _SRC / "t2.jpg"
        self.assertEqual(
            Path(help_dialog.resolve_image_path(str(target))), target)

    # 無い名前・空文字は None
    def test_missing_name(self):
        self.assertIsNone(help_dialog.resolve_image_path("not_exist_zzz.jpg"))
        self.assertIsNone(help_dialog.resolve_image_path(""))
        self.assertIsNone(help_dialog.resolve_image_path(None))

    # src/ の外へ出る相対指定は受け付けない (設定経由で任意ファイルを開かせない)
    def test_escaping_name_rejected(self):
        self.assertIsNone(help_dialog.resolve_image_path("../README.md"))
        self.assertIsNone(
            help_dialog.resolve_image_path("settings/../../docs/CLAUDE.md"))


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が無い環境ではスキップ")
class TestIsEnabled(unittest.TestCase):

    def test_enabled_with_existing_image(self):
        self.assertTrue(help_dialog.is_enabled(_help_cfg()))

    def test_disabled_by_flag(self):
        self.assertFalse(help_dialog.is_enabled(_help_cfg(enabled=False)))

    def test_disabled_without_readable_image(self):
        self.assertFalse(help_dialog.is_enabled(_help_cfg(images=[])))
        self.assertFalse(
            help_dialog.is_enabled(_help_cfg(images=["not_exist_zzz.jpg"])))

    # 1 枚でも読めれば出す (残りは案内文へ置き換える / §3.5)
    def test_enabled_with_one_readable_image(self):
        self.assertTrue(help_dialog.is_enabled(
            _help_cfg(images=["not_exist_zzz.jpg", "t2.jpg"])))

    def test_none_is_safe(self):
        self.assertFalse(help_dialog.is_enabled(None))


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が無い環境ではスキップ")
class TestHelpDialog(unittest.TestCase):

    def setUp(self):
        _app()

    # 設定の順に画像が並ぶ (R4)
    def test_pages_in_configured_order(self):
        dialog = help_dialog.TimelineHelpDialog(_help_cfg())
        pages = dialog.pages()
        self.assertEqual(len(pages), 2)
        # 1 枚目 = t1.jpg / 2 枚目 = t2.jpg を原寸の一致で確かめる
        for page, name in zip(pages, _DEFAULT_IMAGES):
            expected = QPixmap(str(_SRC / name))
            self.assertEqual(page._source.size(), expected.size())

    # スクロールできる (R5)
    def test_scrollable(self):
        dialog = help_dialog.TimelineHelpDialog(_help_cfg())
        areas = dialog.findChildren(QScrollArea)
        self.assertEqual(len(areas), 1)
        area = areas[0]
        self.assertTrue(area.widgetResizable())
        self.assertEqual(
            area.verticalScrollBarPolicy(), Qt.ScrollBarAsNeeded)
        self.assertEqual(
            area.horizontalScrollBarPolicy(), Qt.ScrollBarAsNeeded)

    # 開いた直後からビューポートいっぱいに広がる (§3.3)。
    # ビューポートの幅が決まるのは窓の resizeEvent より後のため、窓側だけを
    # 見ていると最初の 1 枚目が最小幅のまま止まる (実装中に踏んだ不具合)。
    def test_pages_fill_viewport_on_open(self):
        app = _app()
        dialog = help_dialog.TimelineHelpDialog(_help_cfg())
        dialog.resize(980, 760)
        dialog.show()
        app.processEvents()
        expected = dialog._scroll.viewport().width() - _help_cfg()["image_margin_px"] * 2
        # 最小幅に張り付いていないこと = 窓の広さが使われていること
        self.assertGreater(expected, _help_cfg()["image_min_width_px"])
        for page in dialog.pages():
            self.assertEqual(page.applied_width(), expected)

    # 窓を狭めると画像も縮む (最小幅までは追従する)
    def test_pages_follow_resize(self):
        app = _app()
        dialog = help_dialog.TimelineHelpDialog(_help_cfg())
        dialog.resize(980, 760)
        dialog.show()
        app.processEvents()
        wide = dialog.pages()[0].applied_width()
        dialog.resize(760, 600)
        app.processEvents()
        narrow = dialog.pages()[0].applied_width()
        self.assertLess(narrow, wide)
        self.assertGreaterEqual(narrow, _help_cfg()["image_min_width_px"])

    # 窓の大きさ・下限は設定どおり (§3.3)
    def test_window_size_from_settings(self):
        dialog = help_dialog.TimelineHelpDialog(_help_cfg(
            window_width=700, window_height=500,
            window_min_width_px=300, window_min_height_px=200))
        self.assertEqual(dialog.minimumWidth(), 300)
        self.assertEqual(dialog.minimumHeight(), 200)

    # 一部の画像が無くても開ける。残り 1 枚だけが並ぶ (§3.5)
    def test_missing_image_falls_back_to_note(self):
        dialog = help_dialog.TimelineHelpDialog(
            _help_cfg(images=["not_exist_zzz.jpg", "t2.jpg"]))
        self.assertEqual(len(dialog.pages()), 1)

    # 全部無くても例外にならない (ボタン自体は出ないが、直接開かれても壊れない)
    def test_all_missing_opens_empty(self):
        dialog = help_dialog.TimelineHelpDialog(
            _help_cfg(images=["not_exist_zzz.jpg"]))
        self.assertEqual(dialog.pages(), [])

    # 閉じるボタンで閉じられる
    def test_close_button(self):
        dialog = help_dialog.TimelineHelpDialog(_help_cfg())
        dialog.close_button.click()
        self.assertFalse(dialog.isVisible())


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が無い環境ではスキップ")
class TestHelpPageWidth(unittest.TestCase):

    def setUp(self):
        _app()
        # 検査用の横長画像 (1920x1080 の素材と同じ縦横比)
        self.pixmap = QPixmap(1920, 1080)
        self.pixmap.fill(QColor("#808080"))

    # 幅に合わせて縮み、縦横比を保つ (§3.3)
    def test_scales_down_keeping_aspect(self):
        page = help_dialog._HelpPage(self.pixmap, 100)
        page.apply_width(960)
        self.assertEqual(page.pixmap().width(), 960)
        self.assertEqual(page.pixmap().height(), 540)
        # 縦スクロールの総量になるので、高さも縮小後の実寸へ合わせる
        self.assertEqual(page.height(), 540)

    # 最小幅より狭くは縮めない (横スクロールで見せる / R5)
    def test_does_not_shrink_below_min(self):
        page = help_dialog._HelpPage(self.pixmap, 640)
        page.apply_width(200)
        self.assertEqual(page.applied_width(), 640)
        self.assertEqual(page.pixmap().width(), 640)

    # 等倍より大きくは拡大しない
    def test_does_not_scale_up(self):
        page = help_dialog._HelpPage(self.pixmap, 100)
        page.apply_width(4000)
        self.assertEqual(page.applied_width(), 1920)

    # 同じ幅で呼ばれたら貼り直さない (resizeEvent の再入で回り続けないため)
    def test_same_width_is_noop(self):
        page = help_dialog._HelpPage(self.pixmap, 100)
        page.apply_width(800)
        first = page.pixmap().cacheKey()
        page.apply_width(800)
        self.assertEqual(page.pixmap().cacheKey(), first)


class TestHelpConfig(unittest.TestCase):

    # timeline_config が help を通す (§1.7 / §3.7)
    def test_defaults(self):
        cfg = builder.timeline_config({})["help"]
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["images"], _DEFAULT_IMAGES)
        self.assertEqual(cfg["button_width_px"], 32)
        self.assertEqual(cfg["window_width"], 980)
        self.assertEqual(cfg["window_height"], 760)
        self.assertEqual(cfg["image_min_width_px"], 640)

    # 利用者の値が通る
    def test_user_values(self):
        cfg = builder.timeline_config({"timeline": {"help": {
            "enabled": False,
            "images": ["a.jpg", "b.jpg", "c.jpg"],
            "button_width_px": 40,
        }}})["help"]
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["images"], ["a.jpg", "b.jpg", "c.jpg"])
        self.assertEqual(cfg["button_width_px"], 40)

    # 壊れた寸法は既定へ落ちる (0 / 負数 / 文字列)
    def test_broken_sizes_fall_back(self):
        cfg = builder.timeline_config({"timeline": {"help": {
            "button_width_px": 0,
            "window_width": -100,
            "image_min_width_px": "広め",
        }}})["help"]
        self.assertEqual(cfg["button_width_px"], 32)
        self.assertEqual(cfg["window_width"], 980)
        self.assertEqual(cfg["image_min_width_px"], 640)

    # images が一覧でなければ既定へ落ちる
    def test_broken_images_fall_back(self):
        for broken in ("t1.jpg", {"a": "t1.jpg"}, 5):
            cfg = builder.timeline_config(
                {"timeline": {"help": {"images": broken}}})["help"]
            self.assertEqual(cfg["images"], _DEFAULT_IMAGES, msg=repr(broken))

    # 空文字・重複は捨て、指定順は保つ
    def test_images_normalized(self):
        cfg = builder.timeline_config({"timeline": {"help": {
            "images": ["t2.jpg", "", "t1.jpg", "t2.jpg", None, "  t3.jpg  "],
        }}})["help"]
        self.assertEqual(cfg["images"], ["t2.jpg", "t1.jpg", "t3.jpg"])

    # 空の一覧は空のまま (「？」を出さない指定として意味を持つ)
    def test_empty_images_kept(self):
        cfg = builder.timeline_config(
            {"timeline": {"help": {"images": []}}})["help"]
        self.assertEqual(cfg["images"], [])


class TestHelpAssets(unittest.TestCase):

    # 説明画像が src/ に実在する (§3.6)
    def test_images_exist(self):
        for name in _DEFAULT_IMAGES:
            self.assertTrue((_SRC / name).is_file(), msg=name)

    # 両方の .spec の datas に同梱指定がある。
    # 入れ忘れると開発環境では動き、配布版だけ「？」が消える (§3.6)
    def test_specs_bundle_images(self):
        for spec in ("main_window.spec", "main.spec"):
            source = (_SRC / spec).read_text(encoding="utf-8")
            for name in _DEFAULT_IMAGES:
                pattern = r"\(\s*'%s'\s*,\s*'src'\s*\)" % re.escape(name)
                self.assertRegex(source, pattern, msg="%s / %s" % (spec, name))

    # 既定設定 (settings_window) と setting.json の help が揃っている
    def test_default_settings_has_help(self):
        from src.settings import settings_window

        default = settings_window.DEFAULT_SETTINGS["timeline"]["help"]
        self.assertEqual(default["images"], _DEFAULT_IMAGES)
        self.assertTrue(default["enabled"])


if __name__ == "__main__":
    unittest.main()
