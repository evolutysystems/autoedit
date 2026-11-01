# テロップの基準キャンバス (src/modules/subtitle_generator.reference_canvas) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/quality/resolve3.md §3 R2 の付帯対応):
#   font_size / margin / comment_icon_size_px は**絶対 px** で 1080p を前提に調整された値。
#   素材等倍 (720p) で ASS の PlayRes を実キャンバスに合わせると文字が 1.5 倍になる。
#   PlayRes を基準キャンバスへ固定し、libass の相似変換に任せれば見た目が一致する。
#   ・720p 等倍で PlayResX/Y が 1920x1080 のままになる
#   ・scale が 0.667 でアイコンへ渡る
#   ・1080p 以上では scale=1.0 で**従来と完全に同一の ASS** になる
#   ・4:3 素材で基準のアスペクト比が実キャンバスと一致する
# FFmpeg は起動しない。ASS の中身と換算倍率だけを検証する。
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import clip_writer
from src.modules import comment_decor, output_profile, subtitle_generator


def _profile(width, height, reference_height=1080):
    profile = {"is_portrait": False, "orientation": "landscape",
               "width": width, "height": height, "fps": 30}
    output_profile._attach_reference(profile, reference_height)
    return profile


class ReferenceCanvasTest(unittest.TestCase):

    # 720p 等倍では基準 1920x1080 / scale = 2/3
    def test_720p_keeps_the_1080p_reference(self):
        ref_w, ref_h, scale = subtitle_generator.reference_canvas(
            _profile(1280, 720))
        self.assertEqual((ref_w, ref_h), (1920, 1080))
        self.assertAlmostEqual(scale, 720 / 1080.0)

    # 1080p では基準 = 実キャンバス / scale=1.0 (従来と同一)
    def test_1080p_is_identity(self):
        self.assertEqual(subtitle_generator.reference_canvas(_profile(1920, 1080)),
                         (1920, 1080, 1.0))

    # 4:3 の素材では基準も 4:3 (縦横が食い違わない)
    def test_four_three_keeps_the_aspect_ratio(self):
        ref_w, ref_h, _scale = subtitle_generator.reference_canvas(
            _profile(960, 720))
        self.assertEqual((ref_w, ref_h), (1440, 1080))
        self.assertAlmostEqual(ref_w / float(ref_h), 960 / 720.0, places=3)

    # reference_* を持たない古いプロファイルでは実キャンバスをそのまま使う
    def test_legacy_profile_falls_back_to_the_canvas(self):
        self.assertEqual(
            subtitle_generator.reference_canvas({"width": 1280, "height": 720}),
            (1280, 720, 1.0))

    # プロファイルが無い呼び出しでも落ちない
    def test_without_profile(self):
        self.assertEqual(subtitle_generator.reference_canvas(None),
                         (1920, 1080, 1.0))


class AssPlayResTest(unittest.TestCase):

    def _build(self, width, height, path):
        font_profile = subtitle_generator.build_font_profile(
            {"font_family": "Yu Gothic UI", "font_size": 48})
        timeline = [{"start": 0.0, "end": 1.0, "text": "テスト", "role": "streamer"}]
        subtitle_generator.build_subtitle_file(
            timeline, font_profile, path, video_width=width, video_height=height)
        with open(path, encoding="utf-8") as f:
            return f.read()

    # 基準キャンバスを渡すと PlayRes が 1920x1080 のままになる
    def test_play_res_uses_the_reference(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ref_w, ref_h, _scale = subtitle_generator.reference_canvas(
                _profile(1280, 720))
            content = self._build(ref_w, ref_h, os.path.join(tmp, "a.ass"))
            self.assertIn("PlayResX: 1920", content)
            self.assertIn("PlayResY: 1080", content)
            # Fontsize は絶対 px のまま (libass が PlayRes → フレームへ相似変換する)
            self.assertIn(",48,", content)

    # 1080p の素材では従来とまったく同じ ASS になる
    def test_1080p_ass_is_unchanged(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ref_w, ref_h, _scale = subtitle_generator.reference_canvas(
                _profile(1920, 1080))
            reference = self._build(ref_w, ref_h, os.path.join(tmp, "ref.ass"))
            legacy = self._build(1920, 1080, os.path.join(tmp, "legacy.ass"))
            self.assertEqual(reference, legacy)


class IconScaleTest(unittest.TestCase):

    def _eff_cfg(self):
        return {"comment_icon_enabled": True, "comment_icon_size_px": 100,
                "comment_icon_gap_px": 50, "font_family": "Yu Gothic UI",
                "font_size": 48, "margin_l": 40, "margin_r": 40, "margin_v": 60}

    # 配置を基準キャンバスで計算し scale を掛けると、実キャンバスへ相似に収まる
    def test_icon_box_scales_with_the_canvas(self):
        item = {"role": "comment", "text": "コメント", "start": 0.0, "end": 1.0}
        full = comment_decor.icon_box(item, self._eff_cfg(), 1920, 1080, 1.0)
        scaled = comment_decor.icon_box(
            item, self._eff_cfg(), 1920, 1080, 720 / 1080.0)
        if full is None:
            self.skipTest("コメントアイコンが無効な構成のため判定できない")
        self.assertAlmostEqual(scaled["size"], full["size"] * (720 / 1080.0),
                               places=0)
        self.assertAlmostEqual(scaled["x"], full["x"] * (720 / 1080.0), places=0)
        self.assertAlmostEqual(scaled["y"], full["y"] * (720 / 1080.0), places=0)

    # scale=1.0 では従来と 1 px も変わらない
    def test_identity_scale_is_unchanged(self):
        item = {"role": "comment", "text": "コメント", "start": 0.0, "end": 1.0}
        raw = comment_decor.icon_box(item, self._eff_cfg(), 1920, 1080)
        scaled = comment_decor.icon_box(item, self._eff_cfg(), 1920, 1080, 1.0)
        self.assertEqual(raw, scaled)


class IntroCardCanvasTest(unittest.TestCase):

    # イントロカード / タグの ASS も PlayRes を基準キャンバスへ固定する
    def test_ass_canvas_uses_the_reference(self):
        cfg = {"output_width": 1280, "output_height": 720,
               "output_reference_height": 1080}
        self.assertEqual(clip_writer._ass_canvas(cfg, 1280, 720), (1920, 1080))

    # 1080p では実キャンバスのまま (従来と同一の ASS)
    def test_1080p_keeps_the_canvas(self):
        cfg = {"output_width": 1920, "output_height": 1080}
        self.assertEqual(clip_writer._ass_canvas(cfg, 1920, 1080), (1920, 1080))

    # 基準が取れない構成でも実キャンバスへ倒れる (落ちない)
    def test_without_reference_falls_back(self):
        self.assertEqual(clip_writer._ass_canvas({}, 1920, 1080), (1920, 1080))


if __name__ == "__main__":
    unittest.main()
