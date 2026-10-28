# テロップを分割したときの本文の扱い (ver9 要望) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・既定では本文を分けずに複製する (「こんにちは」→「こんにちは」「こんにちは」)
#   ・単語列 (origin.words) を持つ認識由来のテロップでも複製されること
#   ・時刻・尺・トラックの扱いは従来どおりであること
#   ・設定 timeline.subtitle_split_text=true で本文も分ける旧挙動へ戻せること
#   ・「ここで分割」(SplitClip) でも規約が同じであること
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.settings import settings_window as sw
from src.timeline import commands
from src.timeline.builder import timeline_config
from src.timeline.model import TRACK_SUBTITLE, SubtitleClip, Timeline, Track

# 認識由来のテロップ (単語の時刻を持つ = 本文を分けられる条件がそろった状態)
_ORIGIN = {"type": "asr", "words": [
    {"start": 10.0, "end": 10.4, "text": "こんに"},
    {"start": 10.4, "end": 10.8, "text": "ちは"},
]}


def _timeline(origin=None):
    timeline = Timeline()
    track = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1")
    timeline.tracks = [track]
    track.clips.append(SubtitleClip("s1", 0.0, 2.0, text="こんにちは",
                                    origin=origin))
    return timeline, track


def _clips(track):
    return sorted(track.clips, key=lambda c: c.timeline_start)


class SplitKeepsTextTest(unittest.TestCase):

    # 既定: 本文は分けずに複製する (要望そのもの)
    def test_default_duplicates_text(self):
        timeline, track = _timeline(_ORIGIN)
        self.assertTrue(commands.SplitSubtitle("s1", 1.0).apply(timeline))
        self.assertEqual([c.text for c in _clips(track)],
                         ["こんにちは", "こんにちは"])

    # 単語列を持たない手入力のテロップでも複製される
    def test_default_duplicates_without_words(self):
        timeline, track = _timeline()
        commands.SplitSubtitle("s1", 1.0).apply(timeline)
        self.assertEqual([c.text for c in _clips(track)],
                         ["こんにちは", "こんにちは"])

    # 時刻・尺の割り方は従来どおり (本文の扱いだけを変えた)
    def test_timing_is_unchanged(self):
        timeline, track = _timeline(_ORIGIN)
        commands.SplitSubtitle("s1", 1.5).apply(timeline)
        head, tail = _clips(track)
        self.assertAlmostEqual(head.timeline_start, 0.0)
        self.assertAlmostEqual(head.duration, 1.5)
        self.assertAlmostEqual(tail.timeline_start, 1.5)
        self.assertAlmostEqual(tail.duration, 0.5)
        self.assertNotEqual(head.id, tail.id)
        self.assertEqual(len(track.clips), 2)

    # 複製なので単語列も両方へ残る (次に設定を戻しても語境界で割れる)
    def test_default_keeps_words_on_both(self):
        timeline, track = _timeline(_ORIGIN)
        commands.SplitSubtitle("s1", 1.0).apply(timeline)
        for clip in _clips(track):
            self.assertEqual(len(clip.origin.get("words", [])), 2)

    # 個別指定 (色・文字寄せ・フォント) も両方へ引き継がれる
    def test_default_keeps_overrides(self):
        timeline, track = _timeline()
        clip = timeline.clip_by_id("s1")
        clip.color, clip.align, clip.font = "#FF0000", "left", "Meiryo"
        commands.SplitSubtitle("s1", 1.0).apply(timeline)
        for got in _clips(track):
            self.assertEqual((got.color, got.align, got.font),
                             ("#FF0000", "left", "Meiryo"))

    # 最小尺を満たさない位置では割れない (従来の規約どおり)
    def test_min_duration_is_respected(self):
        timeline, track = _timeline()
        self.assertFalse(
            commands.SplitSubtitle("s1", 0.01, min_clip_sec=0.05).apply(timeline))
        self.assertEqual(len(track.clips), 1)

    # 「ここで分割」(SplitClip) でも既定は複製
    def test_split_clip_command_duplicates(self):
        timeline, track = _timeline(_ORIGIN)
        self.assertTrue(commands.SplitClip("s1", 1.0).apply(timeline))
        self.assertEqual([c.text for c in _clips(track)],
                         ["こんにちは", "こんにちは"])

    # 設定で戻せば本文も分かれる (温存した旧挙動 / G4)
    # 語の境界 (0.4 秒 = 2 語目の開始) では語で分かれる
    def test_split_text_option_splits_at_word(self):
        timeline, track = _timeline(_ORIGIN)
        commands.SplitSubtitle("s1", 0.4, split_text=True).apply(timeline)
        self.assertEqual([c.text for c in _clips(track)], ["こんに", "ちは"])

    # 語の境界から外れた位置では尺の比率で按分される (G4 の既定の振る舞い)
    def test_split_text_option_falls_back_to_ratio(self):
        timeline, track = _timeline(_ORIGIN)
        commands.SplitSubtitle("s1", 1.0, split_text=True).apply(timeline)
        self.assertEqual([c.text for c in _clips(track)], ["こん", "にちは"])
        timeline, track = _timeline(_ORIGIN)
        commands.SplitClip("s1", 1.0, split_text=True).apply(timeline)
        self.assertEqual([c.text for c in _clips(track)], ["こん", "にちは"])


class SplitTextSettingTest(unittest.TestCase):

    # 既定は false (複製) で、不正値も false 側へ寄せない (bool 化のみ)
    def test_timeline_config_default(self):
        self.assertFalse(timeline_config({})["subtitle_split_text"])
        self.assertTrue(timeline_config(
            {"timeline": {"subtitle_split_text": True}})["subtitle_split_text"])

    # 更新インストール (旧 setting.json が復元される) でも既定が届く
    def test_merge_with_defaults_fills_key(self):
        merged = sw._merge_with_defaults({"timeline": {"min_clip_sec": 0.05}})
        self.assertIn("subtitle_split_text", merged["timeline"])
        self.assertFalse(merged["timeline"]["subtitle_split_text"])

    # 配布既定値 (setting.json) も false であること
    def test_shipped_setting_is_false(self):
        import json

        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "src", "settings", "setting.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertIs(data["timeline"]["subtitle_split_text"], False)


class ControllerSplitTest(unittest.TestCase):

    # 画面からの分割 (controller 経由) で複製され、Undo 1 手で戻る
    def _controller(self, settings=None):
        from PySide6.QtWidgets import QApplication

        from src.gui.timeline.timeline_controller import TimelineController

        self.app = QApplication.instance() or QApplication([])
        timeline, track = _timeline(_ORIGIN)
        return TimelineController(
            timeline, settings or dict(sw.DEFAULT_SETTINGS)), track

    def test_controller_duplicates_text(self):
        controller, track = self._controller()
        self.assertTrue(controller.split_subtitle("s1", 1.0))
        self.assertEqual([c.text for c in _clips(track)],
                         ["こんにちは", "こんにちは"])
        controller.undo()
        # Undo はスナップショットを戻すため、トラックは取り直して見る
        self.assertEqual(
            [c.text for c in controller.timeline.subtitle_clips()], ["こんにちは"])

    def test_controller_honors_setting(self):
        import copy

        settings = copy.deepcopy(sw.DEFAULT_SETTINGS)
        settings["timeline"]["subtitle_split_text"] = True
        controller, track = self._controller(settings)
        controller.split_subtitle("s1", 0.4)
        self.assertEqual([c.text for c in _clips(track)], ["こんに", "ちは"])


if __name__ == "__main__":
    unittest.main()
