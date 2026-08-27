# コメント字幕の装飾 (背景ボックス + アイコン) の単体テスト
# (docs/request/ver3/resolve11.md §10.1)
# 実行: python -m unittest discover -s tests
# 重点:
#   ・設計書 §6.1 / §6.1.1 に書いた数値がそのまま出ること (横 / 縦)
#   ・キャンバスからはみ出さないこと (要望「画像がはみ出さないように注意」)
#   ・アイコンを切っても背景だけが正しく縮むこと
#   ・ffmpeg のフィルタが「入力を増やさず・位置ごとにまとめる」形になっていること
import unittest

from src.modules import comment_decor, subtitle_generator
from src.settings.settings_window import DEFAULT_SETTINGS


# 既定設定の字幕セクション (横動画)
def _cfg(**overrides):
    cfg = dict(DEFAULT_SETTINGS["subtitle"])
    cfg.update(overrides)
    return cfg


# 既定設定を縦動画プロファイルへ寄せた実効設定
def _vertical_cfg():
    return subtitle_generator.build_effective_subtitle_cfg(
        dict(DEFAULT_SETTINGS["subtitle"]), dict(DEFAULT_SETTINGS["vertical"]),
        {"is_portrait": True})


# 2 行・全角 14 文字 (最長行) のコメント item
def _comment(text="ここにコメント本文が入ります\\Nもう一行だけ", **extra):
    item = {"start": 3.1, "end": 5.4, "text": text, "role": "comment", "use": True}
    item.update(extra)
    return item


class CommentIconTest(unittest.TestCase):

    def setUp(self):
        comment_decor.reset_warnings()

    # 横 1920x1080 の既定値。設計書 §6.1 の「アイコン左端 40px / 垂直中央」
    def test_icon_box_landscape_defaults(self):
        box = comment_decor.icon_box(_comment(), _cfg(), 1920, 1080)
        self.assertEqual((box["x"], box["y"], box["size"]), (40.0, 490.0, 100))
        self.assertFalse(box["clamped"])

    # 縦 1080x1920 は 50x50 (Q7 の回答)。左端はやはり 40px に揃う (§6.1.1)
    def test_icon_box_portrait_defaults(self):
        box = comment_decor.icon_box(_comment(), _vertical_cfg(), 1080, 1920)
        self.assertEqual((box["x"], box["y"], box["size"]), (40.0, 935.0, 50))

    # 左余白を詰めてもキャンバス外へ出ない (クランプ)
    def test_icon_never_leaves_canvas(self):
        box = comment_decor.icon_box(_comment(), _cfg(comment_margin_l=10), 1920, 1080)
        self.assertEqual(box["x"], 0.0)
        self.assertTrue(box["clamped"])

    # 位置指定 (ドラッグ済み) では pos_x がそのまま文字の左端として扱われる
    def test_icon_uses_positioned_left_edge(self):
        item = _comment(pos_x=0.0, pos_y=0.0)   # キャンバス中心
        box = comment_decor.icon_box(item, _cfg(), 1920, 1080)
        # 左端 960 - 間隔 50 - アイコン 100 = 810 / 垂直中心 540 - 50 = 490
        self.assertEqual((box["x"], box["y"]), (810.0, 490.0))

    # コメント以外の役割には付けない
    def test_no_icon_for_other_roles(self):
        item = _comment()
        item["role"] = "streamer"
        self.assertIsNone(comment_decor.icon_box(item, _cfg(), 1920, 1080))

    # 設定で切れる
    def test_icon_disabled(self):
        self.assertIsNone(comment_decor.icon_box(
            _comment(), _cfg(comment_icon_enabled=False), 1920, 1080))


class CommentIconChainTest(unittest.TestCase):

    def setUp(self):
        comment_decor.reset_warnings()

    # 同じ位置のコメントは 1 グループへまとまり、enable が論理和で連結される
    def test_same_position_is_one_overlay(self):
        items = [_comment(), dict(_comment(), start=9.0, end=11.2)]
        chains, count, groups = comment_decor.build_icon_chains(
            items, _cfg(), 1920, 1080, "[vsub]", "")
        self.assertEqual((count, groups), (2, 1))
        # movie ソース 1 本 + overlay 1 本 = 入力 (-i) は 1 つも増えない
        self.assertEqual(len(chains), 2)
        self.assertTrue(chains[0].startswith("movie="))
        self.assertIn("between(t,3.100,5.400)+between(t,9.000,11.200)", chains[1])
        self.assertIn("overlay=40.0:490.0", chains[1])

    # 位置が違えば split して overlay を鎖状に足す
    def test_different_positions_are_split(self):
        items = [_comment(), _comment(pos_x=0.0, pos_y=0.0)]
        chains, count, groups = comment_decor.build_icon_chains(
            items, _cfg(), 1920, 1080, "[vsub]", "")
        self.assertEqual((count, groups), (2, 2))
        self.assertIn("split=2", chains[0])
        self.assertEqual(len(chains), 3)

    # 対象が無ければ何も足さない (= 従来と完全に同一のコマンドになる)
    def test_no_comment_no_chain(self):
        item = _comment()
        item["role"] = "streamer"
        chains, count, groups = comment_decor.build_icon_chains(
            [item], _cfg(), 1920, 1080, "[vsub]", "")
        self.assertEqual((chains, count, groups), ([], 0, 0))

    # 静止画プレビュー用に表示時間を外せる
    def test_without_enable(self):
        chains, _count, _groups = comment_decor.build_icon_chains(
            [_comment()], _cfg(), 1920, 1080, "[vsub]", "", with_enable=False)
        self.assertNotIn("enable=", chains[1])


class CommentBackgroundTest(unittest.TestCase):

    def setUp(self):
        comment_decor.reset_warnings()

    # 文字幅の推定 (既定係数は Yu Gothic UI の実測に合わせてある)
    def test_text_extent_estimation(self):
        extent = comment_decor.text_extent(_comment(), _cfg())
        # 最長行は 14 文字 x 0.78 x 48px = 524.16 → 525 / 2 行 x 48 x 1.2 → 116
        self.assertEqual((extent["width"], extent["height"], extent["lines"]),
                         (525, 116, 2))

    def test_half_width_characters(self):
        extent = comment_decor.text_extent(_comment(text="abcd"), _cfg())
        self.assertEqual(extent["width"], 87)      # 4 文字 x 0.45 x 48 = 86.4

    # 係数を上げれば箱も広がる (等幅フォントへ切り替えたときの調整口)
    def test_width_coefficient_is_configurable(self):
        extent = comment_decor.text_extent(
            _comment(), _cfg(comment_bg_char_width_full=1.0))
        self.assertEqual(extent["width"], 672)     # 14 文字 x 1.0 x 48

    # 設計書 §6.1 の箱 (左端 16 / 角丸 24)
    def test_background_box_landscape(self):
        box = comment_decor.background_box(_comment(), _cfg(), 1920, 1080)
        self.assertEqual(box["x"], 16.0)           # アイコン左端 40 - 余白 24
        self.assertEqual(box["y"], 458.0)          # 540 - 58(高さの半分) - 24
        self.assertEqual(box["w"], 723.0)          # (文字右端 715 - 40) + 24*2
        self.assertEqual(box["h"], 164.0)          # 58*2 + 24*2
        self.assertEqual(box["radius"], 24)
        self.assertFalse(box["clamped"])

    # アイコンを切ると箱は文字だけを囲う (左端 = 文字左 190 - 余白 24)
    def test_background_without_icon(self):
        box = comment_decor.background_box(
            _comment(), _cfg(comment_icon_enabled=False), 1920, 1080)
        self.assertEqual(box["x"], 166.0)

    # 角丸半径は短辺の半分を超えない
    def test_radius_is_capped(self):
        box = comment_decor.background_box(
            _comment(text="あ"), _cfg(comment_bg_radius_px=999), 1920, 1080)
        self.assertLessEqual(box["radius"], box["h"] / 2.0)

    # キャンバスに収まらないときは詰める (縦動画の既定値で起きる / §9.0-b)
    def test_background_clamped_when_too_wide(self):
        box = comment_decor.background_box(_comment(), _vertical_cfg(), 1080, 1920)
        self.assertTrue(box["clamped"])
        self.assertLessEqual(box["x"] + box["w"], 1080)

    def test_background_disabled(self):
        self.assertIsNone(comment_decor.background_box(
            _comment(), _cfg(comment_bg_enabled=False), 1920, 1080))

    # 角丸矩形の描画コマンド (始点・ベジェ・直角のとき)
    def test_rounded_rect_path(self):
        path = comment_decor.rounded_rect_path(200, 100, 20)
        self.assertTrue(path.startswith("m 20 0 "))
        self.assertIn("b 200 0 200 0 200 20", path)
        self.assertEqual(comment_decor.rounded_rect_path(200, 100, 0),
                         "m 0 0 l 200 0 l 200 100 l 0 100")

    # 背景の Dialogue 本文 (黒・透明度 50% / 縁と影は消す)
    def test_background_text_tags(self):
        text = comment_decor.build_background_text(
            _comment(), _cfg(), 1920, 1080)
        self.assertIn("\\an7\\pos(16.0,458.0)", text)
        self.assertIn("\\1c&H000000&", text)
        self.assertIn("\\1a&H80&", text)
        self.assertIn("\\bord0\\shad0", text)
        self.assertIn("\\p1}", text)
        self.assertTrue(text.endswith("{\\p0}"))


if __name__ == "__main__":
    unittest.main()
