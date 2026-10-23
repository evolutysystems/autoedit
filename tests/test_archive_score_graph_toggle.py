# 採点グラフ (アーカイブ用 Timeline 画面のドック) の表示切替の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver8/resolve2.md §8):
#   ・クリップバーのトグルとドックの表示状態が双方向で一致すること (§3-2)
#   ・ショートカットが timeline.shortcuts から登録されること (§3-3)
#   ・設定が既定で「必ず表示」になること (§3-1)
# 重点 (2026-10-23 の報告 / 採点グラフが出せない):
#   ・切り離したまま画面に出ていないときはドック位置へ戻すこと
#   ・配置の復元が「画面が出たとき 1 回だけ」であること
#   ・画面に出ていない切り離し位置を覚え直さないこと
import base64
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
def _dialog(shortcut_key="G", always_show=True, redock=True):
    _app()
    dialog = module.ArchiveTimelineDialog.__new__(module.ArchiveTimelineDialog)
    cfg = {
        "shortcuts": {"toggle_score_graph": shortcut_key},
        "ui": {
            "archive_graph_dock_always_show": always_show,
            "archive_graph_dock_redock_when_hidden": redock,
            "window_min_visible_px": 120,
            "archive_dock_state": "",
        },
    }
    dialog.controller = type("C", (), {"cfg": cfg})()
    dialog._dock_restored = False
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


# 「切り離してあるが画面には出ていない」状態を作る。
# 実機では、記憶した切り離し位置を画面に入る前に復元すると、
# 浮いた窓が画面へ出ないまま可視フラグだけ True になる (2026-10-23 の報告)。
def _floating_but_invisible(dialog):
    dialog.graph_dock.setFloating(True)
    dialog._dock_on_screen = lambda: False


class ScoreGraphRecoveryTest(unittest.TestCase):

    # 浮いたまま画面に出ていないときはドック位置へ戻すこと
    def test_invisible_floating_dock_is_docked_again(self):
        dialog = _dialog()
        dialog._bind_graph_toggle()
        _floating_but_invisible(dialog)
        dialog._ensure_graph_visible()
        self.assertFalse(dialog.graph_dock.isFloating())
        self.assertTrue(dialog.graph_dock.isVisible())

    # 画面に出ている切り離しはそのまま (利用者が選んだ配置を壊さない)
    def test_visible_floating_dock_is_kept(self):
        dialog = _dialog()
        dialog._bind_graph_toggle()
        dialog.graph_dock.setFloating(True)
        dialog._dock_on_screen = lambda: True
        dialog._ensure_graph_visible()
        self.assertTrue(dialog.graph_dock.isFloating())

    # 設定で戻しを切れること (記憶した切り離し位置をそのまま使う)
    def test_redock_can_be_disabled(self):
        dialog = _dialog(redock=False)
        dialog._bind_graph_toggle()
        _floating_but_invisible(dialog)
        dialog._ensure_graph_visible()
        self.assertTrue(dialog.graph_dock.isFloating())

    # ボタンを押して ON にすると「見える場所へ出す」を通ること
    # (押しても出ない、を無くす)
    def test_button_recovers_an_invisible_floating_dock(self):
        dialog = _dialog()
        dialog._bind_graph_toggle()
        _floating_but_invisible(dialog)
        dialog.graph_toggle.click()         # OFF (隠す)
        dialog.graph_toggle.click()         # ON  (出し直す = 戻す)
        self.assertFalse(dialog.graph_dock.isFloating())
        self.assertTrue(dialog.graph_dock.isVisible())

    # 押下以外の追従 (setChecked) では戻さないこと。
    # 切り離しの操作中にもボタンの状態は動くため、ここで戻すと
    # 利用者が意図して切り離した直後にドックへ引き戻してしまう。
    def test_following_the_state_does_not_redock(self):
        dialog = _dialog()
        dialog._bind_graph_toggle()
        dialog.graph_dock.setFloating(True)
        dialog._dock_on_screen = lambda: False
        dialog.graph_toggle.setChecked(False)
        dialog.graph_toggle.setChecked(True)
        self.assertTrue(dialog.graph_dock.isFloating())

    # ショートカットも同じ経路を通ること
    def test_shortcut_recovers_an_invisible_floating_dock(self):
        dialog = _dialog()
        dialog._bind_graph_toggle()
        _floating_but_invisible(dialog)
        dialog._toggle_graph_dock()         # 表示中なので隠す
        dialog._toggle_graph_dock()         # 出し直す = 戻す
        self.assertFalse(dialog.graph_dock.isFloating())
        self.assertTrue(dialog.graph_dock.isVisible())

    # 配置の復元は画面が出た最初の 1 回だけ (2 度目以降は動かさない)
    def test_dock_state_is_restored_once(self):
        dialog = _dialog()
        dialog._bind_graph_toggle()
        calls = []
        dialog._restore_dock_state = lambda ui_cfg: calls.append(ui_cfg)
        dialog._restore_dock_once()
        dialog._restore_dock_once()
        self.assertEqual(len(calls), 1)

    # always_show が False なら出し直さない (閉じたままを覚えたい利用者向け)
    def test_restore_respects_always_show(self):
        dialog = _dialog(always_show=False)
        dialog._bind_graph_toggle()
        dialog._restore_dock_state = lambda ui_cfg: None
        shown = []
        dialog._ensure_graph_visible = lambda: shown.append(True)
        dialog._restore_dock_once()
        self.assertEqual(shown, [])


class ScoreGraphStateSaveTest(unittest.TestCase):

    # 画面に出ている配置はそのまま覚えること
    def test_visible_dock_state_is_remembered(self):
        dialog = _dialog()
        dialog._bind_graph_toggle()
        store = {}
        self.assertTrue(dialog._collect_window_state(store))
        self.assertTrue(store["archive_dock_state"])
        # 同じ状態なら書き換えない
        self.assertFalse(dialog._collect_window_state(dict(store)))

    # 画面に出ていない切り離し位置は覚えないこと (次回は既定の配置で開く)
    def test_invisible_floating_state_is_not_remembered(self):
        dialog = _dialog()
        dialog._bind_graph_toggle()
        _floating_but_invisible(dialog)
        store = {"archive_dock_state": "old"}
        self.assertTrue(dialog._collect_window_state(store))
        self.assertEqual(store["archive_dock_state"], "")

    # 覚えた配置は base64 で、復元に使える形であること
    def test_remembered_state_is_base64(self):
        dialog = _dialog()
        dialog._bind_graph_toggle()
        store = {}
        dialog._collect_window_state(store)
        base64.b64decode(store["archive_dock_state"].encode("ascii"))


class ScoreGraphSettingsTest(unittest.TestCase):

    # 既定は「記憶した配置が閉じていても出し直す」(§3-1)
    def test_always_show_defaults_to_true(self):
        cfg = builder.timeline_config({})
        self.assertTrue(cfg["ui"]["archive_graph_dock_always_show"])
        self.assertEqual(cfg["shortcuts"]["toggle_score_graph"], "G")

    # 既定は「画面に出ていない切り離しはドック位置へ戻す」(2026-10-23 の報告)
    def test_redock_when_hidden_defaults_to_true(self):
        cfg = builder.timeline_config({})
        self.assertTrue(cfg["ui"]["archive_graph_dock_redock_when_hidden"])

    # 設定で切れること
    def test_redock_when_hidden_can_be_disabled(self):
        cfg = builder.timeline_config(
            {"timeline": {"ui": {"archive_graph_dock_redock_when_hidden": False}}})
        self.assertFalse(cfg["ui"]["archive_graph_dock_redock_when_hidden"])

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
