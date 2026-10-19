# Timeline 編集画面の大きさ・位置・モニターの記憶の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver8/resolve3.md §8):
#   ・閉じたときの大きさ・位置を控え、次に開くとき復元すること (§3-1)
#   ・クリップ用とアーカイブ用で別に覚えること (§3-1)
#   ・今のモニター構成から外れた位置は主モニターへ戻すこと (§3-2)
#   ・大きさとドック配置を 1 回の保存にまとめること (§3-3)
#   ・記憶が壊れていても既定サイズで開くこと (§3-1)
import base64
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QDialog, QSplitter, QWidget

from src.gui.timeline import timeline_editor_dialog as module
from src.timeline import builder, project_io

_DEFAULT_W = 1280
_DEFAULT_H = 820


def _app():
    return QApplication.instance() or QApplication([])


def _settings(**ui):
    base = {"window_width": _DEFAULT_W, "window_height": _DEFAULT_H}
    base.update(ui)
    return {"timeline": {"ui": base}}


# 今のモニターへ収まる大きさ (復元では Qt が画面内へ丸めるため、
# 画面より大きい値で検査すると丸めの結果と食い違う)
def _fits(shrink=0):
    available = QGuiApplication.primaryScreen().availableGeometry()
    return (available.width() // 2 - shrink, available.height() // 2 - shrink)


# 2 枚のパネルを持つ分割 (saveState / restoreState の検査に要る)
def _pane_splitter(orientation):
    splitter = QSplitter(orientation)
    splitter.addWidget(QWidget())
    splitter.addWidget(QWidget())
    return splitter


# 記憶の読み書きに要る部分だけを持つ窓 (画面全体は組まない)
class _Dialog(QDialog):

    _geometry_key = module.TimelineEditorDialog._geometry_key
    _restore_geometry = module.TimelineEditorDialog._restore_geometry
    _pull_into_screen = module.TimelineEditorDialog._pull_into_screen
    _save_window_state = module.TimelineEditorDialog._save_window_state
    _collect_geometry = module.TimelineEditorDialog._collect_geometry
    _collect_window_state = module.TimelineEditorDialog._collect_window_state
    _splitters = module.TimelineEditorDialog._splitters
    _restore_splitters = module.TimelineEditorDialog._restore_splitters
    _collect_splitters = module.TimelineEditorDialog._collect_splitters

    def __init__(self, settings, kind=project_io.KIND_CLIP):
        _app()
        super().__init__()
        self._settings = settings
        self._kind = kind
        self.controller = type(
            "C", (), {"cfg": builder.timeline_config(settings)})()
        self._splitter = _pane_splitter(Qt.Vertical)
        self._upper_splitter = _pane_splitter(Qt.Horizontal)

    def _project_kind(self):
        return self._kind

    def ui_cfg(self):
        return self.controller.cfg["ui"]


# save_settings を呼ばせずに回数だけ数える
class _CountedSave:

    def __init__(self):
        self.calls = 0

    def __enter__(self):
        self._original = module.save_settings
        module.save_settings = self._record
        return self

    def __exit__(self, *_exc):
        module.save_settings = self._original
        return False

    def _record(self, _settings):
        self.calls += 1


class GeometryMemoryTest(unittest.TestCase):

    # 閉じたときの大きさを控え、次に開いたときに復元すること (§3-1)
    def test_size_survives_a_reopen(self):
        settings = _settings()
        first = _Dialog(settings)
        size = _fits()
        first.resize(*size)
        with _CountedSave():
            first._save_window_state()

        store = settings["timeline"]["ui"]["window_geometry"]
        self.assertIn(project_io.KIND_CLIP, store)

        second = _Dialog(settings)
        second._restore_geometry(second.ui_cfg())
        self.assertEqual((second.width(), second.height()), size)

    # 記憶が無ければ setting.json の既定サイズで開くこと
    def test_without_memory_uses_the_default_size(self):
        dialog = _Dialog(_settings())
        dialog._restore_geometry(dialog.ui_cfg())
        self.assertEqual((dialog.width(), dialog.height()),
                         (_DEFAULT_W, _DEFAULT_H))

    # クリップ用とアーカイブ用は別に覚えること (要る広さが違う / §3-1)
    def test_kinds_are_remembered_separately(self):
        settings = _settings()
        clip = _Dialog(settings, kind=project_io.KIND_CLIP)
        clip.resize(*_fits(40))
        archive = _Dialog(settings, kind=project_io.KIND_ARCHIVE)
        archive_size = _fits()
        archive.resize(*archive_size)
        with _CountedSave():
            clip._save_window_state()
            archive._save_window_state()

        store = settings["timeline"]["ui"]["window_geometry"]
        self.assertEqual(sorted(store), [project_io.KIND_ARCHIVE,
                                         project_io.KIND_CLIP])

        reopened = _Dialog(settings, kind=project_io.KIND_ARCHIVE)
        reopened._restore_geometry(reopened.ui_cfg())
        self.assertEqual((reopened.width(), reopened.height()), archive_size)

    # 記憶が壊れていても例外にせず、既定サイズで開くこと (§3-1)
    def test_broken_memory_falls_back_to_the_default(self):
        settings = _settings(
            window_geometry={project_io.KIND_CLIP: "これは base64 ではない"})
        dialog = _Dialog(settings)
        dialog._restore_geometry(dialog.ui_cfg())
        self.assertEqual((dialog.width(), dialog.height()),
                         (_DEFAULT_W, _DEFAULT_H))

    # base64 としては読めても中身が壊れている控えも既定へ落ちること
    def test_garbage_state_falls_back_to_the_default(self):
        state = base64.b64encode(b"not a geometry").decode("ascii")
        settings = _settings(window_geometry={project_io.KIND_CLIP: state})
        dialog = _Dialog(settings)
        dialog._restore_geometry(dialog.ui_cfg())
        self.assertEqual((dialog.width(), dialog.height()),
                         (_DEFAULT_W, _DEFAULT_H))

    # 記憶を切れば控えを読まず、書かないこと
    def test_can_be_turned_off(self):
        settings = _settings(remember_window_geometry=False,
                             window_geometry={project_io.KIND_CLIP: "x"})
        dialog = _Dialog(settings)
        dialog._restore_geometry(dialog.ui_cfg())
        self.assertEqual((dialog.width(), dialog.height()),
                         (_DEFAULT_W, _DEFAULT_H))

        dialog.resize(900, 640)
        with _CountedSave() as saved:
            dialog._save_window_state()
        self.assertEqual(saved.calls, 0)
        self.assertEqual(settings["timeline"]["ui"]["window_geometry"],
                         {project_io.KIND_CLIP: "x"})

    # 大きさが変わっていなければ保存しないこと (閉じるたびに書かない)
    def test_unchanged_geometry_is_not_written_again(self):
        settings = _settings()
        dialog = _Dialog(settings)
        dialog.resize(900, 640)
        with _CountedSave() as saved:
            dialog._save_window_state()
            dialog._save_window_state()
        self.assertEqual(saved.calls, 1)

    # 派生画面ぶんの状態も同じ 1 回の保存に相乗りすること (§3-3)
    def test_extra_state_shares_one_save(self):
        settings = _settings()

        class _WithDock(_Dialog):
            def _collect_window_state(self, store):
                store["archive_dock_state"] = "dock"
                return True

        dialog = _WithDock(settings, kind=project_io.KIND_ARCHIVE)
        dialog.resize(900, 640)
        with _CountedSave() as saved:
            dialog._save_window_state()
        self.assertEqual(saved.calls, 1)
        ui = settings["timeline"]["ui"]
        self.assertEqual(ui["archive_dock_state"], "dock")
        self.assertIn(project_io.KIND_ARCHIVE, ui["window_geometry"])

    # 保存で落ちても閉じられること (記憶は「あると嬉しいもの」)
    def test_failure_to_save_does_not_raise(self):
        settings = _settings()
        dialog = _Dialog(settings)

        def _boom(_settings):
            raise OSError("書き込めません")

        original = module.save_settings
        module.save_settings = _boom
        try:
            dialog.resize(900, 640)
            dialog._save_window_state()      # 例外が出ないこと
        finally:
            module.save_settings = original


class SplitterMemoryTest(unittest.TestCase):

    # 分割位置 (プレビュー / Timeline) を控え、次に開くとき戻すこと (resolve4 §3-1)
    def test_split_position_survives_a_reopen(self):
        settings = _settings()
        first = _Dialog(settings)
        first._splitter.setSizes([220, 480])
        with _CountedSave():
            first._save_window_state()

        second = _Dialog(settings)
        second._restore_splitters(second.ui_cfg())
        self.assertEqual(second._splitter.sizes(), first._splitter.sizes())

    # プレビュー / インスペクタの分割も覚えること
    def test_both_splitters_are_remembered(self):
        settings = _settings()
        dialog = _Dialog(settings)
        dialog._upper_splitter.setSizes([700, 180])
        with _CountedSave():
            dialog._save_window_state()

        saved = settings["timeline"]["ui"]["splitter_state"][project_io.KIND_CLIP]
        self.assertEqual(sorted(saved), ["main", "upper"])

        reopened = _Dialog(settings)
        reopened._restore_splitters(reopened.ui_cfg())
        self.assertEqual(reopened._upper_splitter.sizes(),
                         dialog._upper_splitter.sizes())

    # 画面の種別ごとに別で覚えること (クリップ用 / アーカイブ用)
    def test_kinds_are_remembered_separately(self):
        settings = _settings()
        clip = _Dialog(settings, kind=project_io.KIND_CLIP)
        clip._splitter.setSizes([150, 550])
        archive = _Dialog(settings, kind=project_io.KIND_ARCHIVE)
        archive._splitter.setSizes([400, 300])
        with _CountedSave():
            clip._save_window_state()
            archive._save_window_state()

        saved = settings["timeline"]["ui"]["splitter_state"]
        self.assertEqual(sorted(saved), [project_io.KIND_ARCHIVE,
                                         project_io.KIND_CLIP])
        self.assertNotEqual(saved[project_io.KIND_CLIP]["main"],
                            saved[project_io.KIND_ARCHIVE]["main"])

    # 記憶が壊れていても例外にせず、既定の比率のままにすること
    def test_broken_state_is_ignored(self):
        settings = _settings(
            splitter_state={project_io.KIND_CLIP: {"main": "base64 ではない"}})
        dialog = _Dialog(settings)
        before = dialog._splitter.sizes()
        dialog._restore_splitters(dialog.ui_cfg())
        self.assertEqual(dialog._splitter.sizes(), before)

    # 記憶を切れば分割位置も読まず、書かないこと
    def test_can_be_turned_off(self):
        settings = _settings(remember_window_geometry=False)
        dialog = _Dialog(settings)
        dialog._splitter.setSizes([220, 480])
        with _CountedSave() as saved:
            dialog._save_window_state()
        self.assertEqual(saved.calls, 0)
        self.assertNotIn("splitter_state", settings["timeline"]["ui"])

    # 分割位置が変わっていなければ書き直さないこと
    def test_unchanged_state_is_not_written_again(self):
        settings = _settings()
        dialog = _Dialog(settings)
        dialog._splitter.setSizes([220, 480])
        with _CountedSave() as saved:
            dialog._save_window_state()
            dialog._save_window_state()
        self.assertEqual(saved.calls, 1)


class ScreenFallbackTest(unittest.TestCase):

    # 今あるモニターから外れた位置は主モニターの中へ戻すこと (§3-2)
    def test_offscreen_position_returns_to_the_primary_screen(self):
        dialog = _Dialog(_settings())
        available = QGuiApplication.primaryScreen().availableGeometry()
        dialog.resize(640, 480)
        dialog.move(available.right() + 4000, available.bottom() + 4000)
        dialog._pull_into_screen(dialog.ui_cfg())
        self.assertTrue(available.intersects(dialog.frameGeometry()))

    # 画面内にある位置は動かさないこと (利用者が置いた場所を尊重する)
    def test_onscreen_position_is_left_alone(self):
        dialog = _Dialog(_settings())
        available = QGuiApplication.primaryScreen().availableGeometry()
        dialog.resize(min(640, available.width()), min(480, available.height()))
        dialog.move(available.topLeft())
        before = dialog.frameGeometry()
        dialog._pull_into_screen(dialog.ui_cfg())
        self.assertEqual(dialog.frameGeometry(), before)

    # モニターより大きい窓は主モニターへ収まるまで縮めること (§3-2)
    def test_oversized_window_is_shrunk_to_the_screen(self):
        dialog = _Dialog(_settings())
        available = QGuiApplication.primaryScreen().availableGeometry()
        dialog.resize(available.width() + 2000, available.height() + 2000)
        dialog.move(available.right() + 4000, available.bottom() + 4000)
        dialog._pull_into_screen(dialog.ui_cfg())
        self.assertLessEqual(dialog.width(), available.width())
        self.assertLessEqual(dialog.height(), available.height())


class GeometrySettingsTest(unittest.TestCase):

    # 既定は「覚える」。控えの置き場は空の辞書 (§3-1)
    def test_defaults(self):
        ui = builder.timeline_config({})["ui"]
        self.assertTrue(ui["remember_window_geometry"])
        self.assertEqual(ui["window_geometry"], {})
        self.assertEqual(ui["window_min_visible_px"], 120)

    # 壊れた控え (辞書でない・空文字・文字列でない) は捨てること
    def test_broken_store_is_dropped(self):
        ui = builder.timeline_config(
            {"timeline": {"ui": {"window_geometry": "壊れている"}}})["ui"]
        self.assertEqual(ui["window_geometry"], {})

        ui = builder.timeline_config(
            {"timeline": {"ui": {"window_geometry": {"clip": "", "archive": 12,
                                                     "vertical": "ok"}}}})["ui"]
        self.assertEqual(ui["window_geometry"], {"vertical": "ok"})

    # 見え幅の下限は正の整数へ寄せること (0 や不正値は既定)
    def test_min_visible_px_is_positive(self):
        ui = builder.timeline_config(
            {"timeline": {"ui": {"window_min_visible_px": 0}}})["ui"]
        self.assertEqual(ui["window_min_visible_px"], 120)

    # 分割位置の控えも既定は空の辞書 (resolve4 §3-1)
    def test_splitter_store_defaults(self):
        self.assertEqual(builder.timeline_config({})["ui"]["splitter_state"], {})

    # 壊れた分割位置の控え (辞書でない・空文字・文字列でない) は捨てること
    def test_broken_splitter_store_is_dropped(self):
        ui = builder.timeline_config(
            {"timeline": {"ui": {"splitter_state": "壊れている"}}})["ui"]
        self.assertEqual(ui["splitter_state"], {})

        ui = builder.timeline_config({"timeline": {"ui": {"splitter_state": {
            "clip": {"main": "ok", "upper": "", "broken": 12},
            "archive": "辞書でない",
            "vertical": {},
        }}}})["ui"]
        self.assertEqual(ui["splitter_state"], {"clip": {"main": "ok"}})


if __name__ == "__main__":
    unittest.main()
