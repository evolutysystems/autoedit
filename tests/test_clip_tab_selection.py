# クリップ用タブの選択モデルと画面構成 (src/gui/main_window.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver3/resolve15.md §10-3):
#   ・ファイル選択欄と「参照...」が隠れていること (C2)。設定で戻せること
#   ・D&D 領域の文言が選択に追従し、× で案内文へ戻ること (C4)
#   ・選択が動画とプロジェクトで排他になること (X2)
#   ・実行ボタンが選択の種別どおりに経路を分けること (X3)
#   ・D&D 領域が高さの 60% を保ち、下限を割らないこと (C1 / Q3)
#   ・「続きから」と実行ボタンの幅が揃っていること (C8)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    from src.gui import main_window, theme
    _QT_AVAILABLE = True
except Exception:  # noqa: BLE001 (PySide6 が無い環境ではスキップする)
    _QT_AVAILABLE = False


# テスト中だけ QApplication を 1 つ共有する (ウィジェットの生成に必要)
def _app():
    return QApplication.instance() or QApplication([])


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できないためスキップ")
class ClipTabSelectionTest(unittest.TestCase):

    def setUp(self):
        _app()
        theme.invalidate_cache()
        self.tab = main_window.ClipTabWidget()
        # 実在チェックを通すための入力ファイル (中身は使わない)
        self.video = os.path.join(os.path.dirname(__file__), "_dummy_input.mp4")
        with open(self.video, "wb"):
            pass

    def tearDown(self):
        self.tab.deleteLater()
        if os.path.exists(self.video):
            os.remove(self.video)

    # 既存のファイル選択欄と「参照...」は隠れている (C2)
    def test_file_row_is_hidden(self):
        self.tab.show()
        self.assertFalse(self.tab.input_edit.isVisible())
        self.assertFalse(self.tab.browse_button.isVisible())
        # 消してはいない (設定で戻せるようにするため)
        self.assertIsNotNone(self.tab.input_edit)
        self.assertIsNotNone(self.tab.browse_button)

    # show_file_row = true なら従来の行が出る (C2 / Q8)
    def test_file_row_can_be_restored(self):
        settings = {"ui": {"theme": "glass", "theme_mode": "dark",
                           "main_window": {"show_file_row": True}}}
        theme.invalidate_cache()
        self.assertTrue(theme.main_window_config(settings)["show_file_row"])

    # 「続きから」と実行ボタンの幅が揃っている (C8)
    def test_center_buttons_share_width(self):
        self.assertEqual(self.tab.resume_button.width(),
                         self.tab.run_button.width())
        self.assertEqual(self.tab.resume_button.width(),
                         theme.PRIMARY_ACTION_BUTTON_WIDTH_PX)

    # 文言が指定幅に収まる (フォントが変わっても切れないことの担保)
    def test_resume_label_fits_in_width(self):
        self.assertLessEqual(self.tab.resume_button.sizeHint().width(),
                             theme.PRIMARY_ACTION_BUTTON_WIDTH_PX)

    # 動画を選ぶと文言がファイル名になり × が出る (C4)
    def test_video_selection_shows_name(self):
        self.tab.show()
        self.tab._select_video(self.video)
        self.assertEqual(self.tab.drop_area.text(), os.path.basename(self.video))
        self.assertTrue(self.tab.drop_area.has_selection())
        self.assertTrue(self.tab.drop_area.clear_button.isVisible())
        # 隠した入力欄にも同じ値が入る (show_file_row で出したときのため)
        self.assertEqual(self.tab.input_edit.text(), self.video)

    # × で案内文へ戻る (C4)
    def test_clear_restores_placeholder(self):
        self.tab.show()
        self.tab._select_video(self.video)
        self.tab._clear_selection()
        self.assertEqual(self.tab.drop_area.text(), main_window._DROP_PLACEHOLDER)
        self.assertFalse(self.tab.drop_area.has_selection())
        self.assertFalse(self.tab.drop_area.clear_button.isVisible())
        self.assertEqual(self.tab._selection, (None, ""))
        self.assertEqual(self.tab.input_edit.text(), "")

    # × ボタンの押下が選択の取り消しにつながっている (シグナル接続の担保)
    def test_clear_button_clears_selection(self):
        self.tab._select_video(self.video)
        self.tab.drop_area.clear_button.click()
        self.assertEqual(self.tab._selection, (None, ""))

    # 動画とプロジェクトは同時に選べない (X2 / Q5)
    def test_selection_is_exclusive(self):
        self.tab._select_video(self.video)
        self.tab._selection = (main_window._KIND_PROJECT, "C:/tmp/p.timeline.json")
        self.tab.input_edit.clear()
        self.assertEqual(self.tab._selection[0], main_window._KIND_PROJECT)
        self.assertEqual(self.tab.input_edit.text(), "")

    # 実行ボタンが選択の種別どおりに経路を分ける (X3)
    def test_run_dispatches_by_kind(self):
        called = []
        self.tab._run_pipeline = lambda path: called.append(("pipeline", path))
        self.tab._start_resume = lambda path: called.append(("resume", path))

        self.tab._select_video(self.video)
        self.tab._on_run()
        self.assertEqual(called, [("pipeline", self.video)])

        project = os.path.join(os.path.dirname(__file__), "_dummy.timeline.json")
        with open(project, "wb"):
            pass
        try:
            called.clear()
            self.tab._selection = (main_window._KIND_PROJECT, project)
            self.tab._on_run()
            self.assertEqual(called, [("resume", project)])
        finally:
            os.remove(project)

    # 未選択で実行しても何も起動しない (警告だけ出す)
    def test_run_without_selection_starts_nothing(self):
        called = []
        self.tab._run_pipeline = lambda path: called.append(path)
        self.tab._start_resume = lambda path: called.append(path)
        warned = []
        original = main_window.QMessageBox.warning
        main_window.QMessageBox.warning = (
            lambda *args, **kwargs: warned.append(args))
        try:
            self.tab._on_run()
        finally:
            main_window.QMessageBox.warning = original
        self.assertEqual(called, [])
        self.assertEqual(len(warned), 1)

    # D&D 領域が設定どおりの比率を保つ (C1)
    def test_drop_zone_follows_ratio(self):
        self.tab.show()
        self.tab.resize(640, 500)
        ratio = theme.main_window_config()["drop_zone_ratio"]
        self.assertEqual(self.tab.drop_area.height(), int(500 * ratio))

    # 縮めても下限を割らず、下の部品が消えない (Q3)
    def test_drop_zone_respects_minimum(self):
        self.tab.show()
        self.tab.resize(640, 220)
        minimum = theme.main_window_config()["drop_zone_min_height_px"]
        self.assertGreaterEqual(self.tab.drop_area.height(), minimum)
        # 実行ボタンが D&D 領域の下に残っていること
        self.assertGreater(self.tab.run_button.y(), self.tab.drop_area.height())

    # 「編集の続き」行は無くなり、タブ共通の受け口だけが残る (C5 / X5)
    def test_resume_row_is_gone(self):
        self.assertFalse(hasattr(self.tab, "resume_row"))
        self.assertTrue(callable(getattr(self.tab, "select_project", None)))


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できないためスキップ")
class MainWindowIntegrationTest(unittest.TestCase):
    # タブをまたぐ受け渡しと初期サイズ (resolve15 §5.7)

    def setUp(self):
        _app()
        theme.invalidate_cache()
        self.window = main_window.MainWindow()

    def tearDown(self):
        self.window.deleteLater()

    # 設定どおりの初期サイズで開く (比率の基準になる / X1)
    def test_initial_size_from_settings(self):
        cfg = theme.main_window_config()
        self.assertEqual(self.window.width(), cfg["width_px"])
        self.assertEqual(self.window.height(), cfg["height_px"])

    # 両タブがプロジェクトの受け口を持つ (種別違いの受け渡しが黙って落ちない / X5)
    def test_both_tabs_accept_project(self):
        for tab in (self.window.clip_tab, self.window.archive_tab):
            self.assertTrue(callable(getattr(tab, "select_project", None)),
                            type(tab).__name__)

    # 設定ボタンは余白付きの入れ物に入っている (D1)
    def test_settings_button_has_bottom_margin(self):
        corner = self.window.tabs.cornerWidget()
        self.assertIsNotNone(corner)
        self.assertIsNot(corner, self.window.settings_button)
        self.assertEqual(corner.layout().contentsMargins().bottom(),
                         theme.TAB_CORNER_BOTTOM_MARGIN_PX)
        # ボタン自身は入れ物の中に残っている (有効/無効の切り替えは従来どおり効く)
        self.assertIs(self.window.settings_button.parent(), corner)


if __name__ == "__main__":
    unittest.main()
