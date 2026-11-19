# テロップアニメーションのデータ (docs/request/ver12/resolve2.md §4 / §11) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・新しい 4 項目 (animation / fade_in / fade_out / emphasis) が往復すること
#   ・**指定が無いときはキーを書かない** (旧プロジェクトとの差分を増やさない)
#   ・旧プロジェクト (キーが無いファイル) を読んでも壊れないこと
#   ・不正な種類は「なし」へ倒れること (画面を壊さない)
#   ・to_item() が焼き込みへ渡す形 (words つき) になること
#   ・コピー・コマンド (EditSubtitles) と Undo が新項目を扱えること
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.timeline import commands
from src.timeline.model import (
    ANIM_BIG_WORD,
    ANIM_BOUNCE,
    ANIM_NONE,
    ANIM_WORD_POP,
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

    def test_known_kinds(self):
        self.assertEqual(SUBTITLE_ANIMATIONS,
                         (ANIM_WORD_POP, ANIM_BOUNCE, ANIM_BIG_WORD))

    # 不正な種類は「なし」へ倒す (EditSubtitles の setattr から直接入るため)
    def test_unknown_kind_falls_back_to_none(self):
        self.assertEqual(_clip(animation="spin").animation, ANIM_NONE)
        self.assertEqual(_clip(animation=None).animation, ANIM_NONE)
        self.assertEqual(_clip(animation="WORD_POP").animation, ANIM_WORD_POP)

    def test_has_animation_includes_fades(self):
        self.assertTrue(_clip(fade_in=True).has_animation())
        self.assertTrue(_clip(fade_out=True).has_animation())
        self.assertTrue(_clip(animation=ANIM_BOUNCE).has_animation())

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
        clip = _clip(animation=ANIM_WORD_POP, fade_in=True, fade_out=True,
                     emphasis="天気")
        restored = SubtitleClip.from_dict(clip.to_dict())

        self.assertEqual(restored.animation, ANIM_WORD_POP)
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
        clip = _clip(animation=ANIM_BIG_WORD, fade_out=True, emphasis="天気")
        copied = clip.copy()
        self.assertEqual(copied.animation, ANIM_BIG_WORD)
        self.assertTrue(copied.fade_out)
        self.assertEqual(copied.emphasis, "天気")


class ToItemTest(unittest.TestCase):

    # 動きを付けていないテロップの item は現行と 1 キーも変わらない (§4)
    def test_plain_item_has_no_animation_keys(self):
        item = _clip().to_item()
        for key in ("animation", "fade_in", "fade_out", "emphasis", "words"):
            self.assertNotIn(key, item)

    def test_item_carries_the_animation(self):
        clip = _clip(animation=ANIM_WORD_POP, fade_in=True, emphasis="天気")
        item = clip.to_item()
        self.assertEqual(item["animation"], ANIM_WORD_POP)
        self.assertTrue(item["fade_in"])
        self.assertNotIn("fade_out", item)
        self.assertEqual(item["emphasis"], "天気")

    # 単語ごとの動きには単語の時刻が要る (動きを付けたときだけ載せる)
    def test_item_carries_word_times_only_when_animated(self):
        words = [{"start": 1.0, "end": 1.4, "text": "今日は"}]
        clip = _clip(origin={"words": words})
        self.assertNotIn("words", clip.to_item())

        clip.animation = ANIM_WORD_POP
        self.assertEqual(clip.to_item()["words"], words)


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
            ["s1"], animation=ANIM_BOUNCE, fade_in=True, emphasis="天気"))
        self.assertTrue(changed)
        clip = self.timeline.clip_by_id("s1")
        self.assertEqual(clip.animation, ANIM_BOUNCE)
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
            ["s1"], animation=ANIM_BOUNCE))
        self.assertFalse(commands.EditSubtitles(
            ["s1"], animation=ANIM_BOUNCE).apply(self.timeline))


if __name__ == "__main__":
    unittest.main()
