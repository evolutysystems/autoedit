# テロップ 1 本ごとの文字寄せ (ver9 resolve3 §9.1) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・寄せ未指定なら ASS が従来と 1 文字も変わらないこと (後方互換の要)
#   ・テロップの位置が動かないこと = 箱 (最長行の矩形) をイベント余白で表すこと
#   ・1 行 / 測れない / 余白に収まらない ときは寄せを諦めること (§R8)
#   ・位置ドラッグ済みでもアンカー補正だけで寄せられること (§R6)
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.modules import subtitle_generator
from src.settings import settings_window as sw
from src.settings.settings_window import DEFAULT_SETTINGS
from src.timeline import commands
from src.timeline.builder import timeline_config
from src.timeline.model import (
    BASE_SUBTITLE_TRACK_ID,
    TRACK_SUBTITLE,
    SubtitleClip,
    Timeline,
    Track,
    Transform,
)

# 2 行のテロップ (折り返し済み)。文字寄せが効くのは複数行のときだけ。
_TWO_LINES = "これは短い行\\Nこちらはずっと長い行です"
# 測定値の差し替え用 (最長行 = 700px / 2 行 / 実測で取れた)
_MEASURED = {"width": 700, "height": 240, "lines": 2, "measured": True}
# 位置未指定のテロップに付く既定アンカー (ver9 resolve8 §3.2-1)。
# 既定 alignment=2 / 余白 40,40,60 → x=(40+1920-40)/2=960 / y=1080-60=1020。
# 文字寄せの箱 (幅 700 / 左端 610) は、この \pos のアンカー移動で表す (§3.2-4)。
_DEFAULT_POS = "{\\an2\\pos(960.0,1020.0)}"


def _cfg(**overrides):
    cfg = dict(DEFAULT_SETTINGS["subtitle"])
    cfg.update(overrides)
    return cfg


def _profile(cfg=None):
    return subtitle_generator.build_font_profile(cfg or _cfg())


def _item(align="", text=_TWO_LINES, **extra):
    item = {"start": 1.0, "end": 2.0, "text": text, "role": "streamer"}
    if align:
        item["align"] = align
    item.update(extra)
    return item


# items から ASS を書き出して行を返す
def _lines(items, cfg=None, subtitle_cfg=None, extent=None):
    with tempfile.TemporaryDirectory() as work:
        path = os.path.join(work, "out.ass")
        if extent is None:
            subtitle_generator.build_subtitle_file(
                items, _profile(cfg), path, video_width=1920, video_height=1080,
                subtitle_cfg=subtitle_cfg)
        else:
            # 箱の幅は実フォントに依存するため、箱の計算だけを見るときは差し替える
            with mock.patch.object(subtitle_generator.comment_decor, "text_extent",
                                   return_value=dict(extent)):
                subtitle_generator.build_subtitle_file(
                    items, _profile(cfg), path, video_width=1920, video_height=1080,
                    subtitle_cfg=subtitle_cfg)
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()


def _dialogues(lines):
    return [line for line in lines if line.startswith("Dialogue: ")]


# Dialogue 行を (余白3つ, 本文) に分ける
def _fields(line):
    parts = line.split(",", 9)
    return parts[5:8], parts[9]


class AlignmentMathTest(unittest.TestCase):

    # 段 (上下) を保ったまま左右だけ差し替わる (§0.3)
    def test_alignment_with_align(self):
        for alignment, align, expected in (
                (2, "left", 1), (2, "center", 2), (2, "right", 3),
                (5, "right", 6), (8, "left", 7), (4, "center", 5), (9, "left", 7)):
            self.assertEqual(
                subtitle_generator.alignment_with_align(alignment, align), expected,
                f"alignment={alignment} align={align}")

    # 未指定・不正値は段も列も崩さない
    def test_alignment_with_align_keeps_value(self):
        for align in ("", None, "bogus", "LEFTISH", 3):
            self.assertEqual(subtitle_generator.alignment_with_align(2, align), 2)
        self.assertEqual(subtitle_generator.alignment_with_align(0, "left"), 0)

    # テンキー配置 → 寄せ (インスペクタの既定表示 / §R3)
    def test_align_of_alignment(self):
        expected = ["left", "center", "right"] * 3
        for alignment in range(1, 10):
            self.assertEqual(subtitle_generator.align_of_alignment(alignment),
                             expected[alignment - 1])
        # 範囲外は既定 (2 = 中央) 扱い
        self.assertEqual(subtitle_generator.align_of_alignment(0), "center")
        self.assertEqual(subtitle_generator.align_of_alignment(None), "center")

    def test_normalize_align(self):
        self.assertEqual(subtitle_generator.normalize_align("LEFT"), "left")
        self.assertEqual(subtitle_generator.normalize_align(" right "), "right")
        for value in ("", None, "bogus", 2):
            self.assertEqual(subtitle_generator.normalize_align(value), "")


class ModelAlignTest(unittest.TestCase):

    # setter で正規化される (EditSubtitle の setattr 経路を含む / §1.5)
    def test_align_is_normalized(self):
        self.assertEqual(SubtitleClip("s1", 0.0, 1.0, align="LEFT").align, "left")
        self.assertEqual(SubtitleClip("s1", 0.0, 1.0, align="bogus").align, "")
        self.assertEqual(SubtitleClip("s1", 0.0, 1.0).align, "")
        clip = SubtitleClip("s1", 0.0, 1.0)
        setattr(clip, "align", " Right ")
        self.assertEqual(clip.align, "right")
        setattr(clip, "align", None)
        self.assertEqual(clip.align, "")

    # 未指定なら item に align キーが載らない (下流の辞書を従来と同一に保つ)
    def test_to_item_omits_unset_align(self):
        self.assertNotIn("align", SubtitleClip("s1", 0.0, 1.0, text="あ").to_item())
        item = SubtitleClip("s1", 0.0, 1.0, text="あ", align="right").to_item()
        self.assertEqual(item["align"], "right")

    # 保存・復元と、キーが無い旧プロジェクトからの復元
    def test_dict_roundtrip(self):
        clip = SubtitleClip("s1", 1.0, 2.0, text="あ", align="center")
        restored = SubtitleClip.from_dict(clip.to_dict())
        self.assertEqual(restored.align, "center")
        data = clip.to_dict()
        data.pop("align")
        self.assertEqual(SubtitleClip.from_dict(data).align, "")
        self.assertEqual(clip.copy().align, "center")


class AssOutputTest(unittest.TestCase):

    # 寄せ未指定なら寄せのタグは出ず、既定アンカーの \pos だけが付く
    # (位置の明示は ver9 resolve8 §3.2。余白は 0,0,0 のまま)
    def test_unset_align_emits_default_anchor_only(self):
        lines = _dialogues(_lines([_item()], subtitle_cfg=_cfg()))
        self.assertEqual(len(lines), 1)
        margins, text = _fields(lines[0])
        self.assertEqual(margins, ["0", "0", "0"])
        self.assertEqual(text, _DEFAULT_POS + _TWO_LINES)

    # 位置の明示を切れば従来と 1 文字も変わらない (ver9 resolve8 §3.2-6 の切り戻し)
    def test_unset_align_is_unchanged_without_explicit_position(self):
        lines = _dialogues(_lines([_item()],
                                  subtitle_cfg=_cfg(explicit_position=False)))
        margins, text = _fields(lines[0])
        self.assertEqual(margins, ["0", "0", "0"])
        self.assertEqual(text, _TWO_LINES)

    # 左寄せ: \an1 が付き、箱 (最長行の矩形) の左端へアンカーが移る
    # 既定 alignment=2 / margin 40,40 / 幅 700 → 余り 1840-700=1140
    #   箱の左端 = 40 + 1140/2 = 610 → \an1\pos(610,1020)
    def test_left_align_keeps_box(self):
        lines = _dialogues(_lines([_item("left")], subtitle_cfg=_cfg(),
                                  extent=_MEASURED))
        margins, text = _fields(lines[0])
        self.assertEqual(margins, ["0", "0", "0"])
        self.assertTrue(text.startswith("{\\an1\\pos(610.0,1020.0)}"), text)

    # 右寄せ: 箱は左寄せと同じで、アンカーが右端 (610+700) へ移る
    def test_right_align_keeps_box(self):
        lines = _dialogues(_lines([_item("right")], subtitle_cfg=_cfg(),
                                  extent=_MEASURED))
        margins, text = _fields(lines[0])
        self.assertEqual(margins, ["0", "0", "0"])
        self.assertTrue(text.startswith("{\\an3\\pos(1310.0,1020.0)}"), text)

    # 位置の明示を切ったときは、従来どおり箱をイベント余白で表す (§3.2-4)
    def test_align_uses_event_margins_without_explicit_position(self):
        cfg = _cfg(explicit_position=False)
        for align, an in (("left", 1), ("right", 3)):
            lines = _dialogues(_lines([_item(align)], subtitle_cfg=cfg,
                                      extent=_MEASURED))
            margins, text = _fields(lines[0])
            self.assertEqual(margins, ["610", "610", "0"])
            self.assertTrue(text.startswith("{\\an%d}" % an), text)

    # 設定が左寄せ (alignment=1) のときの箱は左余白から始まる
    def test_box_follows_setting_alignment(self):
        lines = _dialogues(_lines([_item("right")], cfg=_cfg(alignment=1),
                                  subtitle_cfg=_cfg(alignment=1), extent=_MEASURED))
        margins, text = _fields(lines[0])
        self.assertEqual(margins, ["0", "0", "0"])
        # 箱の左端 = 40 / 右端 = 40+700 = 740 (上下は下段のまま y=1020)
        self.assertTrue(text.startswith("{\\an3\\pos(740.0,1020.0)}"), text)

    # 実効が設定と同じ (中央寄せ × alignment=2) なら寄せのタグは出さない
    # (位置の明示だけが残る = 既定アンカー)
    def test_same_as_setting_emits_nothing(self):
        lines = _dialogues(_lines([_item("center")], subtitle_cfg=_cfg(),
                                  extent=_MEASURED))
        margins, text = _fields(lines[0])
        self.assertEqual(margins, ["0", "0", "0"])
        self.assertEqual(text, _DEFAULT_POS + _TWO_LINES)

    # 1 行のテロップは寄せても見た目が変わらないため出さない (§R8)
    def test_single_line_emits_nothing(self):
        extent = dict(_MEASURED, lines=1)
        lines = _dialogues(_lines([_item("left", text="1行だけ")],
                                  subtitle_cfg=_cfg(), extent=extent))
        margins, text = _fields(lines[0])
        self.assertEqual(margins, ["0", "0", "0"])
        self.assertEqual(text, _DEFAULT_POS + "1行だけ")

    # フォントの送り幅を読めないときは寄せない (位置がぶれるより寄らない方を選ぶ)
    def test_unmeasured_font_emits_nothing(self):
        extent = dict(_MEASURED, measured=False)
        lines = _dialogues(_lines([_item("left")], subtitle_cfg=_cfg(),
                                  extent=extent))
        margins, text = _fields(lines[0])
        self.assertEqual(margins, ["0", "0", "0"])
        self.assertEqual(text, _DEFAULT_POS + _TWO_LINES)

    # require_measured_font=false なら推定値でも寄せる
    def test_unmeasured_allowed_by_setting(self):
        extent = dict(_MEASURED, measured=False)
        cfg = _cfg(line_align={"enabled": True, "require_measured_font": False})
        lines = _dialogues(_lines([_item("left")], subtitle_cfg=cfg, extent=extent))
        self.assertTrue(
            _fields(lines[0])[1].startswith("{\\an1\\pos(610.0,1020.0)}"))

    # 箱が余白に収まらないときは寄せない (§R8)
    def test_overflow_emits_nothing(self):
        extent = dict(_MEASURED, width=1900)
        lines = _dialogues(_lines([_item("left")], subtitle_cfg=_cfg(),
                                  extent=extent))
        margins, text = _fields(lines[0])
        self.assertEqual(margins, ["0", "0", "0"])
        self.assertEqual(text, _DEFAULT_POS + _TWO_LINES)

    # 設定で切ったら焼き込み側で効かない (値は保持する / §R13)
    def test_disabled_by_setting(self):
        cfg = _cfg(line_align={"enabled": False})
        lines = _dialogues(_lines([_item("left")], subtitle_cfg=cfg,
                                  extent=_MEASURED))
        self.assertEqual(_fields(lines[0])[0], ["0", "0", "0"])

    # subtitle_cfg を渡さない旧来の呼び出しでは何も変わらない (後方互換)
    def test_without_subtitle_cfg(self):
        lines = _dialogues(_lines([_item("left")], extent=_MEASURED))
        self.assertEqual(_fields(lines[0])[0], ["0", "0", "0"])

    # 位置ドラッグ済み: アンカーと \pos の x だけ動かす (上下 = py は不変 / §R6)
    def test_positioned_uses_anchor(self):
        base = _dialogues(_lines([_item(pos_x=0.0, pos_y=0.0)], subtitle_cfg=_cfg(),
                                 extent=_MEASURED))[0]
        self.assertIn("{\\an5\\pos(960.0,540.0)}", base)
        left = _dialogues(_lines([_item("left", pos_x=0.0, pos_y=0.0)],
                                 subtitle_cfg=_cfg(), extent=_MEASURED))[0]
        # 箱の中心 960 / 幅 700 → 左端 610 でアンカーを左中央 (\an4) へ
        self.assertIn("{\\an4\\pos(610.0,540.0)}", left)
        right = _dialogues(_lines([_item("right", pos_x=0.0, pos_y=0.0)],
                                  subtitle_cfg=_cfg(), extent=_MEASURED))[0]
        self.assertIn("{\\an6\\pos(1310.0,540.0)}", right)
        # 位置指定ありのときイベント余白は使わない (\pos が位置を決めるため)
        self.assertEqual(_fields(left)[0], ["0", "0", "0"])

    # コメント役割は Style が左寄せ (4) のため、左寄せ指定では何も出さない
    def test_comment_left_is_noop(self):
        item = _item("left", text="コメント本文\\N2行目", role="comment")
        lines = _dialogues(_lines([item], subtitle_cfg=_cfg(), extent=_MEASURED))
        body = [line for line in lines if "{\\an" in line or "コメント" in line]
        self.assertTrue(all("\\an1" not in line for line in body), body)

    # 文字寄せ以外の個別指定 (フォント/サイズ/色) と同時に使える
    def test_combined_with_other_overrides(self):
        item = _item("left", font="Meiryo", font_size=80, color="#FF0000")
        lines = _dialogues(_lines([item], subtitle_cfg=_cfg(), extent=_MEASURED))
        text = _fields(lines[0])[1]
        self.assertTrue(
            text.startswith(
                "{\\an1\\pos(610.0,1020.0)\\fnMeiryo\\fs80\\1c&H0000FF&}"),
            text)


class CommandAlignTest(unittest.TestCase):

    def _timeline(self):
        timeline = Timeline(fps=60, width=1920, height=1080)
        timeline.tracks = [
            Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1",
                  clips=[SubtitleClip("s1", 1.0, 2.0, text="あ"),
                         SubtitleClip("s2", 4.0, 2.0, text="い")]),
        ]
        return timeline

    # 複数の字幕へ 1 コマンドで入り、Undo 1 手で戻る (§R7 / §R10)
    def test_edit_subtitles_sets_align(self):
        timeline = self._timeline()
        stack = commands.CommandStack()
        self.assertTrue(stack.push(
            timeline, commands.EditSubtitles(["s1", "s2"], align="right")))
        self.assertEqual([c.align for c in timeline.subtitle_clips()],
                         ["right", "right"])
        stack.undo(timeline)
        self.assertEqual([c.align for c in timeline.subtitle_clips()], ["", ""])

    # 不正値はコマンド経由でも "" へ正規化される
    def test_edit_subtitle_normalizes(self):
        timeline = self._timeline()
        stack = commands.CommandStack()
        stack.push(timeline, commands.EditSubtitle("s1", align="BOGUS"))
        self.assertEqual(timeline.clip_by_id("s1").align, "")

    # 設定に戻す ("" の指定) も 1 手で効く
    def test_clear_align(self):
        timeline = self._timeline()
        timeline.clip_by_id("s1").align = "left"
        stack = commands.CommandStack()
        self.assertTrue(stack.push(
            timeline, commands.EditSubtitles(["s1"], align="")))
        self.assertEqual(timeline.clip_by_id("s1").align, "")


class SettingsTest(unittest.TestCase):

    # UI 側の設定が既定で補われ、不正値も吸収される
    def test_timeline_config_defaults(self):
        cfg = timeline_config({})["subtitle_align"]
        self.assertEqual(cfg, {"enabled": True, "apply_to_selection": True,
                               "clear_on_reclick": True,
                               "allow_comment_role": False})
        broken = timeline_config({"timeline": {"subtitle_align": "bogus"}})
        self.assertTrue(broken["subtitle_align"]["enabled"])
        self.assertEqual(
            timeline_config({})["ui"]["subtitle_align_button_width_px"], 56)

    # 更新インストール (旧 setting.json が復元される) でも既定が届く
    def test_merge_with_defaults_fills_new_keys(self):
        data = {"subtitle": {"alignment": 2}, "timeline": {"min_clip_sec": 0.05}}
        merged = sw._merge_with_defaults(data)
        self.assertIn("line_align", merged["subtitle"])
        self.assertTrue(merged["subtitle"]["line_align"]["enabled"])
        self.assertIn("subtitle_align", merged["timeline"])
        self.assertIn("subtitle_align_button_width_px", merged["timeline"]["ui"])


class ResolveExportAlignTest(unittest.TestCase):

    # title の寄せ (align) だけ差し替わり、位置 (position) は変わらない (§4.4)
    def test_title_align_only(self):
        from src.export import resolve_export
        from src.export.config import resolve_config

        title_cfg = resolve_config(dict(DEFAULT_SETTINGS))["title"]
        profile = _profile()
        base = resolve_export._title_from_item(
            {"start": 0.0, "end": 1.0, "text": "あ\nい", "role": "streamer"},
            0.0, profile, (1920, 1080), title_cfg, "t1")
        right = resolve_export._title_from_item(
            {"start": 0.0, "end": 1.0, "text": "あ\nい", "role": "streamer",
             "align": "right"},
            0.0, profile, (1920, 1080), title_cfg, "t1")
        self.assertEqual(base["align"], "center")
        self.assertEqual(right["align"], "right")
        self.assertEqual(right["position"], base["position"])


class InspectorAlignTest(unittest.TestCase):

    # インスペクタの 3 ボタンが排他で、押すと保存され、押し直しで設定へ戻る
    # (ver9 resolve3 §R1 / §R4 / §R7)
    def _panel(self, settings=None):
        from PySide6.QtWidgets import QApplication

        from src.gui.timeline.timeline_controller import TimelineController
        from src.gui.timeline.timeline_editor_dialog import _InspectorPanel

        self.app = QApplication.instance() or QApplication([])
        timeline = Timeline(fps=60, width=1920, height=1080)
        timeline.tracks = [
            Track(BASE_SUBTITLE_TRACK_ID, TRACK_SUBTITLE, 1, name="Subtitle 1",
                  clips=[SubtitleClip("s1", 1.0, 2.0, text="あ\nい"),
                         SubtitleClip("s2", 4.0, 2.0, text="う\nえ")]),
        ]
        controller = TimelineController(timeline, settings or dict(DEFAULT_SETTINGS))
        return controller, _InspectorPanel(controller)

    # 機能 OFF では行を出さない (保存済みの値は残す / §R13)
    def test_disabled_builds_without_row(self):
        import copy

        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["timeline"]["subtitle_align"]["enabled"] = False
        controller, panel = self._panel(settings)
        clip = controller.timeline.clip_by_id("s1")
        clip.align = "left"
        panel.show_clip(clip)              # 落ちないこと
        self.assertEqual(panel.align_buttons, {})
        self.assertEqual(clip.align, "left")

    def test_default_follows_setting(self):
        controller, panel = self._panel()
        clip = controller.timeline.clip_by_id("s1")
        panel.show_clip(clip)
        # 設定 alignment=2 (下段・中央) → 中央寄せが押された状態で出る
        self.assertTrue(panel.align_buttons["center"].isChecked())
        self.assertFalse(panel.align_buttons["left"].isChecked())
        self.assertEqual(clip.align, "")        # 表示しただけでは保存しない

    def test_click_saves_and_toggles(self):
        controller, panel = self._panel()
        clip = controller.timeline.clip_by_id("s1")
        controller.select(["s1"])
        panel.show_clip(clip)
        panel.align_buttons["left"].click()
        self.assertEqual(clip.align, "left")
        self.assertTrue(panel.align_buttons["left"].isChecked())
        # 同じボタンを押し直したら「設定に従う」へ戻る
        panel.align_buttons["left"].click()
        self.assertEqual(clip.align, "")
        self.assertTrue(panel.align_buttons["center"].isChecked())

    def test_applies_to_selection(self):
        controller, panel = self._panel()
        controller.select(["s1", "s2"])
        panel.show_clip(controller.timeline.clip_by_id("s1"))
        panel.align_buttons["right"].click()
        self.assertEqual([c.align for c in controller.timeline.subtitle_clips()],
                         ["right", "right"])
        # 1 コマンド = Undo 1 手
        controller.undo()
        self.assertEqual([c.align for c in controller.timeline.subtitle_clips()],
                         ["", ""])

    def test_comment_role_is_disabled(self):
        controller, panel = self._panel()
        clip = controller.timeline.clip_by_id("s1")
        clip.role = "comment"
        panel.show_clip(clip)
        self.assertFalse(panel.align_buttons["left"].isEnabled())
        # コメントの設定は comment_alignment=4 (左) のため左寄せが押された状態
        self.assertTrue(panel.align_buttons["left"].isChecked())


class PreviewAlignTest(unittest.TestCase):

    # プレビューの行寄せはクリップの指定が最優先 (箱の位置は変えない / §4.3)
    def test_line_alignment_prefers_clip(self):
        from src.gui.timeline import preview_items

        eff = _cfg()
        clip = SubtitleClip("s1", 0.0, 1.0, text="あ\nい")
        self.assertEqual(
            preview_items._line_alignment(clip, eff, (1920, 1080), "streamer"),
            "center")
        clip.align = "left"
        self.assertEqual(
            preview_items._line_alignment(clip, eff, (1920, 1080), "streamer"),
            "left")
        # 位置指定ありでも寄せの指定が勝つ (焼き込みと同じ規約)
        clip.transform = Transform(x=0.2, y=0.2)
        self.assertEqual(
            preview_items._line_alignment(clip, eff, (1920, 1080), "streamer"),
            "left")


if __name__ == "__main__":
    unittest.main()
