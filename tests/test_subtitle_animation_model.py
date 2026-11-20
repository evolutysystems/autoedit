# テロップアニメーションのデータ (docs/request/ver12/resolve2.md §4 / §11 / resolve2-add.md §4) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・新しい 4 項目 (animation / fade_in / fade_out / emphasis) が往復すること
#   ・**指定が無いときはキーを書かない** (旧プロジェクトとの差分を増やさない)
#   ・旧プロジェクト (キーが無いファイル) を読んでも壊れないこと
#   ・不正な種類と廃止した種類 (word_pop など) は「なし」へ倒れること (画面を壊さない)
#   ・to_item() が焼き込みへ渡す形になること
#   ・コピー・コマンド (EditSubtitles) と Undo が新項目を扱えること
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.timeline import commands
from src.timeline.model import (
    ANIM_BLACK_BLUR_ZOOM_OUT,
    ANIM_NONE,
    ANIM_POYON,
    ANIM_SHAKE,
    ANIM_SHOCK1,
    SUBTITLE_ANIMATIONS,
    SubtitleClip,
    TRACK_SUBTITLE,
    Timeline,
    Track,
)


def _clip(**fields):
    clip = SubtitleClip("s1", 1.0, 2.0, text="今日はとても良い天気ですね")
    for key, value in fields.items():
        setattr(clip, key, value)
    return clip


class DefaultsTest(unittest.TestCase):

    def test_defaults_are_off(self):
        clip = _clip()
        self.assertEqual(clip.animation, ANIM_NONE)
        self.assertFalse(clip.fade_in)
        self.assertFalse(clip.fade_out)
        self.assertEqual(clip.emphasis, "")
        self.assertFalse(clip.has_animation())

    # 種類は 12 種で、焼き込み側 (subtitle_animation) と同じ値・同じ並び
    def test_known_kinds(self):
        from src.modules import subtitle_animation

        self.assertEqual(len(SUBTITLE_ANIMATIONS), 12)
        self.assertEqual(SUBTITLE_ANIMATIONS, subtitle_animation.ANIMATIONS)

    # 不正な種類は「なし」へ倒す (EditSubtitles の setattr から直接入るため)
    def test_unknown_kind_falls_back_to_none(self):
        self.assertEqual(_clip(animation="spin").animation, ANIM_NONE)
        self.assertEqual(_clip(animation=None).animation, ANIM_NONE)
        self.assertEqual(_clip(animation="SHAKE").animation, ANIM_SHAKE)

    # 廃止した種類 (resolve2-add §1 / §13) を持つ旧プロジェクトは「なし」で開く
    def test_removed_kinds_fall_back_to_none(self):
        for old in ("word_pop", "bounce", "big_word", "clip_in", "glow_wipe", "wave",
                    "ez_from_right", "ez_from_left"):
            data = _clip().to_dict()
            data["animation"] = old
            self.assertEqual(SubtitleClip.from_dict(data).animation, ANIM_NONE)

    def test_has_animation_includes_fades(self):
        self.assertTrue(_clip(fade_in=True).has_animation())
        self.assertTrue(_clip(fade_out=True).has_animation())
        self.assertTrue(_clip(animation=ANIM_SHOCK1).has_animation())

    # 強調する語は読点・カンマ・空白のどれでも区切れる
    def test_emphasis_words(self):
        clip = _clip(emphasis="天気、とても　今日は,良い")
        self.assertEqual(clip.emphasis_words(),
                         ["天気", "とても", "今日は", "良い"])


class RoundTripTest(unittest.TestCase):

    # 指定が無いときはキーを書かない (§11)
    def test_plain_clip_has_no_animation_keys(self):
        data = _clip().to_dict()
        for key in ("animation", "fade_in", "fade_out", "emphasis"):
            self.assertNotIn(key, data)

    def test_round_trip(self):
        clip = _clip(animation=ANIM_BLACK_BLUR_ZOOM_OUT, fade_in=True, fade_out=True,
                     emphasis="天気")
        restored = SubtitleClip.from_dict(clip.to_dict())

        self.assertEqual(restored.animation, ANIM_BLACK_BLUR_ZOOM_OUT)
        self.assertTrue(restored.fade_in)
        self.assertTrue(restored.fade_out)
        self.assertEqual(restored.emphasis, "天気")

    # 旧プロジェクト (キーが無い) を読んでも落ちない
    def test_old_project_is_readable(self):
        data = _clip().to_dict()
        restored = SubtitleClip.from_dict(data)
        self.assertEqual(restored.animation, ANIM_NONE)
        self.assertFalse(restored.has_animation())

    def test_copy_keeps_the_animation(self):
        clip = _clip(animation=ANIM_POYON, fade_out=True, emphasis="天気")
        copied = clip.copy()
        self.assertEqual(copied.animation, ANIM_POYON)
        self.assertTrue(copied.fade_out)
        self.assertEqual(copied.emphasis, "天気")


class ToItemTest(unittest.TestCase):

    # 動きを付けていないテロップの item は現行と 1 キーも変わらない (§4)
    def test_plain_item_has_no_animation_keys(self):
        item = _clip().to_item()
        for key in ("animation", "fade_in", "fade_out", "emphasis", "words"):
            self.assertNotIn(key, item)

    def test_item_carries_the_animation(self):
        clip = _clip(animation=ANIM_SHAKE, fade_in=True, emphasis="天気")
        item = clip.to_item()
        self.assertEqual(item["animation"], ANIM_SHAKE)
        self.assertTrue(item["fade_in"])
        self.assertNotIn("fade_out", item)
        self.assertEqual(item["emphasis"], "天気")

    # 単語の時刻は使わなくなった (resolve2-add §1)。動きを付けても item へ載せない
    def test_item_has_no_word_times(self):
        words = [{"start": 1.0, "end": 1.4, "text": "今日は"}]
        clip = _clip(origin={"words": words}, animation=ANIM_SHAKE)
        self.assertNotIn("words", clip.to_item())


class CommandTest(unittest.TestCase):

    def setUp(self):
        self.timeline = Timeline(width=1920, height=1080, fps=30.0)
        track = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1")
        track.clips.append(_clip())
        self.timeline.tracks.append(track)
        self.stack = commands.CommandStack()

    # インスペクタからの適用と Undo (選択中すべてへ 1 手)
    def test_edit_and_undo(self):
        changed = self.stack.push(self.timeline, commands.EditSubtitles(
            ["s1"], animation=ANIM_SHOCK1, fade_in=True, emphasis="天気"))
        self.assertTrue(changed)
        clip = self.timeline.clip_by_id("s1")
        self.assertEqual(clip.animation, ANIM_SHOCK1)
        self.assertTrue(clip.fade_in)
        self.assertEqual(clip.emphasis, "天気")

        self.stack.undo(self.timeline)
        clip = self.timeline.clip_by_id("s1")
        self.assertEqual(clip.animation, ANIM_NONE)
        self.assertFalse(clip.fade_in)
        self.assertEqual(clip.emphasis, "")

    # 同じ値を入れ直したら「変化なし」(履歴を汚さない)
    def test_same_value_is_not_a_change(self):
        self.stack.push(self.timeline, commands.EditSubtitles(
            ["s1"], animation=ANIM_SHOCK1))
        self.assertFalse(commands.EditSubtitles(
            ["s1"], animation=ANIM_SHOCK1).apply(self.timeline))


if __name__ == "__main__":
    unittest.main()
