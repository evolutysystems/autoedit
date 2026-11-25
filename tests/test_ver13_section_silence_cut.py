# セクション追加の無音カット選択 (docs/request/ver13/resolve1.md)
# 実行: python -m unittest discover -s tests
#
# 要件 (docs/request/ver13/resolve1.md §5):
#   ・追加ダイアログのチェックで、その区間だけ無音カットの有無を決められること (R1)
#   ・指定は区間ごとの clip_settings として運び、画面が持つ設定を書き換えないこと (R2)
# ffmpeg も Whisper も呼ばない (prepare_one_clip は差し替える)。
import copy
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialogButtonBox

from src.gui.timeline import archive_timeline_dialog as atd
from src.gui.timeline.section_add_dialog import SectionAddDialog

_SECTION_CFG = {
    "default_length_sec": 90.0,
    "min_length_sec": 5.0,
    "merge_on_overlap": True,
    "occupied_by": "used",
    "used_gap_merge_sec": 1.0,
    "max_ranges": 4,
}

# clip_writer.build_clip_settings が入れる形 (無音カットの出どころはここ 1 か所)
_CLIP_SETTINGS = {"silence_cut": {"enabled": True, "mode": "edit_points"},
                  "subtitle": {"review_enabled": True}}


def _app():
    return QApplication.instance() or QApplication([])


class SectionAddDialogTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._qt = _app()

    def _dialog(self, silence_cut_default=True):
        return SectionAddDialog(600.0, 100.0, _SECTION_CFG,
                                silence_cut_default=silence_cut_default)

    # T1: 既定が呼び出し側の値に従う
    def test_default_follows_the_caller(self):
        for default in (True, False):
            dialog = self._dialog(default)
            try:
                self.assertEqual(dialog.silence_cut_enabled(), default)
            finally:
                dialog.deleteLater()

    # T2: チェックを外すと False になる
    def test_unchecking_turns_it_off(self):
        dialog = self._dialog(True)
        try:
            dialog.silence_cut_check.setChecked(False)
            self.assertFalse(dialog.silence_cut_enabled())
        finally:
            dialog.deleteLater()

    # T3: 無音カットの選択は入力検証に影響しない (「追加」の可否が変わらない)
    def test_choice_does_not_affect_validation(self):
        dialog = self._dialog(True)
        try:
            ok_button = dialog.buttons.button(QDialogButtonBox.Ok)
            self.assertTrue(ok_button.isEnabled())
            dialog.silence_cut_check.setChecked(False)
            self.assertTrue(ok_button.isEnabled())
            # 区間が不正なら、チェックの状態に関係なく押せない
            dialog.end_edit.setText("0:00:00")
            self.assertFalse(ok_button.isEnabled())
        finally:
            dialog.deleteLater()

    # 既定値を渡さない従来の呼び出しでも落ちない (回帰)
    def test_default_argument_is_optional(self):
        dialog = SectionAddDialog(600.0, 100.0, _SECTION_CFG)
        try:
            self.assertTrue(dialog.silence_cut_enabled())
        finally:
            dialog.deleteLater()


# QThread を起動せずに _clip_settings_for だけを借りる器。
class _WorkerStub:

    _clip_settings_for = atd._SectionPrepareWorker._clip_settings_for

    def __init__(self, clip_settings):
        self._clip_settings = clip_settings
        self._settings_variants = {}


class ClipSettingsForJobTest(unittest.TestCase):

    def setUp(self):
        self.clip_settings = copy.deepcopy(_CLIP_SETTINGS)
        self.worker = _WorkerStub(self.clip_settings)

    def _enabled(self, settings):
        return bool(settings["silence_cut"]["enabled"])

    # T4: 指定が False なら無音カットを切った設定を渡す
    def test_false_gives_settings_with_silence_cut_off(self):
        got = self.worker._clip_settings_for({"silence_cut": False})
        self.assertFalse(self._enabled(got))
        self.assertIsNot(got, self.clip_settings)

    # T5: 画面の値と同じなら同一オブジェクト (無駄なコピーをしない)
    def test_same_value_reuses_the_original(self):
        got = self.worker._clip_settings_for({"silence_cut": True})
        self.assertIs(got, self.clip_settings)

    # T6: 指定が混在しても、それぞれ正しい設定になる
    def test_mixed_jobs_get_their_own_settings(self):
        jobs = [{"silence_cut": True}, {"silence_cut": False}, {"silence_cut": False}]
        got = [self.worker._clip_settings_for(job) for job in jobs]
        self.assertEqual([self._enabled(s) for s in got], [True, False, False])
        # False 用の控えは 1 つだけ作って使い回す
        self.assertIs(got[1], got[2])

    # T7: 画面が持つ設定を書き換えない
    def test_original_settings_are_untouched(self):
        self.worker._clip_settings_for({"silence_cut": False})
        self.assertTrue(self._enabled(self.clip_settings))

    # T8: 指定が無い従来の job は画面の値をそのまま使う (回帰)
    def test_job_without_the_key_uses_the_screen_value(self):
        got = self.worker._clip_settings_for({"index": 1, "start": 0.0, "end": 10.0})
        self.assertIs(got, self.clip_settings)

    # 画面側が無音カット OFF のとき、True の指定でコピーが作られる (逆向き)
    def test_screen_off_and_job_on(self):
        self.clip_settings["silence_cut"]["enabled"] = False
        got = self.worker._clip_settings_for({"silence_cut": True})
        self.assertTrue(self._enabled(got))
        self.assertFalse(self._enabled(self.clip_settings))


class SilenceCutNowTest(unittest.TestCase):

    # ダイアログへ渡す既定値が clip_settings から読まれる
    def test_reads_from_clip_settings(self):
        screen = mock.Mock()
        screen._clip_settings = {"silence_cut": {"enabled": False}}
        self.assertFalse(
            atd.ArchiveTimelineDialog._silence_cut_now(screen))
        screen._clip_settings = {"silence_cut": {"enabled": True}}
        self.assertTrue(
            atd.ArchiveTimelineDialog._silence_cut_now(screen))

    # 設定が無い・空のときは ON (従来の既定)
    def test_missing_settings_default_to_on(self):
        screen = mock.Mock()
        for settings in ({}, None, {"silence_cut": {}}):
            screen._clip_settings = settings
            self.assertTrue(atd.ArchiveTimelineDialog._silence_cut_now(screen))


if __name__ == "__main__":
    unittest.main()
