# テロップトラックの行き来 (ドラッグ＆ドロップ) と削除 (ver9 要望) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・既にあるトラックへ移せること = 毎回トラックを増やさずに済むこと
#   ・役割 (role) が変わらないこと → 見た目は変わらない
#   ・移動先で時間が重なるときは空きへ寄せること (テロップが消えない)
#   ・コメント用トラック (S2) と通常テロップが混ざらないこと
#   ・削除できるのは増やしたトラック (S3 以降) だけで、S1/S2 は守られること
#   ・テロップごと削除しても Undo 1 手で戻ること
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.settings import settings_window as sw
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


# S1 / S2 / S3 を持つ Timeline (S3 は「増やしたトラック」)
def _timeline(with_comment=True, with_extra=True):
    timeline = Timeline(fps=60, width=1920, height=1080)
    timeline.tracks = [
        Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, name="Video 1", is_base=True),
        Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1",
              clips=[SubtitleClip("s1", 1.0, 2.0, text="テロップ1")]),
    ]
    if with_comment:
        timeline.tracks.append(
            Track(COMMENT_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 2, name="Comment"))
    if with_extra:
        timeline.tracks.append(
            Track("S3", TRACK_SUBTITLE, 3, name="Subtitle 3"))
    return timeline


def _track_of(timeline, clip_id):
    track = timeline.track_of_clip(clip_id)
    return None if track is None else track.id


class MoveSubtitleToTrackTest(unittest.TestCase):

    # S1 → S3 へ移せる (新しいトラックを作らない)
    def test_move_to_existing_track(self):
        timeline = _timeline()
        before = len(timeline.subtitle_tracks())
        command = commands.MoveSubtitleToTrack("s1", "S3")
        self.assertTrue(command.apply(timeline))
        self.assertEqual(_track_of(timeline, "s1"), "S3")
        self.assertEqual(len(timeline.subtitle_tracks()), before)

    # S3 → S1 へ戻せる (行き来できる)
    def test_move_back(self):
        timeline = _timeline()
        commands.MoveSubtitleToTrack("s1", "S3").apply(timeline)
        self.assertTrue(
            commands.MoveSubtitleToTrack("s1", BASE_SUBTITLE_TRACK_ID).apply(timeline))
        self.assertEqual(_track_of(timeline, "s1"), BASE_SUBTITLE_TRACK_ID)

    # 役割は変えない = アイコンも背景も付かない (ver9 resolve2 §1.1)
    def test_role_is_kept(self):
        timeline = _timeline()
        commands.MoveSubtitleToTrack("s1", "S3").apply(timeline)
        self.assertEqual(timeline.clip_by_id("s1").role, "streamer")

    # 時刻も一緒に変えられる (斜めのドラッグ)
    def test_move_with_new_start(self):
        timeline = _timeline()
        commands.MoveSubtitleToTrack("s1", "S3", new_start=5.0).apply(timeline)
        clip = timeline.clip_by_id("s1")
        self.assertEqual(_track_of(timeline, "s1"), "S3")
        self.assertAlmostEqual(clip.timeline_start, 5.0)

    # 移動先が埋まっていたら空きへ寄せる (テロップを消さない / 重ねない)
    def test_overlap_is_pushed_to_free_space(self):
        timeline = _timeline()
        timeline.track_by_id("S3").clips.append(
            SubtitleClip("s9", 4.0, 2.0, text="先客"))
        command = commands.MoveSubtitleToTrack("s1", "S3", new_start=4.5)
        self.assertTrue(command.apply(timeline))
        clip = timeline.clip_by_id("s1")
        self.assertEqual(_track_of(timeline, "s1"), "S3")
        self.assertEqual(len(timeline.track_by_id("S3").clips), 2)
        self.assertGreaterEqual(clip.timeline_start, 6.0 - 1e-6)

    # 同じトラックへの移動は何もしない (履歴を汚さない)
    def test_same_track_is_noop(self):
        timeline = _timeline()
        self.assertFalse(
            commands.MoveSubtitleToTrack("s1", BASE_SUBTITLE_TRACK_ID).apply(timeline))

    # ロック中のトラックへは移せない
    def test_locked_track_is_refused(self):
        timeline = _timeline()
        timeline.track_by_id("S3").locked = True
        self.assertFalse(commands.MoveSubtitleToTrack("s1", "S3").apply(timeline))
        self.assertEqual(_track_of(timeline, "s1"), BASE_SUBTITLE_TRACK_ID)

    # 映像トラックへは移せない
    def test_video_track_is_refused(self):
        timeline = _timeline()
        self.assertFalse(
            commands.MoveSubtitleToTrack("s1", BASE_VIDEO_TRACK_ID).apply(timeline))

    # Undo 1 手で元のトラックへ戻る
    def test_undo(self):
        timeline = _timeline()
        stack = commands.CommandStack()
        self.assertTrue(stack.push(timeline, commands.MoveSubtitleToTrack("s1", "S3")))
        stack.undo(timeline)
        self.assertEqual(_track_of(timeline, "s1"), BASE_SUBTITLE_TRACK_ID)


class RemoveSubtitleTrackTest(unittest.TestCase):

    # 増やしたトラック (S3) は削除できる
    def test_remove_extra_track(self):
        timeline = _timeline()
        command = commands.RemoveSubtitleTrack("S3", _cfg())
        self.assertTrue(command.apply(timeline))
        self.assertIsNone(timeline.track_by_id("S3"))
        self.assertEqual(command.removed_clips, 0)

    # 載っているテロップも一緒に消え、件数が分かる (確認ダイアログの文言用)
    def test_remove_with_clips(self):
        timeline = _timeline()
        timeline.track_by_id("S3").clips.extend([
            SubtitleClip("s8", 1.0, 1.0, text="あ"),
            SubtitleClip("s9", 3.0, 1.0, text="い"),
        ])
        command = commands.RemoveSubtitleTrack("S3", _cfg())
        self.assertTrue(command.apply(timeline))
        self.assertEqual(command.removed_clips, 2)
        self.assertIsNone(timeline.clip_by_id("s8"))

    # 既定トラック (S1) は消せない = テロップの置き場が無くならない
    def test_base_track_is_protected(self):
        timeline = _timeline()
        self.assertFalse(
            commands.RemoveSubtitleTrack(BASE_SUBTITLE_TRACK_ID, _cfg()).apply(timeline))
        self.assertIsNotNone(timeline.track_by_id(BASE_SUBTITLE_TRACK_ID))

    # コメント用トラック (S2) も消せない = 役割変更の移動先が無くならない
    def test_comment_track_is_protected(self):
        timeline = _timeline()
        self.assertFalse(
            commands.RemoveSubtitleTrack(COMMENT_SUBTITLE_TRACK_ID,
                                         _cfg()).apply(timeline))
        self.assertIsNotNone(timeline.track_by_id(COMMENT_SUBTITLE_TRACK_ID))

    # 映像トラックは対象外
    def test_video_track_is_refused(self):
        timeline = _timeline()
        self.assertFalse(
            commands.RemoveSubtitleTrack(BASE_VIDEO_TRACK_ID, _cfg()).apply(timeline))

    # S1 が無い (S3 が最小) ときは S3 が「既定トラック」なので消せない
    def test_last_track_is_protected(self):
        timeline = _timeline(with_comment=False)
        timeline.tracks = [t for t in timeline.tracks
                           if t.id != BASE_SUBTITLE_TRACK_ID]
        self.assertFalse(commands.RemoveSubtitleTrack("S3", _cfg()).apply(timeline))

    # Undo 1 手でトラックとテロップが丸ごと戻る
    def test_undo_restores_track_and_clips(self):
        timeline = _timeline()
        timeline.track_by_id("S3").clips.append(
            SubtitleClip("s9", 1.0, 1.0, text="あ"))
        stack = commands.CommandStack()
        self.assertTrue(stack.push(timeline, commands.RemoveSubtitleTrack("S3", _cfg())))
        stack.undo(timeline)
        self.assertIsNotNone(timeline.track_by_id("S3"))
        self.assertIsNotNone(timeline.clip_by_id("s9"))

    # 削除可否の判定 (UI のメニュー出し分けと同じ関数)
    def test_is_removable(self):
        timeline = _timeline()
        cfg = _cfg()
        self.assertTrue(commands.is_removable_subtitle_track(
            timeline, timeline.track_by_id("S3"), cfg))
        for track_id in (BASE_SUBTITLE_TRACK_ID, COMMENT_SUBTITLE_TRACK_ID,
                         BASE_VIDEO_TRACK_ID):
            self.assertFalse(commands.is_removable_subtitle_track(
                timeline, timeline.track_by_id(track_id), cfg), track_id)
        self.assertFalse(commands.is_removable_subtitle_track(timeline, None, cfg))


class TrackSettingTest(unittest.TestCase):

    def test_defaults(self):
        cfg = timeline_config({})["subtitle_tracks"]
        self.assertTrue(cfg["drag_between_tracks"])
        self.assertTrue(cfg["remove_track_enabled"])

    # 更新インストール (旧 setting.json が復元される) でも既定が届く
    def test_merge_with_defaults_fills_keys(self):
        merged = sw._merge_with_defaults(
            {"timeline": {"subtitle_tracks": {"role_track_enabled": True}}})
        sw._normalize_legacy_values(merged)
        tracks = merged["timeline"]["subtitle_tracks"]
        for key in ("drag_between_tracks", "remove_track_enabled"):
            self.assertIn(key, tracks)
            self.assertTrue(tracks[key])


class ViewDropRuleTest(unittest.TestCase):

    # ドロップを受けられるトラックの判定 (ビューの出し分け)
    def _view(self, settings=None):
        from PySide6.QtWidgets import QApplication

        from src.gui.timeline.timeline_controller import TimelineController
        from src.gui.timeline.timeline_view import TimelineView

        self.app = QApplication.instance() or QApplication([])
        timeline = _timeline()
        controller = TimelineController(timeline, settings or dict(sw.DEFAULT_SETTINGS))
        return controller, TimelineView(controller)

    def test_normal_subtitle_can_drop_on_s1_s3_only(self):
        controller, view = self._view()
        timeline = controller.timeline
        clip = timeline.clip_by_id("s1")
        self.assertTrue(view._can_drop_subtitle(
            clip, timeline.track_by_id("S3")))
        self.assertTrue(view._can_drop_subtitle(
            clip, timeline.track_by_id(BASE_SUBTITLE_TRACK_ID)))
        # コメント用トラックは受けない (そこで足す字幕はコメントになる規約のため)
        self.assertFalse(view._can_drop_subtitle(
            clip, timeline.track_by_id(COMMENT_SUBTITLE_TRACK_ID)))
        # 映像トラックも受けない
        self.assertFalse(view._can_drop_subtitle(
            clip, timeline.track_by_id(BASE_VIDEO_TRACK_ID)))

    def test_comment_clip_stays_on_comment_track(self):
        controller, view = self._view()
        timeline = controller.timeline
        clip = timeline.clip_by_id("s1")
        clip.role = ROLE_COMMENT
        self.assertTrue(view._can_drop_subtitle(
            clip, timeline.track_by_id(COMMENT_SUBTITLE_TRACK_ID)))
        self.assertFalse(view._can_drop_subtitle(
            clip, timeline.track_by_id("S3")))

    def test_disabled_by_setting(self):
        import copy

        settings = copy.deepcopy(sw.DEFAULT_SETTINGS)
        settings["timeline"]["subtitle_tracks"]["drag_between_tracks"] = False
        controller, view = self._view(settings)
        timeline = controller.timeline
        self.assertFalse(view._can_drop_subtitle(
            timeline.clip_by_id("s1"), timeline.track_by_id("S3")))


class DragAndDropTest(unittest.TestCase):

    # 実際のマウス操作 (押す → 下の行まで動かす → 離す) を再現する
    def _drag(self, from_pos, to_pos, settings=None):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication

        from src.gui.timeline.timeline_controller import TimelineController
        from src.gui.timeline.timeline_view import TimelineView, build_rows

        self.app = QApplication.instance() or QApplication([])
        controller = TimelineController(_timeline(),
                                        settings or dict(sw.DEFAULT_SETTINGS))
        view = TimelineView(controller)
        view.resize(800, 400)

        def event(kind, pos):
            point = QPointF(*pos)
            # 画面座標も渡す形にする (localPos だけの形は非推奨)
            return QMouseEvent(kind, point, point, Qt.LeftButton, Qt.LeftButton,
                               Qt.NoModifier)

        view.mousePressEvent(event(QEvent.MouseButtonPress, from_pos))
        view.mouseMoveEvent(event(QEvent.MouseMove, to_pos))
        view.mouseReleaseEvent(event(QEvent.MouseButtonRelease, to_pos))
        rows = {row["track"].id: row for row in build_rows(controller.timeline,
                                                           controller.cfg)}
        return controller, rows

    # 行の中心 y を返す (S1 → S2 → S3 の順に並ぶ)
    def _row_y(self, rows, track_id):
        row = rows[track_id]
        return row["y"] + row["height"] / 2

    # S1 のテロップを S3 の行へドラッグすると S3 へ移る
    def test_drag_to_s3(self):
        # 先に行の位置を知るため 1 度組んでから本番のドラッグを行う
        controller, rows = self._drag((60.0, 5.0), (60.0, 5.0))
        y1 = self._row_y(rows, BASE_SUBTITLE_TRACK_ID)
        y3 = self._row_y(rows, "S3")
        controller, _rows = self._drag((60.0, y1), (60.0, y3))
        self.assertEqual(_track_of(controller.timeline, "s1"), "S3")
        # 役割は変わらない (見た目は不変)
        self.assertEqual(controller.timeline.clip_by_id("s1").role, "streamer")

    # S3 から S1 へ戻せる (行き来できる)
    def test_drag_back_to_s1(self):
        controller, rows = self._drag((60.0, 5.0), (60.0, 5.0))
        y1 = self._row_y(rows, BASE_SUBTITLE_TRACK_ID)
        y3 = self._row_y(rows, "S3")
        controller, _rows = self._drag((60.0, y1), (60.0, y3))
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        from src.gui.timeline.timeline_view import TimelineView

        view = TimelineView(controller)
        view.resize(800, 400)

        def event(kind, y):
            point = QPointF(60.0, y)
            return QMouseEvent(kind, point, point, Qt.LeftButton,
                               Qt.LeftButton, Qt.NoModifier)

        view.mousePressEvent(event(QEvent.MouseButtonPress, y3))
        view.mouseMoveEvent(event(QEvent.MouseMove, y1))
        view.mouseReleaseEvent(event(QEvent.MouseButtonRelease, y1))
        self.assertEqual(_track_of(controller.timeline, "s1"),
                         BASE_SUBTITLE_TRACK_ID)

    # 同じ行の中で横へ動かしただけならトラックは変わらない (従来の移動)
    def test_horizontal_drag_keeps_track(self):
        controller, rows = self._drag((60.0, 5.0), (60.0, 5.0))
        y1 = self._row_y(rows, BASE_SUBTITLE_TRACK_ID)
        controller, _rows = self._drag((60.0, y1), (200.0, y1))
        self.assertEqual(_track_of(controller.timeline, "s1"),
                         BASE_SUBTITLE_TRACK_ID)
        self.assertGreater(controller.timeline.clip_by_id("s1").timeline_start, 1.0)

    # コメント用トラック (S2) の行へ落としても移らない
    def test_drag_to_comment_track_is_refused(self):
        controller, rows = self._drag((60.0, 5.0), (60.0, 5.0))
        y1 = self._row_y(rows, BASE_SUBTITLE_TRACK_ID)
        y2 = self._row_y(rows, COMMENT_SUBTITLE_TRACK_ID)
        controller, _rows = self._drag((60.0, y1), (60.0, y2))
        self.assertEqual(_track_of(controller.timeline, "s1"),
                         BASE_SUBTITLE_TRACK_ID)


class ControllerTrackTest(unittest.TestCase):

    def _controller(self, settings=None):
        from PySide6.QtWidgets import QApplication

        from src.gui.timeline.timeline_controller import TimelineController

        self.app = QApplication.instance() or QApplication([])
        return TimelineController(_timeline(), settings or dict(sw.DEFAULT_SETTINGS))

    def test_move_and_remove_via_controller(self):
        controller = self._controller()
        self.assertTrue(controller.move_subtitle_to_track("s1", "S3", 4.0))
        self.assertEqual(_track_of(controller.timeline, "s1"), "S3")
        result = controller.remove_subtitle_track("S3")
        self.assertTrue(result["changed"])
        self.assertEqual(result["clips"], 1)
        controller.undo()
        self.assertIsNotNone(controller.timeline.track_by_id("S3"))

    def test_can_remove_reflects_setting(self):
        import copy

        controller = self._controller()
        self.assertTrue(controller.can_remove_subtitle_track("S3"))
        self.assertFalse(controller.can_remove_subtitle_track(BASE_SUBTITLE_TRACK_ID))
        settings = copy.deepcopy(sw.DEFAULT_SETTINGS)
        settings["timeline"]["subtitle_tracks"]["remove_track_enabled"] = False
        controller = self._controller(settings)
        self.assertFalse(controller.can_remove_subtitle_track("S3"))


if __name__ == "__main__":
    unittest.main()
