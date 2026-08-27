# コメント役割の配置・ラベル撤去・背景行の生成 (ver3 resolve11 §10.1) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・Comment の Style だけ配置が変わり、Streamer/Sub は現行と 1 文字も変わらないこと
#   ・コメントの \pos が左中央アンカー (\an4)、他の役割は従来どおり \an5 であること
#   ・subtitle_cfg を渡さない呼び出しでは背景も Layer 変更も起きないこと (後方互換)
import os
import tempfile
import unittest

from src.modules import subtitle_generator
from src.settings.settings_window import DEFAULT_SETTINGS


def _cfg(**overrides):
    cfg = dict(DEFAULT_SETTINGS["subtitle"])
    cfg.update(overrides)
    return cfg


def _profile(cfg=None):
    return subtitle_generator.build_font_profile(cfg or _cfg())


# items から ASS を書き出して行を返す
def _lines(items, cfg=None, subtitle_cfg=None):
    with tempfile.TemporaryDirectory() as work:
        path = os.path.join(work, "out.ass")
        subtitle_generator.build_subtitle_file(
            items, _profile(cfg), path, video_width=1920, video_height=1080,
            subtitle_cfg=subtitle_cfg)
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()


def _styles(lines):
    return {line.split(",")[0][len("Style: "):]: line
            for line in lines if line.startswith("Style: ")}


def _dialogues(lines):
    return [line for line in lines if line.startswith("Dialogue: ")]


_COMMENT = {"start": 1.0, "end": 2.0, "text": "コメント本文", "role": "comment"}
_STREAMER = {"start": 3.0, "end": 4.0, "text": "通常の字幕", "role": "streamer"}


class RolePlacementTest(unittest.TestCase):

    # Comment だけ Alignment=4 / MarginL=190 になる (§6.1)
    def test_comment_style_uses_own_placement(self):
        styles = _styles(_lines([_COMMENT]))
        fields = styles["Comment"].split(",")
        self.assertEqual(fields[-5], "4")       # Alignment
        self.assertEqual(fields[-4], "190")     # MarginL

    # 配信者・サブは現行と完全に一致する (回帰の要)
    def test_other_styles_unchanged(self):
        styles = _styles(_lines([_COMMENT]))
        for name in ("Streamer", "Sub"):
            fields = styles[name].split(",")
            self.assertEqual(fields[-5], "2")    # Alignment
            self.assertEqual(fields[-4], "40")   # MarginL
            self.assertEqual(fields[-2], "60")   # MarginV

    # 左寄せ以外を指定したら既定 (4) へ落ちる。アイコン位置が確定しなくなるため。
    def test_invalid_comment_alignment_falls_back(self):
        styles = _styles(_lines([_COMMENT], cfg=_cfg(comment_alignment=2)))
        self.assertEqual(styles["Comment"].split(",")[-5], "4")

    # 縦動画では縦タブの値へ差し替わる (§7.2)
    def test_vertical_override(self):
        eff = subtitle_generator.build_effective_subtitle_cfg(
            dict(DEFAULT_SETTINGS["subtitle"]), dict(DEFAULT_SETTINGS["vertical"]),
            {"is_portrait": True})
        self.assertEqual(eff["comment_margin_l"], 140)
        self.assertEqual(eff["comment_icon_size_px"], 50)
        placement = _profile(eff).placement_for_role("comment")
        self.assertEqual(placement[0], 4)
        self.assertEqual(placement[1], 140)


class PositionAnchorTest(unittest.TestCase):

    # コメントは左中央アンカー (\an4)
    def test_comment_uses_left_anchor(self):
        item = dict(_COMMENT, pos_x=0.0, pos_y=0.0)
        line = _dialogues(_lines([item]))[0]
        self.assertIn("\\an4\\pos(960.0,540.0)", line)

    # 他の役割は従来どおり中央アンカー (\an5)
    def test_other_roles_keep_center_anchor(self):
        item = dict(_STREAMER, pos_x=0.0, pos_y=0.0)
        line = _dialogues(_lines([item]))[0]
        self.assertIn("\\an5\\pos(960.0,540.0)", line)


class CommentLabelTest(unittest.TestCase):

    # 既定ではラベルが付かない (C3)
    def test_no_label_by_default(self):
        line = _dialogues(_lines([_COMMENT]))[0]
        self.assertNotIn("コメント：", line)

    # 設定で戻せる (撤去はコード削除ではなく既定値の変更で行っているため)
    def test_label_can_be_restored(self):
        line = _dialogues(_lines([_COMMENT], cfg=_cfg(comment_label="コメント：")))[0]
        self.assertIn("コメント：\\Nコメント本文", line)


class BackgroundEventTest(unittest.TestCase):

    # コメント 1 件につき背景行が 1 本増え、Layer は背景 0 / 本文 1 になる
    def test_background_and_layers(self):
        lines = _dialogues(_lines([_COMMENT, _STREAMER], subtitle_cfg=_cfg()))
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("Dialogue: 0,"))
        self.assertIn("\\p1}", lines[0])                   # 背景 (ベクター描画)
        self.assertTrue(lines[1].startswith("Dialogue: 1,"))
        self.assertIn("コメント本文", lines[1])
        # 配信者の行は背景なし・Layer 0 のまま
        self.assertTrue(lines[2].startswith("Dialogue: 0,"))
        self.assertIn("通常の字幕", lines[2])

    # subtitle_cfg を渡さない従来の呼び出しでは背景も Layer 変更も無い
    def test_legacy_call_is_unchanged(self):
        lines = _dialogues(_lines([_COMMENT, _STREAMER]))
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.startswith("Dialogue: 0,") for line in lines))

    # 背景を切れば行は増えないが、本文の Layer は設定どおりになる
    def test_background_disabled(self):
        lines = _dialogues(_lines([_COMMENT],
                                  subtitle_cfg=_cfg(comment_bg_enabled=False)))
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("Dialogue: 1,"))

    # Layer が逆転していたら本文を背景より上へ引き上げる
    def test_layers_are_corrected_when_inverted(self):
        lines = _dialogues(_lines(
            [_COMMENT], subtitle_cfg=_cfg(comment_bg_layer=5, comment_text_layer=1)))
        self.assertTrue(lines[0].startswith("Dialogue: 5,"))
        self.assertTrue(lines[1].startswith("Dialogue: 6,"))


if __name__ == "__main__":
    unittest.main()
