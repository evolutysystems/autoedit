# コメント専用の字幕トラック (S2) の単体テスト (ver3 resolve11 §10.1)
# 実行: python -m unittest discover -s tests
# 重点:
#   ・役割を「コメント」にすると S2 が生えて移動すること
#   ・移動先が埋まっていたら「役割だけ」変わること (出力は role が決めるため壊れない)
#   ・S1 と S2 で時間が重なっても読み込み時に押し出されないこと (別トラックにする理由)
#   ・設定で従来の 1 トラック運用へ戻せること
import os
import tempfile
import unittest

from src.timeline import commands, project_io
from src.timeline.builder import timeline_config
from src.timeline.model import (
    BASE_SUBTITLE_TRACK_ID,
    BASE_VIDEO_TRACK_ID,
    COMMENT_SUBTITLE_TRACK_ID,
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


# S1 に字幕を 1 件持つ最小の Timeline
def _timeline(clips=None):
    timeline = Timeline(fps=60, width=1920, height=1080)
    timeline.tracks = [
        Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, name="Video 1", is_base=True),
        Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1",
              clips=list(clips or [SubtitleClip("s1", 1.0, 2.0, text="字幕")])),
    ]
    return timeline


class ChangeSubtitleRoleTest(unittest.TestCase):

    # 役割をコメントにすると S2 が生えてそこへ移る
    def test_moves_to_comment_track(self):
        timeline = _timeline()
        stack = commands.CommandStack()
        command = commands.ChangeSubtitleRole("s1", "comment", _cfg())
        self.assertTrue(stack.push(timeline, command))
        self.assertTrue(command.moved)
        self.assertFalse(command.move_blocked)
        track = timeline.track_of_clip("s1")
        self.assertEqual(track.id, COMMENT_SUBTITLE_TRACK_ID)
        self.assertEqual(timeline.clip_by_id("s1").role, "comment")
        # S1 は空になるが残る (行が消えると編集しづらいため)
        self.assertEqual(timeline.track_by_id(BASE_SUBTITLE_TRACK_ID).clips, [])

    # コメントから戻すと S1 へ帰る
    def test_moves_back_to_base_track(self):
        timeline = _timeline()
        stack = commands.CommandStack()
        stack.push(timeline, commands.ChangeSubtitleRole("s1", "comment", _cfg()))
        stack.push(timeline, commands.ChangeSubtitleRole("s1", "streamer", _cfg()))
        self.assertEqual(timeline.track_of_clip("s1").id, BASE_SUBTITLE_TRACK_ID)

    # Undo 1 手で役割もトラックも戻る (スナップショット方式)
    def test_undo_restores_role_and_track(self):
        timeline = _timeline()
        stack = commands.CommandStack()
        stack.push(timeline, commands.ChangeSubtitleRole("s1", "comment", _cfg()))
        stack.undo(timeline)
        self.assertEqual(timeline.clip_by_id("s1").role, "streamer")
        self.assertEqual(timeline.track_of_clip("s1").id, BASE_SUBTITLE_TRACK_ID)

    # 移動先が埋まっていたら役割だけ変える (best-effort / §9.2)
    def test_blocked_when_target_is_occupied(self):
        timeline = _timeline()
        timeline.tracks.append(Track(
            COMMENT_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 2, name="Comment",
            clips=[SubtitleClip("s9", 0.5, 3.0, text="先客", role="comment")]))
        command = commands.ChangeSubtitleRole("s1", "comment", _cfg())
        self.assertTrue(commands.CommandStack().push(timeline, command))
        self.assertFalse(command.moved)
        self.assertTrue(command.move_blocked)
        # 役割は変わっている = 色・配置・アイコン・背景は正しく出る
        self.assertEqual(timeline.clip_by_id("s1").role, "comment")
        self.assertEqual(timeline.track_of_clip("s1").id, BASE_SUBTITLE_TRACK_ID)

    # 時間が重ならなければ既存の S2 へ入る
    def test_moves_into_existing_track_when_free(self):
        timeline = _timeline()
        timeline.tracks.append(Track(
            COMMENT_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 2, name="Comment",
            clips=[SubtitleClip("s9", 10.0, 2.0, text="別の時間", role="comment")]))
        command = commands.ChangeSubtitleRole("s1", "comment", _cfg())
        commands.CommandStack().push(timeline, command)
        self.assertTrue(command.moved)
        self.assertEqual(len(timeline.track_by_id(COMMENT_SUBTITLE_TRACK_ID).clips), 2)

    # 設定を切れば従来どおり 1 トラック運用 (S2 を作らない)
    def test_role_track_disabled(self):
        timeline = _timeline()
        command = commands.ChangeSubtitleRole(
            "s1", "comment", _cfg(role_track_enabled=False))
        commands.CommandStack().push(timeline, command)
        self.assertFalse(command.moved)
        self.assertIsNone(timeline.track_by_id(COMMENT_SUBTITLE_TRACK_ID))
        self.assertEqual(timeline.clip_by_id("s1").role, "comment")

    # 自動移動だけを切ることもできる
    def test_auto_move_disabled(self):
        timeline = _timeline()
        command = commands.ChangeSubtitleRole(
            "s1", "comment", _cfg(auto_move_on_role_change=False))
        commands.CommandStack().push(timeline, command)
        self.assertFalse(command.moved)
        self.assertEqual(timeline.track_of_clip("s1").id, BASE_SUBTITLE_TRACK_ID)


class AddSubtitleRoleTest(unittest.TestCase):

    # 役割を指定して足すと、対応するトラックへ入る (無ければ作る)
    def test_add_comment_creates_track(self):
        timeline = _timeline(clips=[])
        command = commands.AddSubtitleClip(
            5.0, 2.0, text="コメント", role="comment", subtitle_tracks_cfg=_cfg())
        self.assertTrue(commands.CommandStack().push(timeline, command))
        self.assertEqual(timeline.track_of_clip(command.created_clip_id).id,
                         COMMENT_SUBTITLE_TRACK_ID)

    # トラックを明示したときはそちらが優先される
    def test_explicit_track_wins(self):
        timeline = _timeline(clips=[])
        command = commands.AddSubtitleClip(
            5.0, 2.0, text="字幕", role="comment",
            track_id=BASE_SUBTITLE_TRACK_ID, subtitle_tracks_cfg=_cfg())
        commands.CommandStack().push(timeline, command)
        self.assertEqual(timeline.track_of_clip(command.created_clip_id).id,
                         BASE_SUBTITLE_TRACK_ID)


class OverlapAndPersistenceTest(unittest.TestCase):

    # S1 と S2 で時間が重なっても補正されない (= 同時に表示できる)
    def test_cross_track_overlap_is_kept(self):
        timeline = _timeline()
        timeline.tracks.append(Track(
            COMMENT_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 2, name="Comment",
            clips=[SubtitleClip("s2", 1.0, 2.0, text="コメント", role="comment")]))
        project_io.validate(timeline)
        self.assertEqual(timeline.clip_by_id("s1").timeline_start, 1.0)
        self.assertEqual(timeline.clip_by_id("s2").timeline_start, 1.0)

    # 保存 → 読み込みで S2 が往復する (スキーマ変更は不要)
    def test_round_trip(self):
        timeline = _timeline()
        timeline.tracks.append(Track(
            COMMENT_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 2, name="Comment",
            clips=[SubtitleClip("s2", 1.0, 2.0, text="コメント", role="comment")]))
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "sample.timeline.json")
            project_io.save(timeline, path)
            loaded = project_io.load(path, validate_timeline=False)
        track = loaded.track_by_id(COMMENT_SUBTITLE_TRACK_ID)
        self.assertIsNotNone(track)
        self.assertEqual(track.name, "Comment")
        self.assertEqual(track.clips[0].role, "comment")
        # 役割からトラックを引き直せる
        self.assertEqual(loaded.subtitle_track_for_role("comment").id,
                         COMMENT_SUBTITLE_TRACK_ID)
        self.assertEqual(loaded.subtitle_track_for_role("streamer").id,
                         BASE_SUBTITLE_TRACK_ID)


if __name__ == "__main__":
    unittest.main()
