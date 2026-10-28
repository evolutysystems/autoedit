# 位置未指定のテロップへ既定アンカーを明示する (ver9 resolve8 §5) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・テンキー配置 1〜9 で \an 番号とアンカー座標が §3.2-1 の式どおりになること
#   ・explicit_position=false なら従来どおりタグが付かないこと (後方互換)
#   ・コメント役割が \an4\pos(comment_margin_l, H/2) になり、背景・アイコンとずれないこと
#   ・ドラッグ済み (pos_x/pos_y あり) の挙動は従来と変わらないこと
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.modules import comment_decor, subtitle_generator
from src.settings import settings_window as sw
from src.settings.settings_window import DEFAULT_SETTINGS

_W, _H = 1920, 1080


def _cfg(**overrides):
    cfg = dict(DEFAULT_SETTINGS["subtitle"])
    cfg.update(overrides)
    return cfg


def _profile(cfg):
    return subtitle_generator.build_font_profile(cfg)


# items から ASS を書き出して Dialogue 行の本文だけを返す
def _texts(items, cfg, subtitle_cfg=_cfg):
    with tempfile.TemporaryDirectory() as work:
        path = os.path.join(work, "out.ass")
        subtitle_generator.build_subtitle_file(
            items, _profile(cfg), path, video_width=_W, video_height=_H,
            subtitle_cfg=cfg if subtitle_cfg is _cfg else subtitle_cfg)
        with open(path, encoding="utf-8") as f:
            return [line.split(",", 9)[9]
                    for line in f.read().splitlines() if line.startswith("Dialogue: ")]


def _item(**extra):
    item = {"start": 1.0, "end": 2.0, "text": "テロップ", "role": "streamer"}
    item.update(extra)
    return item


class DefaultAnchorTest(unittest.TestCase):

    # §3.2-1 の式: 左=margin_l / 中央=(margin_l + W - margin_r)/2 / 右=W-margin_r
    #              下段=H-margin_v / 中段=H/2 / 上段=margin_v
    def test_anchor_for_each_alignment(self):
        expected = {
            1: (40.0, 1020.0), 2: (960.0, 1020.0), 3: (1880.0, 1020.0),
            4: (40.0, 540.0), 5: (960.0, 540.0), 6: (1880.0, 540.0),
            7: (40.0, 60.0), 8: (960.0, 60.0), 9: (1880.0, 60.0),
        }
        for alignment, (x, y) in expected.items():
            cfg = _cfg(alignment=alignment)
            self.assertEqual(
                subtitle_generator.default_anchor("streamer", _profile(cfg), _W, _H),
                (alignment, x, y), f"alignment={alignment}")

    # 中央揃えは左右の余白を含む (§3.2-1 の実測: 40/240 なら中心 860)
    def test_center_includes_margins(self):
        cfg = _cfg(alignment=2, margin_l=40, margin_r=240)
        _an, x, _y = subtitle_generator.default_anchor(
            "streamer", _profile(cfg), _W, _H)
        self.assertEqual(x, 860.0)

    # 範囲外・不正な配置は既定 (2 = 下段中央) に倒す
    def test_invalid_alignment_falls_back(self):
        for value in (0, 10, -1, "bogus", None):
            cfg = _cfg(alignment=value)
            self.assertEqual(
                subtitle_generator.default_anchor("streamer", _profile(cfg), _W, _H),
                (2, 960.0, 1020.0), f"alignment={value!r}")

    # 縦動画の実効設定 (余白が違う) でもそのまま式が成り立つ
    def test_vertical_canvas(self):
        cfg = _cfg(alignment=5, margin_l=40, margin_r=40, margin_v=320)
        self.assertEqual(
            subtitle_generator.default_anchor("streamer", _profile(cfg), 1080, 1920),
            (5, 540.0, 960.0))


class AssOutputTest(unittest.TestCase):

    # 位置未指定のテロップにも \an{配置}\pos(x,y) が付く
    def test_default_position_is_written(self):
        self.assertEqual(_texts([_item()], _cfg()),
                         ["{\\an2\\pos(960.0,1020.0)}テロップ"])

    # アンカーは \an5 へ寄せず、元の配置をそのまま使う (行数が変わってもずれない)
    def test_anchor_keeps_alignment_number(self):
        for alignment, tag in ((1, "{\\an1\\pos(40.0,1020.0)}"),
                               (8, "{\\an8\\pos(960.0,60.0)}")):
            texts = _texts([_item(text="1行目\\N2行目")], _cfg(alignment=alignment))
            self.assertTrue(texts[0].startswith(tag), texts[0])

    # 設定で切れば従来どおり Style の alignment/margin 任せ (タグが付かない)
    def test_disabled_by_setting(self):
        self.assertEqual(_texts([_item()], _cfg(explicit_position=False)),
                         ["テロップ"])

    # 旧 setting.json を復元した更新インストールでも既定 (true) が届く
    def test_merge_with_defaults_fills_key(self):
        merged = sw._merge_with_defaults({"subtitle": {"alignment": 2}})
        self.assertTrue(merged["subtitle"]["explicit_position"])

    # subtitle_cfg を渡さない旧来の呼び出しでは何も足さない (後方互換)
    def test_without_subtitle_cfg(self):
        self.assertEqual(_texts([_item()], _cfg(), subtitle_cfg=None), ["テロップ"])

    # ドラッグ済みのテロップは従来どおり \an5 (コメントは \an4) のまま
    def test_positioned_is_unchanged(self):
        texts = _texts([_item(pos_x=0.0, pos_y=0.0)], _cfg())
        self.assertEqual(texts, ["{\\an5\\pos(960.0,540.0)}テロップ"])

    # 役割ごとの配置 (コメントだけ中央の左) が反映される
    def test_comment_role_anchor(self):
        cfg = _cfg()
        texts = _texts([_item(role="comment", text="コメント本文")], cfg)
        # 背景 (角丸) が先に出て、本文が後ろに来る
        self.assertEqual(len(texts), 2)
        self.assertTrue(
            texts[1].startswith(
                "{\\an%d\\pos(%.1f,%.1f)}"
                % (cfg["comment_alignment"], float(cfg["comment_margin_l"]), _H / 2.0)),
            texts[1])

    # コメント本文のアンカーと、背景・アイコンが推定する文字の左端・中心が一致する
    # (§3.2-5。ここがずれると箱だけ取り残される)
    def test_comment_anchor_matches_decor(self):
        cfg = _cfg()
        item = _item(role="comment", text="コメント本文")
        _an, x, y = subtitle_generator.default_anchor("comment", _profile(cfg), _W, _H)
        extent = comment_decor.text_extent(item, cfg)
        left, mid = comment_decor._text_anchor(item, cfg, _W, _H, extent)
        self.assertEqual((x, y), (left, mid))

    # 画面外へ出ないこと (\pos はクランプされないため / §6)
    def test_anchor_inside_canvas(self):
        for alignment in range(1, 10):
            cfg = _cfg(alignment=alignment)
            _an, x, y = subtitle_generator.default_anchor(
                "streamer", _profile(cfg), _W, _H)
            self.assertTrue(0 <= x <= _W, f"alignment={alignment} x={x}")
            self.assertTrue(0 <= y <= _H, f"alignment={alignment} y={y}")


if __name__ == "__main__":
    unittest.main()
