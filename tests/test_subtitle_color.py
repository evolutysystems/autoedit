# 字幕のクリップ個別カラー (ver3 resolve6 §10.1) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・色を指定しないときの出力が変更前と 1 文字も変わらないこと (回帰の要)
#   ・ASS インライン上書きタグの形式と順序
#   ・複数選択への一括適用 (EditSubtitles) が 1 手の Undo で戻ること
import os
import tempfile
import unittest

from src.export import resolve_export
from src.modules import subtitle_generator
from src.timeline import commands
from src.timeline.model import (
    AudioClip,
    Clip,
    MediaRef,
    ORIGIN_SILENCE_CUT,
    SubtitleClip,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    Timeline,
    Track,
)


# 最小構成の FontProfile (色以外は既定)
def _profile():
    return subtitle_generator.FontProfile(
        family="TestFont", size=48, color_hex="#FFFFFF",
        outline_color="&H00000000", outline_width=3,
        role_colors={"streamer": "#FFFFFF", "sub": "#00FF00", "comment": "#0000FF"},
        role_outline_colors={"streamer": "&H00000000"},
        comment_label="コメント：",
    )


# items から ASS を書き出し、Dialogue 行だけを返す
def _dialogue_lines(items):
    with tempfile.TemporaryDirectory() as work:
        path = os.path.join(work, "out.ass")
        subtitle_generator.build_subtitle_file(items, _profile(), path,
                                               video_width=1920, video_height=1080)
        with open(path, encoding="utf-8") as f:
            text = f.read()
    return [line for line in text.splitlines() if line.startswith("Dialogue:")]


def _item(**over):
    item = {"start": 1.0, "end": 3.0, "text": "こんばんは", "use": True,
            "role": "streamer", "font": "", "font_size": None}
    item.update(over)
    return item


class TestAssColorOverride(unittest.TestCase):

    # 塗り: HTML #RRGGBB → \1c&HBBGGRR& (BGR へ並べ替える)
    def test_fill_color_tag(self):
        line = _dialogue_lines([_item(color="#123456")])[0]
        self.assertIn("{\\1c&H563412&}", line)

    # 縁: 8 桁 (&HAABBGGRR) は \3c と \3a を対で出す
    def test_outline_color_with_alpha(self):
        line = _dialogue_lines([_item(outline_color="&H80202020")])[0]
        self.assertIn("\\3c&H202020&", line)
        self.assertIn("\\3a&H80&", line)

    # 縁: 6 桁 (&HBBGGRR) は \3c のみ (Style のアルファを引き継ぐ)
    def test_outline_color_without_alpha(self):
        line = _dialogue_lines([_item(outline_color="&H202020")])[0]
        self.assertIn("\\3c&H202020&", line)
        self.assertNotIn("\\3a", line)

    # 位置 → フォント → サイズ → 塗り → 縁 の順に並べる (resolve6 §3-3)
    def test_override_order(self):
        line = _dialogue_lines([_item(
            font="MyFont", font_size=64, color="#FFE24B",
            outline_color="&H00202020", pos_x=0.0, pos_y=0.0)])[0]
        override = line[line.index("{"):line.index("}") + 1]
        self.assertLess(override.index("\\pos"), override.index("\\fn"))
        self.assertLess(override.index("\\fn"), override.index("\\fs"))
        self.assertLess(override.index("\\fs"), override.index("\\1c"))
        self.assertLess(override.index("\\1c"), override.index("\\3c"))

    # 色を指定しない items の出力は従来と同一 (上書きタグ自体が付かない)
    def test_no_color_keeps_output(self):
        line = _dialogue_lines([_item()])[0]
        self.assertEqual(line, "Dialogue: 0,0:00:01.00,0:00:03.00,Streamer,,0,0,0,,こんばんは")

    # 不正な色はタグを出さずに素通しする (落ちない)
    def test_invalid_colors_are_ignored(self):
        for bad in ("red", "#FFF", "#GGGGGG", "&HZZ", "&H1234567", 123):
            line = _dialogue_lines([_item(color=bad, outline_color=bad)])[0]
            self.assertNotIn("{", line, f"不正な色でタグが出ました: {bad!r}")

    # コメント役割ではラベルより前に上書きタグが来る (ラベルにも色が効く)
    def test_comment_label_after_override(self):
        line = _dialogue_lines([_item(role="comment", color="#112233")])[0]
        self.assertIn("{\\1c&H332211&}コメント：", line)


class TestSubtitleClipColor(unittest.TestCase):

    def test_to_item_omits_unset_colors(self):
        item = SubtitleClip("s1", 0.0, 1.0, "あ").to_item()
        self.assertNotIn("color", item)
        self.assertNotIn("outline_color", item)

    def test_to_item_includes_colors(self):
        clip = SubtitleClip("s1", 0.0, 1.0, "あ", color="#ABCDEF",
                            outline_color="&H00112233")
        item = clip.to_item()
        self.assertEqual(item["color"], "#ABCDEF")
        self.assertEqual(item["outline_color"], "&H00112233")

    def test_round_trip(self):
        clip = SubtitleClip("s1", 0.0, 1.0, "あ", color="#ABCDEF",
                            outline_color="&H00112233")
        restored = SubtitleClip.from_dict(clip.to_dict())
        self.assertEqual(restored.color, "#ABCDEF")
        self.assertEqual(restored.outline_color, "&H00112233")

    # 旧プロジェクト JSON (色のキーが無い) を読んでも壊れない
    def test_from_dict_without_colors(self):
        restored = SubtitleClip.from_dict(
            {"id": "s1", "timeline_start": 0.0, "duration": 1.0, "text": "あ"})
        self.assertEqual(restored.color, "")
        self.assertEqual(restored.outline_color, "")

    def test_copy_keeps_colors(self):
        clip = SubtitleClip("s1", 0.0, 1.0, "あ", color="#ABCDEF",
                            outline_color="&H00112233")
        copied = clip.copy()
        self.assertEqual(copied.color, "#ABCDEF")
        self.assertEqual(copied.outline_color, "&H00112233")


# 字幕 3 件 + 映像 1 件の Timeline (一括適用の対象確認用)
def _timeline():
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 30.0, 0.0, 30.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
                  clips=[AudioClip("a1", "c1")])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1", clips=[
        SubtitleClip("s1", 1.0, 2.0, "字幕1"),
        SubtitleClip("s2", 4.0, 2.0, "字幕2", color="#111111"),
        SubtitleClip("s3", 7.0, 2.0, "字幕3"),
    ])
    return Timeline(fps=60, source={"media_id": "m1", "duration_sec": 100.0},
                    media_pool=media, tracks=[video, audio, subtitle])


class TestEditSubtitles(unittest.TestCase):

    def setUp(self):
        self.timeline = _timeline()
        self.stack = commands.CommandStack()

    def _colors(self):
        return [self.timeline.clip_by_id(i).color for i in ("s1", "s2", "s3")]

    # 3 件へ一括適用でき、Undo 1 回で 3 件とも戻る
    def test_bulk_apply_and_single_undo(self):
        before = self._colors()
        ok = self.stack.push(
            self.timeline, commands.EditSubtitles(["s1", "s2", "s3"], color="#FFE24B"))
        self.assertTrue(ok)
        self.assertEqual(self._colors(), ["#FFE24B"] * 3)
        self.stack.undo(self.timeline)
        self.assertEqual(self._colors(), before)
        self.assertFalse(self.stack.can_undo())

    # 字幕以外 (映像・音声) や存在しない ID が混ざっていても落ちない
    def test_skips_non_subtitles(self):
        ok = self.stack.push(
            self.timeline,
            commands.EditSubtitles(["c1", "a1", "s1", "no_such"], color="#FFE24B"))
        self.assertTrue(ok)
        self.assertEqual(self.timeline.clip_by_id("s1").color, "#FFE24B")
        self.assertEqual(self.timeline.clip_by_id("s2").color, "#111111")

    # 変化が無ければ履歴を汚さない
    def test_no_change_returns_false(self):
        self.assertFalse(self.stack.push(
            self.timeline, commands.EditSubtitles(["s2"], color="#111111")))
        self.assertFalse(self.stack.push(
            self.timeline, commands.EditSubtitles([], color="#FFE24B")))
        self.assertFalse(self.stack.can_undo())

    # 一部だけ違う 3 件へ適用すると全件が指定色に揃う
    def test_mixed_values_are_unified(self):
        self.assertTrue(self.stack.push(
            self.timeline, commands.EditSubtitles(["s1", "s2", "s3"], color="#111111")))
        self.assertEqual(self._colors(), ["#111111"] * 3)

    # ラベルに件数が入る (1 件のときは従来と同じ文言)
    def test_label(self):
        self.assertEqual(commands.EditSubtitles(["s1"], color="#000000").label,
                         "字幕の編集")
        self.assertEqual(
            commands.EditSubtitles(["s1", "s2", "s3"], color="#000000").label,
            "字幕の編集（3 件）")

    # 縁の色も同じ仕組みで一括適用できる
    def test_outline_color(self):
        self.assertTrue(self.stack.push(
            self.timeline,
            commands.EditSubtitles(["s1", "s3"], outline_color="&H00202020")))
        self.assertEqual(self.timeline.clip_by_id("s1").outline_color, "&H00202020")
        self.assertEqual(self.timeline.clip_by_id("s3").outline_color, "&H00202020")


class TestResolveExportColor(unittest.TestCase):

    def _title(self, item):
        return resolve_export._title_from_item(
            item, 0.0, _profile(), (1920, 1080),
            {"width_ratio": 0.9, "height_ratio": 0.2}, "字幕1")

    # item 個別の色が title へ反映される
    def test_title_uses_item_color(self):
        title = self._title(_item(color="#FFE24B", outline_color="&H80202020"))
        self.assertEqual(title["color"], "#FFE24B")
        self.assertEqual(title["stroke_color"].upper(), "#202020")

    # 未指定なら従来どおり役割の色になる
    def test_title_falls_back_to_role_color(self):
        title = self._title(_item(role="sub"))
        self.assertEqual(title["color"], "#00FF00")

    # caption 側も同じ規則
    def test_caption_uses_item_color(self):
        caption = resolve_export._caption_from_item(
            _item(color="#FFE24B"), 0.0, _profile(), "字幕1")
        self.assertEqual(caption["color"], "#FFE24B")


if __name__ == "__main__":
    unittest.main()
