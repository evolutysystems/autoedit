# 編集履歴の「保存点」管理 (src/timeline/commands.py CommandStack) の単体テスト
# 実行: python -m unittest discover -s tests
# ver3 resolve7 §5.6 / §10.1:
#   ・保存 → 変更 で「未保存」になること
#   ・Undo で保存点まで戻ると「保存済み」に戻ること
#   ・保存点より前で枝分かれしたら二度と「保存済み」にならないこと
#   ・履歴上限を超えて古い手が捨てられても誤判定しないこと
import unittest

from src.timeline import commands
from src.timeline.model import (
    AudioClip,
    Clip,
    MediaRef,
    ORIGIN_SILENCE_CUT,
    TRACK_AUDIO,
    TRACK_VIDEO,
    Timeline,
    Track,
)

_MIN = 0.05


# 本編 1 クリップ + オーバーレイ 1 本 (大きさ変更で履歴を積むため)
def _build():
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 30.0, 0.0, 30.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=[
        AudioClip("a1", "c1"),
    ])
    overlay = Track("V2", TRACK_VIDEO, 2, name="Video 2", clips=[
        Clip("o1", "m1", 5.0, 5.0, 0.0, 5.0, z_order=10, origin={"type": "user_media"}),
    ])
    return Timeline(fps=60, source={"media_id": "m1", "duration_sec": 100.0},
                    media_pool=media, tracks=[video, audio, overlay])


class CleanDepthTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build()
        self.stack = commands.CommandStack()

    # 大きさを 1 手変える (毎回違う値にして「変化なし」で弾かれないようにする)
    def _edit(self, scale):
        self.assertTrue(
            self.stack.push(self.timeline, commands.ResizeOverlay("o1", scale)))

    # 開いた直後は保存済み扱い (何も編集していない)
    def test_initial_state_is_clean(self):
        self.assertTrue(self.stack.is_clean())

    # 編集すると未保存になる
    def test_edit_makes_dirty(self):
        self._edit(0.5)
        self.assertFalse(self.stack.is_clean())

    # 保存すると保存済みへ戻る
    def test_mark_clean_after_save(self):
        self._edit(0.5)
        self.stack.mark_clean()
        self.assertTrue(self.stack.is_clean())
        self._edit(0.6)
        self.assertFalse(self.stack.is_clean())

    # Undo で保存点まで戻れば保存済みに戻る (Redo でも一致する)
    def test_undo_back_to_save_point(self):
        self._edit(0.5)
        self.stack.mark_clean()
        self._edit(0.6)
        self.stack.undo(self.timeline)
        self.assertTrue(self.stack.is_clean())
        self.stack.redo(self.timeline)
        self.assertFalse(self.stack.is_clean())

    # 保存点より前で枝分かれしたら、以後どう操作しても保存済みにはならない
    def test_branching_before_save_point_invalidates(self):
        self._edit(0.5)
        self.stack.mark_clean()
        self.stack.undo(self.timeline)      # 保存点より 1 つ手前へ戻る
        self._edit(0.7)                     # ここで枝を捨てた
        self.assertFalse(self.stack.is_clean())
        self.stack.undo(self.timeline)
        self.assertFalse(self.stack.is_clean())

    # 履歴上限を超えて古い手が捨てられても誤判定しない
    def test_history_limit_drops_save_point(self):
        stack = commands.CommandStack(limit=3)
        stack.push(self.timeline, commands.ResizeOverlay("o1", 0.5))
        stack.mark_clean()                  # 深さ 1 が保存点
        for scale in (0.6, 0.7, 0.8, 0.9):  # 上限を超えて積む
            stack.push(self.timeline, commands.ResizeOverlay("o1", scale))
        # 保存点そのものが履歴から消えたため、Undo で戻っても保存済みにはならない
        self.assertFalse(stack.is_clean())
        for _ in range(3):
            stack.undo(self.timeline)
        self.assertFalse(stack.is_clean())

    # 履歴を捨てても「今が保存済みか」は変わらない
    def test_clear_keeps_clean_state(self):
        self._edit(0.5)
        self.stack.mark_clean()
        self.stack.clear()
        self.assertTrue(self.stack.is_clean())

        self._edit(0.6)
        self.stack.clear()
        self.assertFalse(self.stack.is_clean())



# source を書き換えるコマンド (アーカイブ用のテーマ) も未保存判定と Undo に載る
# (ver3 resolve9 §3-3。スナップショットが source を控えているかの確認)
class SourceSnapshotTest(unittest.TestCase):

    def setUp(self):
        from src.archive import timeline_builder as archive_timeline
        self.archive_timeline = archive_timeline
        self.timeline = _build()
        self.timeline.source = dict(self.timeline.source)
        self.timeline.source["archive"] = {
            "vod_path": "vod.mp4", "curve": [],
            "clips": [{"index": 1, "vod_start": 0.0, "vod_end": 30.0, "score": 50.0,
                       "media_id": "m1", "theme": "", "media_role": "normalized"}],
        }
        self.stack = commands.CommandStack()

    def test_theme_change_marks_unsaved_and_undo_restores(self):
        self.stack.mark_clean()
        self.assertTrue(self.stack.is_clean())

        changed = self.stack.push(
            self.timeline, self.archive_timeline.SetArchiveClipTheme(1, "神回"))
        self.assertTrue(changed)
        self.assertFalse(self.stack.is_clean())     # 未保存になる
        self.assertEqual(
            self.archive_timeline.clip_theme(self.timeline, 1), "神回")

        self.stack.undo(self.timeline)
        # Undo で source ごと戻る (スナップショットに source が入っているため)
        self.assertEqual(self.archive_timeline.clip_theme(self.timeline, 1), "")
        self.assertTrue(self.stack.is_clean())      # 保存点へ戻れば保存済み

    def test_unchanged_theme_does_not_dirty_history(self):
        self.stack.mark_clean()
        changed = self.stack.push(
            self.timeline, self.archive_timeline.SetArchiveClipTheme(1, ""))
        self.assertFalse(changed)
        self.assertTrue(self.stack.is_clean())


if __name__ == "__main__":
    unittest.main()
