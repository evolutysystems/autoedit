# テロップアニメーションのプレビュー再現 (docs/request/ver12/resolve2.md §8) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・動きが付いたテロップだけが「時刻で描き直す」対象になること (§8 方式 A)
#   ・単語ごとのパスが本文の文字列と一致すること (取りこぼし・重複が無い)
#   ・時刻を進めると見た目 (拡大率・上下のずれ) が変わること
#   ・Big Word は画面中央へ置かれ、位置ドラッグができないこと (§1)
#   ・設定 timeline.preview.subtitle_animation=false で静止表示へ戻せること
#   ・コメント役割はフェードのみ (単語ごとの動きにしない / §12 ②)
#   ・動きの量の式が焼き込み (ASS タグ) と同じ値を返すこと
import copy
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                       # noqa: E402

from src.gui.timeline.preview_items import SubtitleOverlayItem   # noqa: E402
from src.modules import subtitle_animation, subtitle_generator   # noqa: E402
from src.settings.settings_window import DEFAULT_SETTINGS        # noqa: E402
from src.timeline.model import SubtitleClip                      # noqa: E402

_CANVAS = (1920, 1080)
_CFG = copy.deepcopy(DEFAULT_SETTINGS["subtitle"])
_TEXT = "今日はとても良い天気ですね"


def setUpModule():
    if QApplication.instance() is None:
        setUpModule.app = QApplication([])


def _clip(**fields):
    clip = SubtitleClip("s1", 1.0, 2.0, text=_TEXT)
    for key, value in fields.items():
        setattr(clip, key, value)
    return clip


def _item(clip, animate=True, cfg=None):
    cfg = cfg or _CFG
    return SubtitleOverlayItem(clip, cfg, subtitle_generator.build_font_profile(cfg),
                               _CANVAS, animate=animate)


class AnimatedFlagTest(unittest.TestCase):

    # 動きが無いテロップは時刻を渡しても何も起きない (従来どおり描き直さない)
    def test_plain_clip_is_not_animated(self):
        item = _item(_clip())
        self.assertFalse(item.is_animated())

    def test_animated_clip_is_marked(self):
        self.assertTrue(_item(_clip(animation="word_pop")).is_animated())
        self.assertTrue(_item(_clip(fade_in=True)).is_animated())
        self.assertTrue(_item(_clip(fade_out=True)).is_animated())

    # コメントは単語ごとの動きにしない (背景とアイコンが追従できない)
    def test_comment_role_has_no_word_motion(self):
        item = _item(_clip(role="comment", animation="word_pop", fade_in=True))
        self.assertEqual(item._anim_kind, "")
        self.assertTrue(item.is_animated(), "フェードは掛かる")

    # 機能を切ったら動きも印も付かない
    def test_disabled_setting_turns_everything_off(self):
        cfg = copy.deepcopy(_CFG)
        cfg["animation"]["enabled"] = False
        item = _item(_clip(animation="word_pop", fade_in=True), cfg=cfg)
        self.assertFalse(item.is_animated())


class WordPathTest(unittest.TestCase):

    # 単語ごとのパスは本文を取りこぼさない (焼き込みと同じ切り方を使う)
    def test_words_cover_the_text(self):
        item = _item(_clip(animation="word_pop"))
        words = subtitle_animation.split_words(
            item._anim_entry, subtitle_animation.config(_CFG))
        self.assertEqual(len(item._anim_words), len(words))
        self.assertEqual("".join(w["text"] for w in words), _TEXT)

    # 単語は左から順に並ぶ (行の中で重ならない)
    def test_words_are_laid_out_left_to_right(self):
        item = _item(_clip(animation="word_pop"))
        centers = [w["cx"] for w in item._anim_words]
        self.assertEqual(centers, sorted(centers))

    # 時刻を進めると拡大率が変わる (paint のやり直しだけで動く)
    def test_scale_changes_over_time(self):
        item = _item(_clip(animation="word_pop"))
        item.set_time(0.0)
        first = item._animation_frame()
        self.assertEqual(len(first), 1, "まだ 1 語目だけ出ている")
        self.assertGreater(first[0][1], 1.0, "出た直後は拡大している")

        item.set_time(0.5)
        later = item._animation_frame()
        self.assertGreater(len(later), 1, "時間が進めば語が増える")
        self.assertAlmostEqual(later[0][1], 1.0, places=3,
                               msg="先に出た語は元の大きさへ戻る")

    # Bounce は上下にずれる (最後は 0 へ収まる)
    def test_bounce_offset_settles(self):
        item = _item(_clip(animation="bounce"))
        item.set_time(0.05)
        self.assertLess(item._animation_frame()[0][2], 0.0, "上へ跳ねる")
        item.set_time(1.5)
        self.assertAlmostEqual(item._animation_frame()[0][2], 0.0, places=6)

    # 静止表示では時刻を渡しても動かない (退避口 / §8 B 案)
    def test_static_mode_shows_the_settled_state(self):
        item = _item(_clip(animation="word_pop"), animate=False)
        item.set_time(0.0)
        frame = item._animation_frame()
        self.assertEqual(len(frame), len(item._anim_words),
                         "静止表示では全部の語が出ている")
        for _path, scale, offset_y, _emphasis in frame:
            self.assertAlmostEqual(scale, 1.0, places=6)
            self.assertAlmostEqual(offset_y, 0.0, places=6)


class BigWordTest(unittest.TestCase):

    def test_placed_at_the_canvas_center(self):
        item = _item(_clip(animation="big_word"))
        self.assertAlmostEqual(item.pos().x(), _CANVAS[0] / 2.0, places=3)
        self.assertAlmostEqual(item.pos().y(), _CANVAS[1] / 2.0, places=3)
        # 文字の矩形は原点 (= 画面中央) を囲む
        rect = item.text_rect()
        self.assertLess(rect.left(), 0.0)
        self.assertLess(rect.top(), 0.0)

    # 1 語ずつ切り替わる (同時に 2 語は出ない)
    def test_shows_one_word_at_a_time(self):
        item = _item(_clip(animation="big_word"))
        for time_sec in (0.0, 0.5, 1.0, 1.9):
            item.set_time(time_sec)
            self.assertEqual(len(item._animation_frame()), 1)

    # 位置は意味を持たないためドラッグさせない
    def test_not_movable(self):
        from PySide6.QtWidgets import QGraphicsItem

        item = _item(_clip(animation="big_word"))
        self.assertFalse(bool(item.flags() & QGraphicsItem.ItemIsMovable))


class CurveTest(unittest.TestCase):

    # プレビューの式が ASS タグと同じ値を返すこと (ずれると見え方が食い違う)
    def test_word_pop_scale_matches_the_tag(self):
        cfg = subtitle_animation.config(_CFG)
        grow = cfg["word_pop_grow_ms"] / 1000.0
        settle = cfg["word_pop_settle_ms"] / 1000.0
        self.assertAlmostEqual(
            subtitle_animation.word_pop_scale(cfg, 0.0),
            float(cfg["word_pop_scale"]), places=6)
        self.assertAlmostEqual(
            subtitle_animation.word_pop_scale(cfg, grow),
            float(cfg["word_pop_scale"]), places=6)
        self.assertAlmostEqual(
            subtitle_animation.word_pop_scale(cfg, grow + settle), 100.0, places=6)
        middle = subtitle_animation.word_pop_scale(cfg, grow + settle / 2.0)
        self.assertGreater(middle, 100.0)
        self.assertLess(middle, float(cfg["word_pop_scale"]))

    def test_bounce_offset_matches_the_segments(self):
        cfg = subtitle_animation.config(_CFG)
        total = cfg["bounce_ms"] / 1000.0
        span = total / cfg["bounce_segments"]
        height = cfg["bounce_height_ratio"] * 100.0
        # 1 段目の頂点 (= span の半分) で最大の高さになる
        self.assertAlmostEqual(
            subtitle_animation.bounce_offset(cfg, span / 2.0, 100.0),
            -height, places=6)
        # 2 段目は振幅が半分
        self.assertAlmostEqual(
            subtitle_animation.bounce_offset(cfg, span + span / 2.0, 100.0),
            -height / 2.0, places=6)
        # 収まったら 0
        self.assertAlmostEqual(
            subtitle_animation.bounce_offset(cfg, total, 100.0), 0.0, places=6)


class InspectorTest(unittest.TestCase):

    # インスペクタ (本文と同じ欄) から設定できること = 要望の条件 (request2 §1 B)
    def _panel(self, settings=None):
        from src.gui.timeline.timeline_controller import TimelineController
        from src.gui.timeline.timeline_editor_dialog import _InspectorPanel
        from src.timeline.model import (
            BASE_SUBTITLE_TRACK_ID,
            TRACK_SUBTITLE,
            Timeline,
            Track,
        )

        timeline = Timeline(fps=30, width=_CANVAS[0], height=_CANVAS[1])
        timeline.tracks = [
            Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1",
                  clips=[SubtitleClip("s1", 1.0, 2.0, text=_TEXT),
                         SubtitleClip("s2", 4.0, 2.0, text=_TEXT)]),
        ]
        controller = TimelineController(
            timeline, settings or copy.deepcopy(DEFAULT_SETTINGS))
        return controller, _InspectorPanel(controller)

    def test_shows_the_current_values(self):
        controller, panel = self._panel()
        clip = controller.timeline.clip_by_id("s1")
        clip.animation = "bounce"
        clip.fade_in = True
        clip.emphasis = "天気"
        panel.show_clip(clip)

        self.assertEqual(panel.animation_combo.currentData(), "bounce")
        self.assertTrue(panel.fade_in_check.isChecked())
        self.assertFalse(panel.fade_out_check.isChecked())
        self.assertEqual(panel.emphasis_edit.text(), "天気")
        # 強調する語は Word Pop のときだけ触れる (既定)
        self.assertFalse(panel.emphasis_edit.isEnabled())
        clip.animation = "word_pop"
        panel.show_clip(clip)
        self.assertTrue(panel.emphasis_edit.isEnabled())

    def test_selecting_a_kind_applies_to_the_selection(self):
        controller, panel = self._panel()
        controller.select(["s1", "s2"])
        panel.show_clip(controller.timeline.clip_by_id("s1"))
        panel.animation_combo.setCurrentIndex(
            panel.animation_combo.findData("word_pop"))
        panel.animation_combo.activated.emit(panel.animation_combo.currentIndex())

        self.assertEqual([c.animation for c in controller.timeline.subtitle_clips()],
                         ["word_pop", "word_pop"])
        # 1 コマンド = Undo 1 手
        controller.undo()
        self.assertEqual([c.animation for c in controller.timeline.subtitle_clips()],
                         ["", ""])

    def test_fade_checkboxes_apply(self):
        controller, panel = self._panel()
        controller.select(["s1"])
        clip = controller.timeline.clip_by_id("s1")
        panel.show_clip(clip)
        panel.fade_in_check.click()
        self.assertTrue(clip.fade_in)
        panel.fade_out_check.click()
        self.assertTrue(clip.fade_out)
        panel.fade_in_check.click()
        self.assertFalse(clip.fade_in)

    # コメントは種類を選べない (フェードだけ)
    def test_comment_role_disables_the_kind(self):
        controller, panel = self._panel()
        clip = controller.timeline.clip_by_id("s1")
        clip.role = "comment"
        panel.show_clip(clip)

        self.assertFalse(panel.animation_combo.isEnabled())
        self.assertTrue(panel.fade_in_check.isEnabled())
        self.assertIn("フェード", panel.animation_note_label.text())

    # 機能 OFF では欄を出さない (保存済みの値は残す)
    def test_disabled_builds_without_rows(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["timeline"]["subtitle_animation"]["enabled"] = False
        controller, panel = self._panel(settings)
        clip = controller.timeline.clip_by_id("s1")
        clip.animation = "bounce"
        panel.show_clip(clip)               # 落ちないこと

        self.assertIsNone(panel.animation_combo)
        self.assertEqual(clip.animation, "bounce")


if __name__ == "__main__":
    unittest.main()
