# 右クリックから足す通常テロップ用トラックの単体テスト (ver9 resolve2 §9.1)
# 実行: python -m unittest discover -s tests
# 重点:
#   ・S1 だけの状態でも S2 を奪わず S3 から始まること (要望「(追加)」/ §R4)
#   ・役割 (role) が変わらないこと = アイコンも背景も付かないこと (§R7)
#   ・1 本も移せないときに空トラックが残らないこと (§D-7)
#   ・通常テロップ用トラックがコメント用と誤認されないこと (§4.1-b)
import os
import tempfile
import unittest

from src.modules import subtitle_generator
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
    Transform,
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


def _subtitle_ids(timeline):
    return [t.id for t in sorted(timeline.subtitle_tracks(), key=lambda t: t.index)]


class MoveToNewTrackTest(unittest.TestCase):

    # T1: S1 だけの状態では S2 ではなく S3 (index 3) が生え、そこへ移る
    def test_starts_at_third_track(self):
        timeline = _timeline()
        command = commands.MoveSubtitlesToNewTrack(["s1"], _cfg())
        self.assertTrue(commands.CommandStack().push(timeline, command))
        self.assertEqual(command.created_track_id, "S3")
        self.assertEqual(command.moved, 1)
        track = timeline.track_of_clip("s1")
        self.assertEqual(track.id, "S3")
        self.assertEqual(track.index, 3)
        # コメント用の枠 (S2) は奪わない = 欠番のまま予約される
        self.assertIsNone(timeline.track_by_id(COMMENT_SUBTITLE_TRACK_ID))
        self.assertEqual(_subtitle_ids(timeline), ["S1", "S3"])

    # T2: 役割も本文も個別指定も変わらない (= アイコン・背景が付かない根拠)
    def test_keeps_role_and_fields(self):
        clip = SubtitleClip("s1", 1.0, 2.0, text="字幕", role="streamer",
                            color="#123456", font="Meiryo", font_size=60,
                            transform=Transform(x=0.25, y=0.75))
        timeline = _timeline(clips=[clip])
        commands.CommandStack().push(
            timeline, commands.MoveSubtitlesToNewTrack(["s1"], _cfg()))
        moved = timeline.clip_by_id("s1")
        self.assertEqual(moved.id, "s1")             # ID は変えない
        self.assertEqual(moved.role, "streamer")     # 役割は変えない
        self.assertEqual(moved.text, "字幕")
        self.assertEqual(moved.color, "#123456")
        self.assertEqual(moved.font, "Meiryo")
        self.assertEqual(moved.font_size, 60)
        self.assertEqual((moved.transform.x, moved.transform.y), (0.25, 0.75))

    # T3: Undo 1 手でトラックの新設とクリップの移動が同時に戻る
    def test_undo_restores_track_and_clip(self):
        timeline = _timeline()
        stack = commands.CommandStack()
        stack.push(timeline, commands.MoveSubtitlesToNewTrack(["s1"], _cfg()))
        stack.undo(timeline)
        self.assertEqual(_subtitle_ids(timeline), ["S1"])
        self.assertEqual(timeline.track_of_clip("s1").id, BASE_SUBTITLE_TRACK_ID)

    # T4: コメント用 S2 がある状態でも S3 になり、行順は S1 → S2 → S3
    def test_after_comment_track(self):
        timeline = _timeline()
        timeline.tracks.append(Track(
            COMMENT_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 2, name="Comment",
            clips=[SubtitleClip("s9", 5.0, 1.0, text="コメント", role="comment")]))
        command = commands.MoveSubtitlesToNewTrack(["s1"], _cfg())
        commands.CommandStack().push(timeline, command)
        self.assertEqual(command.created_track_id, "S3")
        self.assertEqual(_subtitle_ids(timeline), ["S1", "S2", "S3"])

    # T5: 上限に達していたらトラックは増えない
    def test_limit(self):
        timeline = _timeline()
        for index in (3, 4):
            timeline.tracks.append(
                Track(f"S{index}", TRACK_SUBTITLE, index, name=f"Subtitle {index}"))
        command = commands.MoveSubtitlesToNewTrack(["s1"], _cfg(max_tracks=3))
        self.assertFalse(commands.CommandStack().push(timeline, command))
        self.assertTrue(command.limit_reached)
        self.assertEqual(_subtitle_ids(timeline), ["S1", "S3", "S4"])
        self.assertEqual(timeline.track_of_clip("s1").id, BASE_SUBTITLE_TRACK_ID)

    # T6: ロックされたトラックのクリップは移らず、Timeline も変わらない
    def test_locked_source(self):
        timeline = _timeline()
        timeline.track_by_id(BASE_SUBTITLE_TRACK_ID).locked = True
        command = commands.MoveSubtitlesToNewTrack(["s1"], _cfg())
        self.assertFalse(commands.CommandStack().push(timeline, command))
        self.assertEqual(_subtitle_ids(timeline), ["S1"])
        self.assertEqual(command.skipped, 1)

    # T7: 互いに重なる 2 件は早い 1 件だけ移り、もう 1 件は元トラックに残る
    def test_overlapping_selection(self):
        timeline = _timeline(clips=[
            SubtitleClip("s1", 1.0, 2.0, text="前"),
            SubtitleClip("s2", 2.0, 2.0, text="後"),
        ])
        command = commands.MoveSubtitlesToNewTrack(["s1", "s2"], _cfg())
        self.assertTrue(commands.CommandStack().push(timeline, command))
        self.assertEqual(command.moved, 1)
        self.assertEqual(command.skipped, 1)
        self.assertEqual(timeline.track_of_clip("s1").id, "S3")
        self.assertEqual(timeline.track_of_clip("s2").id, BASE_SUBTITLE_TRACK_ID)

    # T8: 1 件も移せないときは空トラックが残らない
    def test_no_empty_track_left(self):
        timeline = _timeline()
        command = commands.MoveSubtitlesToNewTrack(["missing"], _cfg())
        self.assertFalse(commands.CommandStack().push(timeline, command))
        self.assertEqual(_subtitle_ids(timeline), ["S1"])
        self.assertIsNone(command.created_track_id)

    # 字幕以外の ID が混ざっても読み飛ばす
    def test_ignores_non_subtitle_ids(self):
        timeline = _timeline()
        command = commands.MoveSubtitlesToNewTrack([BASE_VIDEO_TRACK_ID, "s1"], _cfg())
        self.assertTrue(commands.CommandStack().push(timeline, command))
        self.assertEqual(command.moved, 1)


class TrackIdAllocationTest(unittest.TestCase):

    # T9: 既存 ID と衝突しない
    def test_skips_used_ids(self):
        timeline = _timeline()
        timeline.tracks.append(
            Track("S3", TRACK_SUBTITLE, 1, name="Subtitle 3"))
        track_id, index = timeline.next_subtitle_track_id(
            prefix="S", min_index=3, reserved_indexes=(2,), reserved_ids=("S2",))
        self.assertEqual((track_id, index), ("S4", 4))

    # T9b: S1 + S3 の状態では S4 になり、S2 は欠番のまま残る
    def test_keeps_reserving_second_slot(self):
        timeline = _timeline()
        timeline.tracks.append(
            Track("S3", TRACK_SUBTITLE, 3, name="Subtitle 3",
                  clips=[SubtitleClip("s9", 9.0, 1.0, text="既存")]))
        command = commands.MoveSubtitlesToNewTrack(["s1"], _cfg())
        commands.CommandStack().push(timeline, command)
        self.assertEqual(command.created_track_id, "S4")
        self.assertIsNone(timeline.track_by_id(COMMENT_SUBTITLE_TRACK_ID))

    # T9c: そのあとコメント化すると S2 が生え、2 行目に入る
    def test_comment_track_still_takes_second_row(self):
        timeline = _timeline(clips=[
            SubtitleClip("s1", 1.0, 2.0, text="字幕"),
            SubtitleClip("s2", 10.0, 2.0, text="コメントにする"),
        ])
        stack = commands.CommandStack()
        stack.push(timeline, commands.MoveSubtitlesToNewTrack(["s1"], _cfg()))
        stack.push(timeline, commands.ChangeSubtitleRole("s2", "comment", _cfg()))
        self.assertEqual(timeline.track_of_clip("s2").id, COMMENT_SUBTITLE_TRACK_ID)
        self.assertEqual(_subtitle_ids(timeline), ["S1", "S2", "S3"])

    # T9d: コメント用の ID / 番号を設定で変えてもその枠を飛ばす
    def test_respects_configured_comment_slot(self):
        timeline = _timeline()
        cfg = _cfg(comment_track_id="S5", comment_track_index=5,
                   new_track_min_index=5)
        command = commands.MoveSubtitlesToNewTrack(["s1"], cfg)
        commands.CommandStack().push(timeline, command)
        self.assertEqual(command.created_track_id, "S6")

    # トラック名の書式が壊れていても落ちない (ID を名前に使う)
    def test_broken_name_format(self):
        timeline = _timeline()
        command = commands.MoveSubtitlesToNewTrack(
            ["s1"], _cfg(new_track_name_format="Subtitle {nope}"))
        self.assertTrue(commands.CommandStack().push(timeline, command))
        self.assertEqual(timeline.track_by_id("S3").name, "S3")


class RoleTrackLookupTest(unittest.TestCase):

    # T11: 通常テロップの入ったトラックはコメント用として拾わない
    def test_plain_track_is_not_treated_as_comment(self):
        timeline = _timeline()
        timeline.tracks.append(Track(
            "S3", TRACK_SUBTITLE, 3, name="Subtitle 3",
            clips=[SubtitleClip("s9", 5.0, 1.0, text="通常", role="streamer")]))
        self.assertIsNone(timeline.subtitle_track_for_role("comment"))
        # 役割をコメントにすると S3 ではなく S2 が作られる
        commands.CommandStack().push(
            timeline, commands.ChangeSubtitleRole("s1", "comment", _cfg()))
        self.assertEqual(timeline.track_of_clip("s1").id,
                         COMMENT_SUBTITLE_TRACK_ID)

    # T12: 中身が全部コメントのトラックは従来どおり拾う (ID 変更時の救済)
    def test_comment_only_track_is_still_found(self):
        timeline = _timeline()
        timeline.tracks.append(Track(
            "S9", TRACK_SUBTITLE, 9, name="Comment",
            clips=[SubtitleClip("s9", 5.0, 1.0, text="コメント", role="comment")]))
        self.assertEqual(timeline.subtitle_track_for_role("comment").id, "S9")

    # 空トラックはコメント用として拾わない
    def test_empty_track_is_not_found(self):
        timeline = _timeline()
        timeline.tracks.append(Track("S3", TRACK_SUBTITLE, 3, name="Subtitle 3"))
        self.assertIsNone(timeline.subtitle_track_for_role("comment"))


class PersistenceAndOutputTest(unittest.TestCase):

    # T10: 保存 → 読み込みで S3 が往復し、S1 と重なっても押し出されない
    def test_round_trip_and_overlap(self):
        timeline = _timeline()
        timeline.tracks.append(Track(
            "S3", TRACK_SUBTITLE, 3, name="Subtitle 3",
            clips=[SubtitleClip("s2", 1.0, 2.0, text="同時刻")]))
        project_io.validate(timeline)
        self.assertEqual(timeline.clip_by_id("s1").timeline_start, 1.0)
        self.assertEqual(timeline.clip_by_id("s2").timeline_start, 1.0)
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "sample.timeline.json")
            project_io.save(timeline, path)
            loaded = project_io.load(path, validate_timeline=False)
        track = loaded.track_by_id("S3")
        self.assertIsNotNone(track)
        self.assertEqual(track.name, "Subtitle 3")
        self.assertEqual(track.clips[0].role, "streamer")

    # 別トラックへ移しても背景行 (コメント装飾) は 1 行も出ない (§9.3)
    def test_ass_has_no_comment_background(self):
        subtitle_cfg = {
            "font_family": "Yu Gothic UI", "font_size": 48,
            "own_subtitle_color": "#FFFFFF", "comment_bg_enabled": True,
            "comment_bg_color": "&H80000000", "comment_bg_layer": 0,
            "comment_text_layer": 1,
        }
        profile = subtitle_generator.build_font_profile(subtitle_cfg)
        items = [
            {"start": 1.0, "end": 3.0, "text": "本編", "role": "streamer"},
            {"start": 1.0, "end": 3.0, "text": "別トラック", "role": "streamer"},
        ]
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "out.ass")
            subtitle_generator.build_subtitle_file(
                items, profile, path, subtitle_cfg=subtitle_cfg)
            with open(path, encoding="utf-8") as f:
                body = f.read()
        dialogues = [l for l in body.splitlines() if l.startswith("Dialogue:")]
        self.assertEqual(len(dialogues), 2)          # 背景行が増えていない
        self.assertNotIn("\\p1", body)               # 角丸の箱 (描画命令) が無い
        for line in dialogues:
            self.assertTrue(line.startswith("Dialogue: 0,"))
            self.assertIn("Streamer", line)


if __name__ == "__main__":
    unittest.main()
