# 役割を変えてもテロップの置き場所を守る (ver9 resolve10 §5) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・増やしたトラック (S3 以降) のテロップが役割変更で S1 へ引き戻されないこと
#     (報告の症状そのもの / resolve10 §0.2 D・E・G)
#   ・S1 ↔ コメント用 S2 の往復は従来どおり効くこと (ver3 resolve11 の価値)
#   ・同じ役割を選び直しても何も起きないこと (履歴も居場所も動かない / §3.3)
#   ・増やしたトラックがコメント用と誤認されないこと (§3.4)
#   ・役割コンボは利用者が選び直したときだけ効くこと (ホイール事故の防止 / §2.4)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    from src.gui.timeline import timeline_editor_dialog as ted
    from src.gui.timeline.timeline_controller import TimelineController
    _QT_AVAILABLE = True
except Exception:  # noqa: BLE001 (PySide6 が無い環境ではスキップする)
    _QT_AVAILABLE = False

from src.timeline import commands
from src.timeline.builder import timeline_config
from src.timeline.model import (
    BASE_SUBTITLE_TRACK_ID,
    BASE_VIDEO_TRACK_ID,
    COMMENT_SUBTITLE_TRACK_ID,
    ROLE_COMMENT,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    SubtitleClip,
    Timeline,
    Track,
)


def _cfg(**overrides):
    cfg = dict(timeline_config({})["subtitle_tracks"])
    cfg.update(overrides)
    return cfg


# S1 に字幕を持つ最小の Timeline
def _timeline(clips=None):
    timeline = Timeline(fps=60, width=1920, height=1080)
    timeline.tracks = [
        Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, name="Video 1", is_base=True),
        Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1",
              clips=list(clips or [SubtitleClip("s1", 1.0, 2.0, text="字幕")])),
    ]
    return timeline


# 「①新規テロップトラックを作成し、テロップを移す」ところまで進めた Timeline を返す
def _moved_to_new_track(clip_ids=("s1",), clips=None, cfg=None):
    timeline = _timeline(clips)
    stack = commands.CommandStack()
    command = commands.MoveSubtitlesToNewTrack(list(clip_ids), cfg or _cfg())
    stack.push(timeline, command)
    return timeline, stack, command.created_track_id


class RoleChangeKeepsTrackTest(unittest.TestCase):

    # T20: 増やしたトラックのテロップは、役割を変えてもその場に残る (報告の症状)
    def test_role_change_keeps_user_track(self):
        timeline, stack, track_id = _moved_to_new_track()
        self.assertEqual(track_id, "S3")
        command = commands.ChangeSubtitleRole("s1", "sub", _cfg())
        self.assertTrue(stack.push(timeline, command))
        self.assertEqual(timeline.track_of_clip("s1").id, "S3")
        self.assertEqual(timeline.clip_by_id("s1").role, "sub")
        self.assertFalse(command.moved)
        self.assertTrue(command.move_skipped)

    # T21: コメント化してもその場に残る (S2 は作られない / §7 Q1)
    def test_comment_role_keeps_user_track(self):
        timeline, stack, _ = _moved_to_new_track()
        command = commands.ChangeSubtitleRole("s1", ROLE_COMMENT, _cfg())
        self.assertTrue(stack.push(timeline, command))
        self.assertEqual(timeline.track_of_clip("s1").id, "S3")
        self.assertEqual(timeline.clip_by_id("s1").role, ROLE_COMMENT)
        self.assertIsNone(timeline.track_by_id(COMMENT_SUBTITLE_TRACK_ID))

    # T22: T21 の後に S1 のテロップをコメント化すると、S3 ではなく S2 が生える
    #      (増やしたトラックがコメント用と誤認されない / §3.4)
    def test_later_comment_does_not_flow_into_user_track(self):
        timeline, stack, _ = _moved_to_new_track(clips=[
            SubtitleClip("s1", 1.0, 2.0, text="移す"),
            SubtitleClip("s2", 10.0, 2.0, text="あとでコメントにする"),
        ])
        stack.push(timeline, commands.ChangeSubtitleRole("s1", ROLE_COMMENT, _cfg()))
        stack.push(timeline, commands.ChangeSubtitleRole("s2", ROLE_COMMENT, _cfg()))
        self.assertEqual(timeline.track_of_clip("s1").id, "S3")
        self.assertEqual(timeline.track_of_clip("s2").id, COMMENT_SUBTITLE_TRACK_ID)

    # T23: S1 のテロップはコメント化で S2 へ、解除で S1 へ (従来の価値が残っている)
    def test_base_track_still_moves_both_ways(self):
        timeline = _timeline()
        stack = commands.CommandStack()
        stack.push(timeline, commands.ChangeSubtitleRole("s1", ROLE_COMMENT, _cfg()))
        self.assertEqual(timeline.track_of_clip("s1").id, COMMENT_SUBTITLE_TRACK_ID)
        stack.push(timeline, commands.ChangeSubtitleRole("s1", "streamer", _cfg()))
        self.assertEqual(timeline.track_of_clip("s1").id, BASE_SUBTITLE_TRACK_ID)

    # T24: 同じ役割を選び直しただけなら何も起きない (履歴も居場所も動かない)
    def test_same_role_does_nothing(self):
        timeline, stack, _ = _moved_to_new_track()
        depth = len(stack._undo)                     # noqa: SLF001 (履歴の深さを見る)
        command = commands.ChangeSubtitleRole("s1", "streamer", _cfg())
        self.assertFalse(stack.push(timeline, command))
        self.assertEqual(timeline.track_of_clip("s1").id, "S3")
        self.assertEqual(len(stack._undo), depth)    # noqa: SLF001
        self.assertFalse(command.moved)

    # T25: role_move_scope="all" なら従来どおり S1 へ戻る (後方互換)
    def test_scope_all_restores_old_behaviour(self):
        cfg = _cfg(role_move_scope="all")
        timeline, stack, _ = _moved_to_new_track(cfg=cfg)
        command = commands.ChangeSubtitleRole("s1", "sub", cfg)
        self.assertTrue(stack.push(timeline, command))
        self.assertEqual(timeline.track_of_clip("s1").id, BASE_SUBTITLE_TRACK_ID)
        self.assertTrue(command.moved)
        self.assertFalse(command.move_skipped)

    # T26: auto_move_on_role_change=False はどこに居ても移動しない (既存キー)
    def test_auto_move_disabled(self):
        cfg = _cfg(auto_move_on_role_change=False)
        timeline = _timeline()
        stack = commands.CommandStack()
        stack.push(timeline, commands.ChangeSubtitleRole("s1", ROLE_COMMENT, cfg))
        self.assertEqual(timeline.track_of_clip("s1").id, BASE_SUBTITLE_TRACK_ID)
        self.assertEqual(timeline.clip_by_id("s1").role, ROLE_COMMENT)

    # 設定が未知の値でも既定 ("role_tracks") として扱う
    def test_unknown_scope_falls_back_to_default(self):
        cfg = timeline_config({"subtitle_tracks": {"role_move_scope": "nope"}})
        self.assertEqual(cfg["subtitle_tracks"]["role_move_scope"], "role_tracks")

    # T27: Undo / Redo で居場所と役割がそろって戻る
    def test_undo_redo(self):
        timeline, stack, _ = _moved_to_new_track()
        stack.push(timeline, commands.ChangeSubtitleRole("s1", ROLE_COMMENT, _cfg()))
        stack.undo(timeline)
        self.assertEqual(timeline.track_of_clip("s1").id, "S3")
        self.assertEqual(timeline.clip_by_id("s1").role, "streamer")
        stack.redo(timeline)
        self.assertEqual(timeline.track_of_clip("s1").id, "S3")
        self.assertEqual(timeline.clip_by_id("s1").role, ROLE_COMMENT)

    # S1 のテロップをコメント化 → S2 のテロップの役割を戻すと S1 へ帰る
    # (コメント用トラックも「役割が面倒を見るトラック」である確認)
    def test_comment_track_is_role_managed(self):
        timeline = _timeline()
        self.assertTrue(commands.is_role_managed_subtitle_track(
            timeline, timeline.track_by_id(BASE_SUBTITLE_TRACK_ID), _cfg()))
        timeline.tracks.append(Track(COMMENT_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 2,
                                     name="Comment"))
        timeline.tracks.append(Track("S3", TRACK_SUBTITLE, 3, name="Subtitle 3"))
        self.assertTrue(commands.is_role_managed_subtitle_track(
            timeline, timeline.track_by_id(COMMENT_SUBTITLE_TRACK_ID), _cfg()))
        self.assertFalse(commands.is_role_managed_subtitle_track(
            timeline, timeline.track_by_id("S3"), _cfg()))


# T29: 役割コンボは「利用者が選び直したとき」だけ効く (ver9 resolve10 §2.4)
# 画面からの流し込み (setCurrentIndex) で役割変更コマンドが飛ばないことを見る。
@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できないためスキップ")
class RoleComboSignalTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    # 役割コンボは既定で出なくなった (ver9 resolve13 §3.6)。この検査は
    # 「コンボを出したときに信号をどう扱うか」を見るものなので、設定で出して試す。
    def _panel(self, role_control=True):
        timeline, _, _ = _moved_to_new_track()
        settings = {"timeline": {"subtitle_tracks": {
            "role_control_enabled": bool(role_control)}}}
        controller = TimelineController(timeline, settings)
        panel = ted._InspectorPanel(controller)      # noqa: SLF001 (単体テスト)
        controller.selection_changed.connect(panel.show_clip)
        controller.timeline_changed.connect(panel.refresh)
        controller.select(["s1"])
        return timeline, controller, panel

    # setCurrentIndex (ホイール・矢印キー相当) では移動も役割変更も起きない
    def test_set_current_index_does_not_change_role(self):
        timeline, _controller, panel = self._panel()
        panel.role_combo.setCurrentIndex(panel.role_combo.findData("sub"))
        self.assertEqual(timeline.track_of_clip("s1").id, "S3")
        self.assertEqual(timeline.clip_by_id("s1").role, "streamer")

    # 文字色・フォントの変更ではトラックが動かない (報告②の確認)
    def test_color_and_font_keep_track(self):
        timeline, _controller, panel = self._panel()
        panel._on_color_changed("&H000000FF")        # noqa: SLF001
        panel.font_combo.setCurrentIndex(1)
        panel._on_font_changed()                     # noqa: SLF001
        self.assertEqual(timeline.track_of_clip("s1").id, "S3")
        self.assertEqual(timeline.clip_by_id("s1").color, "&H000000FF")

    # 既定では役割コンボを出さない (ver9 resolve13 §3.6)。
    # 代わりに「どのトラックに載っているか」を見せる。
    def test_role_combo_is_hidden_by_default(self):
        _timeline, _controller, panel = self._panel(role_control=False)
        self.assertIsNone(panel.role_combo)
        self.assertIsNotNone(panel.track_label)
        self.assertIn("S3", panel.track_label.text())

    # 利用者が選び直したとき (activated) は役割が変わる。ただし居場所は動かない。
    def test_activated_changes_role_only(self):
        timeline, _controller, panel = self._panel()
        index = panel.role_combo.findData("sub")
        panel.role_combo.setCurrentIndex(index)
        panel.role_combo.activated.emit(index)
        self.assertEqual(timeline.clip_by_id("s1").role, "sub")
        self.assertEqual(timeline.track_of_clip("s1").id, "S3")


if __name__ == "__main__":
    unittest.main()
