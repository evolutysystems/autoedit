# 採点グラフ (アーカイブ用 Timeline 画面のドック) の表示切替の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver8/resolve2.md §8):
#   ・クリップバーのトグルとドックの表示状態が双方向で一致すること (§3-2)
#   ・ショートカットが timeline.shortcuts から登録されること (§3-3)
#   ・設定が既定で「必ず表示」になること (§3-1)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QMainWindow,
    QPushButton,
    QWidget,
)

from src.gui.timeline import archive_timeline_dialog as module
from src.timeline import builder


def _app():
    return QApplication.instance() or QApplication([])


# 結線の検査に要る最小限だけを持つ器 (画面全体は組まない)
def _dialog(shortcut_key="G"):
    _app()
    dialog = module.ArchiveTimelineDialog.__new__(module.ArchiveTimelineDialog)
    dialog.controller = type(
        "C", (), {"cfg": {"shortcuts": {"toggle_score_graph": shortcut_key}}})()
    host = QMainWindow()
    dock = QDockWidget("score")
    dock.setObjectName("archiveScoreGraph")
    dock.setWidget(QWidget())
    host.addDockWidget(module.Qt.TopDockWidgetArea, dock)
    host.show()
    dialog._dock_host = host
    dialog.graph_dock = dock
    dialog.graph_toggle = QPushButton("採点グラフ")
    dialog.graph_toggle.setCheckable(True)
    dialog.graph_toggle.setChecked(True)
    return dialog


class ScoreGraphToggleTest(unittest.TestCase):

    # ボタンを押すとドックが消え、もう一度押すと戻ること (§3-2)
    def test_button_shows_and_hides_the_dock(self):
        dialog = _dialog()
        dialog._bind_graph_toggle()
        self.assertTrue(dialog.graph_toggle.isChecked())
        self.assertTrue(dialog.graph_dock.isVisible())

        dialog.graph_toggle.click()
        self.assertFalse(dialog.graph_dock.isVisible())
        dialog.graph_toggle.click()
        self.assertTrue(dialog.graph_dock.isVisible())

    # ドックを × で閉じたときもボタンの押下状態が追従すること (§3-2)
    def test_closing_the_dock_releases_the_button(self):
        dialog = _dialog()
        dialog._bind_graph_toggle()
        dialog.graph_dock.close()
        self.assertFalse(dialog.graph_toggle.isChecked())
        # 出し直す口が残っているので、ボタンから復帰できる
        dialog.graph_toggle.click()
        self.assertTrue(dialog.graph_dock.isVisible())

    # 結線時点で閉じていたら、ボタンも押されていない状態から始まること
    def test_bind_follows_the_current_state(self):
        dialog = _dialog()
        dialog.graph_dock.hide()
        dialog._bind_graph_toggle()
        self.assertFalse(dialog.graph_toggle.isChecked())

    # ショートカット 1 打鍵で入れ替わり、ボタンも追従すること (§3-3)
    def test_shortcut_toggles_both(self):
        dialog = _dialog()
        dialog._bind_graph_toggle()
        dialog._toggle_graph_dock()
        self.assertFalse(dialog.graph_dock.isVisible())
        self.assertFalse(dialog.graph_toggle.isChecked())
        dialog._toggle_graph_dock()
        self.assertTrue(dialog.graph_dock.isVisible())
        self.assertTrue(dialog.graph_toggle.isChecked())

    # 基底のフック経由で timeline.shortcuts の action 名を登録すること (§3-3)
    def test_registers_the_configured_action(self):
        dialog = _dialog()
        bound = []
        dialog._register_extra_shortcuts(
            lambda action, handler: bound.append((action, handler)))
        self.assertEqual([action for action, _ in bound], ["toggle_score_graph"])
        self.assertEqual(bound[0][1], dialog._toggle_graph_dock)

    # ツールチップへキー割り当てを出すこと (未割り当てなら文言だけ)
    def test_tooltip_mentions_the_key(self):
        dialog = _dialog()
        dialog._bind_graph_toggle()
        self.assertIn("(G)", dialog.graph_toggle.toolTip())

        plain = _dialog(shortcut_key="")
        plain._bind_graph_toggle()
        self.assertNotIn("(", plain.graph_toggle.toolTip())


class ScoreGraphSettingsTest(unittest.TestCase):

    # 既定は「記憶した配置が閉じていても出し直す」(§3-1)
    def test_always_show_defaults_to_true(self):
        cfg = builder.timeline_config({})
        self.assertTrue(cfg["ui"]["archive_graph_dock_always_show"])
        self.assertEqual(cfg["shortcuts"]["toggle_score_graph"], "G")

    # 設定で切れること (閉じたままを覚えたい利用者向け)
    def test_always_show_can_be_disabled(self):
        cfg = builder.timeline_config(
            {"timeline": {"ui": {"archive_graph_dock_always_show": False}}})
        self.assertFalse(cfg["ui"]["archive_graph_dock_always_show"])

    # キー割り当ては setting.json から差し替えられること
    def test_shortcut_can_be_remapped(self):
        cfg = builder.timeline_config(
            {"timeline": {"shortcuts": {"toggle_score_graph": "F2"}}})
        self.assertEqual(cfg["shortcuts"]["toggle_score_graph"], "F2")


if __name__ == "__main__":
    unittest.main()
