# テロップアニメーションのプレビュー再現 (docs/request/ver12/resolve2-add.md §8) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・動きが付いたテロップだけが「時刻で描き直す」対象になること (resolve2 §8 方式 A)
#   ・プレビューが焼き込みと同じキーフレーム (subtitle_animation.spec) を見ること
#   ・時刻を進めると見た目が変わり、描画で落ちないこと (12 種すべて)
#   ・設定 timeline.preview.subtitle_animation=false で静止表示へ戻せること
#   ・コメント役割はフェードのみ (resolve2 §12 ②)
#   ・インスペクタ (本文と同じ欄) から 12 種を選べること
import copy
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPainter                # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsScene        # noqa: E402

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


# アイテムを画像へ描いて、背景以外の画素数を返す
def _ink(item):
    scene = QGraphicsScene(0, 0, _CANVAS[0], _CANVAS[1])
    scene.addItem(item)
    image = QImage(_CANVAS[0] // 4, _CANVAS[1] // 4, QImage.Format_RGB32)
    image.fill(QColor(128, 128, 128))
    painter = QPainter(image)
    scene.render(painter)
    painter.end()
    background = QColor(128, 128, 128).rgb()
    count = 0
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            if image.pixel(x, y) != background:
                count += 1
    scene.removeItem(item)
    return count


class AnimatedFlagTest(unittest.TestCase):

    # 動きが無いテロップは時刻を渡しても何も起きない (従来どおり描き直さない)
    def test_plain_clip_is_not_animated(self):
        self.assertFalse(_item(_clip()).is_animated())

    def test_animated_clip_is_marked(self):
        self.assertTrue(_item(_clip(animation="poyon")).is_animated())
        self.assertTrue(_item(_clip(fade_in=True)).is_animated())
        self.assertTrue(_item(_clip(fade_out=True)).is_animated())

    # 廃止した種類は動きなし
    def test_removed_kind_is_not_animated(self):
        self.assertFalse(_item(_clip(animation="word_pop")).is_animated())

    # コメントは動きにしない (背景とアイコンが追従できない)
    def test_comment_role_has_no_motion(self):
        item = _item(_clip(role="comment", animation="poyon", fade_in=True))
        self.assertEqual(item._anim_kind, "")
        self.assertTrue(item.is_animated(), "フェードは掛かる")

    # 機能を切ったら動きも付かない
    def test_disabled_setting_turns_everything_off(self):
        cfg = copy.deepcopy(_CFG)
        cfg["animation"]["enabled"] = False
        item = _item(_clip(animation="poyon", fade_in=True), cfg=cfg)
        self.assertFalse(item.is_animated())


class KeyframeTest(unittest.TestCase):

    # プレビューは焼き込みと同じキーフレームを使う
    def test_uses_the_same_spec_as_the_burn_in(self):
        item = _item(_clip(animation="poyon"))
        expected = subtitle_animation.spec(
            "poyon", subtitle_animation.config(_CFG), item._anim_size, 2.0,
            float(_CANVAS[0]))
        self.assertEqual(item._anim_spec["layers"][0]["keys"],
                         expected["layers"][0]["keys"])

    # 動きではみ出す分だけ描画範囲が広がる (飛び込みツッコミは 170% から縮む)
    def test_bounding_rect_covers_the_motion(self):
        plain = _item(_clip())
        moving = _item(_clip(animation="dive_tsukkomi"))
        extra = moving.boundingRect().width() - plain.boundingRect().width()
        self.assertGreater(extra, plain._text_width)

    # 12 種すべてが時刻を進めても落ちずに描ける
    def test_every_kind_paints(self):
        for kind in subtitle_animation.ANIMATIONS:
            for time_sec in (0.0, 0.1, 0.3, 1.0):
                item = _item(_clip(animation=kind))
                item.set_time(time_sec)
                _ink(item)

    # 飛び込みツッコミは、はじめは見えず (透明)、あとで見える
    def test_dive_tsukkomi_reveals_over_time(self):
        item = _item(_clip(animation="dive_tsukkomi"))
        item.set_time(0.0)
        hidden = _ink(item)
        item = _item(_clip(animation="dive_tsukkomi"))
        item.set_time(1.0)
        shown = _ink(item)
        self.assertLess(hidden, shown / 10 + 1)

    # 静止表示では時刻を渡しても動かない (退避口 / resolve2 §8 B 案)
    def test_static_mode_shows_the_settled_state(self):
        plain = _ink(_item(_clip()))
        item = _item(_clip(animation="dive_tsukkomi"), animate=False)
        item.set_time(0.0)
        self.assertEqual(_ink(item), plain)


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
        clip.animation = "shock2"
        clip.fade_in = True
        panel.show_clip(clip)

        self.assertEqual(panel.animation_combo.currentData(), "shock2")
        self.assertTrue(panel.fade_in_check.isChecked())
        self.assertFalse(panel.fade_out_check.isChecked())
        # 廃止した「強調する語」の欄は出さない
        self.assertFalse(hasattr(panel, "emphasis_edit"))

    # コンボには「なし」+ 12 種が並ぶ (値は焼き込みと同じ)
    def test_combo_lists_all_kinds(self):
        _controller, panel = self._panel()
        values = [panel.animation_combo.itemData(i)
                  for i in range(panel.animation_combo.count())]
        self.assertEqual(values, [""] + list(subtitle_animation.ANIMATIONS))

    def test_selecting_a_kind_applies_to_the_selection(self):
        controller, panel = self._panel()
        controller.select(["s1", "s2"])
        panel.show_clip(controller.timeline.clip_by_id("s1"))
        panel.animation_combo.setCurrentIndex(
            panel.animation_combo.findData("double_jump"))
        panel.animation_combo.activated.emit(panel.animation_combo.currentIndex())

        self.assertEqual([c.animation for c in controller.timeline.subtitle_clips()],
                         ["double_jump", "double_jump"])
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
        clip.animation = "shine"
        panel.show_clip(clip)               # 落ちないこと

        self.assertIsNone(panel.animation_combo)
        self.assertEqual(clip.animation, "shine")


if __name__ == "__main__":
    unittest.main()
