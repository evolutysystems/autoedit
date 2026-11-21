# CommandStack の観測口と操作ログの単体テスト
# (docs/AiEditor/resolve1.md §15 / §20 の test_command_stack_observer.py)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・`observer=None` (既定) で**現行と完全に同じ挙動**
#   ・push が成功したときだけ呼ばれる (失敗時・Undo/Redo では呼ばれない)
#   ・観測側で例外が出ても編集は止まらない
#   ・操作ログは 1 行 1 操作の追記 (<project>.editlog.jsonl)。スナップショットは書かない
#   ・既定は無効 (ai.learning.operation_log = false)
import json
import os
import tempfile
import unittest

from src.ai import edit_log
from src.timeline.commands import Command, CommandStack, MoveClip
from src.timeline.model import (BASE_VIDEO_TRACK_ID, MEDIA_VIDEO, TRACK_VIDEO,
                                Clip, MediaRef, Timeline, Track)


def _timeline():
    timeline = Timeline(fps=60)
    track = Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, is_base=True)
    timeline.tracks = [track]
    timeline.media_pool = [MediaRef("m1", MEDIA_VIDEO, "x.mp4", duration_sec=30.0)]
    track.clips = [Clip("c1", "m1", 0.0, 5.0, source_in=0.0, source_out=5.0),
                   Clip("c2", "m1", 10.0, 5.0, source_in=10.0, source_out=15.0)]

    return timeline


class _Noop(Command):

    label = "何もしない"

    def apply(self, timeline):
        return False


class _Broken(Command):

    label = "壊れる"

    def apply(self, timeline):
        raise RuntimeError("boom")


class ObserverTest(unittest.TestCase):

    def test_default_is_unchanged(self):
        timeline = _timeline()
        stack = CommandStack()

        self.assertTrue(stack.push(timeline, MoveClip("c1", 6.0)))
        self.assertTrue(stack.can_undo())

    def test_called_once_per_successful_push(self):
        timeline = _timeline()
        seen = []
        stack = CommandStack(observer=lambda label, command: seen.append(
            (label, type(command).__name__)))

        stack.push(timeline, MoveClip("c1", 6.0))

        self.assertEqual([("クリップの移動", "MoveClip")], seen)

    # 変化が無かった操作では呼ばれない (履歴も汚れない)。
    def test_not_called_when_nothing_changed(self):
        timeline = _timeline()
        seen = []
        stack = CommandStack(observer=lambda label, command: seen.append(label))

        self.assertFalse(stack.push(timeline, _Noop()))
        self.assertEqual([], seen)

    def test_not_called_when_the_command_fails(self):
        timeline = _timeline()
        seen = []
        stack = CommandStack(observer=lambda label, command: seen.append(label))

        self.assertFalse(stack.push(timeline, _Broken()))
        self.assertEqual([], seen)

    def test_not_called_on_undo_and_redo(self):
        timeline = _timeline()
        seen = []
        stack = CommandStack(observer=lambda label, command: seen.append(label))
        stack.push(timeline, MoveClip("c1", 6.0))
        seen.clear()

        stack.undo(timeline)
        stack.redo(timeline)

        self.assertEqual([], seen)

    # 記録の失敗で編集を止めない。
    def test_broken_observer_does_not_break_editing(self):
        timeline = _timeline()

        def broken(label, command):
            raise RuntimeError("boom")

        stack = CommandStack(observer=broken)

        self.assertTrue(stack.push(timeline, MoveClip("c1", 20.0)))
        self.assertEqual(20.0, timeline.clip_by_id("c1").timeline_start,
                         "記録が落ちても編集そのものは通る")


class EditLogTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.project = os.path.join(self._dir.name, "video.timeline.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_path(self):
        self.assertTrue(edit_log.log_path_for(self.project).endswith(
            ".timeline.editlog.jsonl"))
        self.assertEqual("", edit_log.log_path_for(""))

    # 既定は無効 (§15)。
    def test_disabled_by_default(self):
        self.assertFalse(edit_log.is_enabled({}))
        self.assertIsNone(edit_log.observer({}, self.project))

    def test_enabled_by_the_setting(self):
        settings = {"ai": {"learning": {"enabled": True, "operation_log": True}}}

        self.assertTrue(edit_log.is_enabled(settings))
        self.assertIsNotNone(edit_log.observer(settings, self.project))

    def test_learning_off_disables_the_log(self):
        settings = {"ai": {"learning": {"enabled": False, "operation_log": True}}}

        self.assertFalse(edit_log.is_enabled(settings))

    # 1 行 1 操作の追記。スナップショットは書かない。
    def test_records_one_line_per_operation(self):
        timeline = _timeline()
        settings = {"ai": {"learning": {"enabled": True, "operation_log": True}}}
        stack = CommandStack(observer=edit_log.observer(settings, self.project))

        stack.push(timeline, MoveClip("c1", 6.0))
        stack.push(timeline, MoveClip("c2", 20.0))

        path = edit_log.log_path_for(self.project)
        with open(path, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]

        self.assertEqual(2, len(rows))
        self.assertEqual("MoveClip", rows[0]["kind"])
        self.assertEqual("c1", rows[0]["params"]["clip_id"])
        self.assertEqual(6.0, rows[0]["params"]["new_start"])
        self.assertNotIn("snapshot", rows[0])

    def test_rows_skips_broken_lines(self):
        path = edit_log.log_path_for(self.project)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"at":"x","label":"a"}\n')
            handle.write("壊れた行\n")

        self.assertEqual(1, len(edit_log.EditLog(path).rows()))

    def test_missing_file_reads_as_empty(self):
        self.assertEqual([], edit_log.EditLog(
            os.path.join(self._dir.name, "nope.jsonl")).rows())

    # 本文は切って残す (長い文字列を丸ごと溜めない)。
    def test_long_strings_are_shortened(self):
        from src.timeline.commands import AddSubtitleClip

        command = AddSubtitleClip(0.0, 2.0, text="あ" * 300)
        params = edit_log.params_of(command)

        self.assertTrue(len(params["text"]) <= 80)


if __name__ == "__main__":
    unittest.main()
