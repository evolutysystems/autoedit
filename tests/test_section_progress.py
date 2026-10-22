# セクション追加の進捗表示の単体テスト (docs/error/fromUser/resolve.md §9-3)
# 実行: python -m unittest discover -s tests
#
# 背景 (2026-10-22 の報告):
#   「セクション追加」を押すと準備は成功するのに
#   AttributeError: 'NoneType' object has no attribute 'setLabelText' で落ちた。
#
#   モーダルの QProgressDialog の setValue() は内部で processEvents() を回す。
#   その最中にワーカーの完了通知が届いて _on_section_ready →
#   _close_section_progress が走り、self._section_progress が None になる。
#   setValue() から戻った次の行で None を触って落ちていた。
#
# 重点: 再入が起きても落ちないこと。Qt を起動せず、再入する偽ダイアログで再現する。
import unittest

from src.gui.timeline.archive_timeline_dialog import ArchiveTimelineDialog


# setValue() の中で「完了処理が割り込む」= 進捗ダイアログが捨てられる状況を作る。
class _ReentrantDialog:

    def __init__(self, owner):
        self._owner = owner
        self.labels = []
        self.values = []

    def setLabelText(self, text):
        self.labels.append(text)

    def setValue(self, value):
        self.values.append(value)
        # ここで processEvents() 相当の割り込みが起きる
        self._owner._section_progress = None


class _Owner:
    _on_section_progress = ArchiveTimelineDialog._on_section_progress

    def __init__(self):
        self._section_progress = None


class SectionProgressTest(unittest.TestCase):

    # 再入でダイアログが捨てられても落ちない (本題)
    def test_survives_reentrant_close(self):
        owner = _Owner()
        dialog = _ReentrantDialog(owner)
        owner._section_progress = dialog

        owner._on_section_progress(0.5, "準備中…")      # 例外が出なければ合格

        self.assertEqual(dialog.labels, ["準備中…"])
        self.assertEqual(dialog.values, [50])
        self.assertIsNone(owner._section_progress)

    # 既に閉じていれば何もしない
    def test_ignores_progress_after_close(self):
        owner = _Owner()
        owner._on_section_progress(0.5, "準備中…")      # 例外が出なければ合格

    # 進捗率は 0〜100 に収める (外れ値で Qt を怒らせない)
    def test_clamps_the_ratio(self):
        for ratio, expected in ((-1.0, 0), (0.0, 0), (1.0, 100), (2.5, 100)):
            with self.subTest(ratio=ratio):
                owner = _Owner()
                dialog = _ReentrantDialog(owner)
                owner._section_progress = dialog
                owner._on_section_progress(ratio, "x")
                self.assertEqual(dialog.values, [expected])


if __name__ == "__main__":
    unittest.main()
