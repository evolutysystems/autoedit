# V1 (ベース映像) の拡大率・表示位置の単体テスト (docs/request/ver9/resolve6.md §7.1)
# 実行: python -m unittest discover -s tests
#
# 方針: 「100% は今の見た目のまま。それ以外は見たとおりに書き出される」
#
# 重点:
#   ・**100% では従来と 1 文字も変わらないこと。** ここが崩れると既存プロジェクトの
#     出力が全部変わる (resolve6 §3.2)。
#   ・軸ごとに crop と pad のどちらが要るかが違うこと。4:3 素材を 110% にすると
#     高さは余り (crop) 幅は足りない (pad) で、片方だけでは FFmpeg が失敗する。
#   ・本番とプロキシが**相似**であること (既存の compositor の規約)。
#   ・黒帯が出る位置指定を**モデル側で**丸めること。画面側だけで丸めると、
#     拡大率を下げたときに前の位置が範囲外のまま残る。
# FFmpeg は起動しない。文字列の組み立てと丸めだけを検証する。
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.timeline import commands, compositor, crop
from src.timeline.compositor import CanvasProfile
from src.timeline.model import (
    BASE_VIDEO_TRACK_ID,
    TRACK_VIDEO,
    Clip,
    MediaRef,
    Timeline,
    Track,
    Transform,
)

_FPS = 60
_W, _H = 1920, 1080


def _media(media_id="m1", width=1920, height=1080):
    return MediaRef(media_id, "video", __file__, 100.0, width, height, _FPS, True)


def _clip(zoom=1.0, x=0.0, y=0.0, media_id="m1"):
    clip = Clip("c1", media_id, 0.0, 5.0, 0.0)
    clip.transform = Transform(x=x, y=y, scale=zoom)
    return clip


def _timeline(media=None, width=_W, height=_H, source=None):
    media = media or _media()
    base = {"media_id": media.id, "input_path": __file__}
    base.update(source or {})
    timeline = Timeline(fps=_FPS, width=width, height=height, source=base,
                        media_pool=[media])
    track = Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, name="Video 1", is_base=True)
    timeline.tracks = [track]
    return timeline


def _profile(width=_W, height=_H, scale=1.0):
    if scale == 1.0:
        return CanvasProfile(width, height, _FPS)
    return CanvasProfile(int(width * scale), int(height * scale), _FPS,
                         scale=scale, production_width=width,
                         production_height=height)


# フィルタ列から "名前=引数" の引数部分を取り出す (無ければ None)
def _arg_of(filters, name):
    for item in filters:
        if item.startswith(name + "="):
            return item[len(name) + 1:]
    return None


class IdentityZoomTest(unittest.TestCase):
    """100% では従来の経路へそのまま落ちること (resolve6 §3.2)"""

    def setUp(self):
        self.timeline = _timeline()
        self.profile = _profile()

    # 等倍なら拡大率のフィルタは出ない
    def test_identity_returns_nothing(self):
        self.assertEqual(
            compositor.base_zoom_filters(_media(), _clip(1.0), self.profile), [])

    # 素材がキャンバスと同じで等倍なら、フィルタが 1 つも出ない (従来の性質)
    def test_matching_media_needs_no_filter(self):
        filters = compositor.still_filters(
            self.timeline, _media(), self.profile, None, clip=_clip(1.0))
        self.assertEqual(filters, [])

    # 寸法が違う素材でも、等倍なら従来の正規化チェーンのまま
    def test_normalize_chain_is_unchanged(self):
        filters = compositor.still_filters(
            self.timeline, _media(width=1280, height=720), self.profile, None,
            clip=_clip(1.0))
        self.assertEqual(filters, [
            "scale=1920:1080:force_original_aspect_ratio=decrease",
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
            "setsar=1",
            "fps=60",
            "format=yuv420p",
        ])

    # clip を渡さない呼び出し (従来の呼び出し) も変わらない
    def test_without_clip_is_unchanged(self):
        self.assertEqual(
            compositor.still_filters(self.timeline, _media(), self.profile, None), [])

    # ごく小さな差は等倍として扱う (浮動小数の往復で拡大が始まらないように)
    def test_tiny_difference_is_identity(self):
        self.assertTrue(compositor.is_identity_zoom(_clip(1.00001)))
        self.assertFalse(compositor.is_identity_zoom(_clip(1.01)))

    # 位置だけ入っていても等倍なら何もしない (等倍では動かせない / §3.4)
    def test_identity_with_position_returns_nothing(self):
        self.assertEqual(
            compositor.base_zoom_filters(_media(), _clip(1.0, 0.5, 0.5),
                                         self.profile), [])


class ZoomFilterTest(unittest.TestCase):
    """拡大・縮小のフィルタが出力の寸法をキャンバスへ揃えること (§3.3)"""

    def setUp(self):
        self.profile = _profile()

    # 拡大: 収めた寸法の z 倍へ広げ、キャンバスの大きさで切る
    def test_zoom_in_crops_to_the_canvas(self):
        filters = compositor.base_zoom_filters(_media(), _clip(1.5), self.profile)
        self.assertEqual(_arg_of(filters, "scale"), "2880:1620")
        self.assertEqual(_arg_of(filters, "crop"), "1920:1080:480:270")
        self.assertIsNone(_arg_of(filters, "pad"))

    # 縮小: 収めた寸法の z 倍へ縮め、余白を黒で埋める
    def test_zoom_out_pads_with_black(self):
        filters = compositor.base_zoom_filters(_media(), _clip(0.5), self.profile)
        self.assertEqual(_arg_of(filters, "scale"), "960:540")
        self.assertEqual(_arg_of(filters, "pad"), "1920:1080:480:270:black")
        self.assertIsNone(_arg_of(filters, "crop"))

    # 本題: 軸ごとに crop と pad の要否が違う場合 (4:3 素材の 110%)。
    # 片方だけで済ませると FFmpeg が「入力が余白の内に収まらない」と失敗する。
    def test_mixed_axes_need_both_crop_and_pad(self):
        filters = compositor.base_zoom_filters(
            _media(width=1440, height=1080), _clip(1.1), self.profile)
        self.assertEqual(_arg_of(filters, "scale"), "1584:1188")
        self.assertEqual(_arg_of(filters, "crop"), "1584:1080:0:54")
        self.assertEqual(_arg_of(filters, "pad"), "1920:1080:168:0:black")

    # 縦素材を拡大しても高さだけ切られ、幅は余白で埋まる
    def test_portrait_media_keeps_side_padding(self):
        filters = compositor.base_zoom_filters(
            _media(width=1080, height=1920), _clip(1.5), self.profile)
        self.assertEqual(_arg_of(filters, "crop").split(":")[1], "1080")
        self.assertTrue(_arg_of(filters, "pad").startswith("1920:1080:"))

    # 正規化の後段 (sar / fps / 画素形式) は従来と同じものを残す
    def test_normalize_tail_is_kept(self):
        filters = compositor.base_zoom_filters(_media(), _clip(1.5), self.profile)
        self.assertEqual(filters[-3:], ["setsar=1", "fps=60", "format=yuv420p"])

    # 幅と高さはすべて偶数 (yuv420p は奇数の幅・高さを扱えない)。
    # **座標 (3・4 番目) は対象外。** 座標まで偶数へ丸めると、プロキシとの相似が
    # 崩れるため整数丸めにしてある (compositor._clamp_int の注記)。
    def test_sizes_are_even(self):
        for zoom in (1.07, 1.33, 0.77, 2.95):
            for media in (_media(), _media(width=1440, height=1080),
                          _media(width=1366, height=768)):
                filters = compositor.base_zoom_filters(media, _clip(zoom), self.profile)
                numbers = []
                for name in ("scale", "crop", "pad"):
                    arg = _arg_of(filters, name)
                    if arg:
                        numbers += [int(v) for v in arg.split(":")[:2] if v.isdigit()]
                odd = [v for v in numbers if v % 2]
                self.assertEqual(odd, [], f"zoom={zoom} に奇数の寸法: {filters}")

    # 縦横比が素材と一致すること (縦横比を崩す指定はそもそも与えられない)
    def test_aspect_ratio_is_kept(self):
        for width, height in ((1920, 1080), (1440, 1080), (1080, 1920), (1280, 720)):
            filters = compositor.base_zoom_filters(
                _media(width=width, height=height), _clip(1.5), self.profile)
            scaled = [int(v) for v in _arg_of(filters, "scale").split(":")]
            self.assertAlmostEqual(scaled[0] / scaled[1], width / height, places=2)

    # 素材の寸法が分からなければ拡大しない (倍率の基準が決まらない)
    def test_unknown_media_size_is_skipped(self):
        self.assertEqual(
            compositor.base_zoom_filters(_media(width=0, height=0), _clip(1.5),
                                         self.profile), [])

    # 収めた寸法は scale=…:force_original_aspect_ratio=decrease と同じ結果になる
    def test_fit_size_matches_the_normalize_chain(self):
        self.assertEqual(compositor.fit_size(_media(), self.profile), (1920, 1080))
        self.assertEqual(compositor.fit_size(_media(width=1440, height=1080),
                                             self.profile), (1440, 1080))
        self.assertEqual(compositor.fit_size(_media(width=1280, height=720),
                                             self.profile), (1920, 1080))
        self.assertEqual(compositor.fit_size(_media(width=0, height=0),
                                             self.profile), (0, 0))


class ZoomPositionTest(unittest.TestCase):
    """表示位置が切り出す場所 / 置く場所へ効くこと (§3.4)"""

    def setUp(self):
        self.profile = _profile()

    # 拡大時: 右へずらすと切り出し位置は左へ動く (映像が右へ寄って見える)
    def test_zoom_in_position_moves_the_crop_window(self):
        right = compositor.base_zoom_filters(_media(), _clip(1.5, 0.5, 0.0),
                                             self.profile)
        left = compositor.base_zoom_filters(_media(), _clip(1.5, -0.5, 0.0),
                                            self.profile)
        self.assertEqual(_arg_of(right, "crop"), "1920:1080:0:270")
        self.assertEqual(_arg_of(left, "crop"), "1920:1080:960:270")

    # Y は上が正 (model.py の規約)。上へずらすと切り出しは下へ動く
    def test_y_is_positive_upward(self):
        up = compositor.base_zoom_filters(_media(), _clip(1.5, 0.0, 0.5), self.profile)
        down = compositor.base_zoom_filters(_media(), _clip(1.5, 0.0, -0.5),
                                            self.profile)
        self.assertEqual(_arg_of(up, "crop"), "1920:1080:480:540")
        self.assertEqual(_arg_of(down, "crop"), "1920:1080:480:0")

    # 縮小時: 右へずらすと置く位置も右へ動く (符号はそのまま)
    def test_zoom_out_position_moves_the_pad_origin(self):
        right = compositor.base_zoom_filters(_media(), _clip(0.5, 0.5, 0.0),
                                             self.profile)
        self.assertEqual(_arg_of(right, "pad"), "1920:1080:960:270:black")

    # 範囲を越える指定でも crop / pad の座標は範囲内へ収まる (負や超過を出さない)
    def test_extreme_position_is_clamped_in_the_filter(self):
        for x, y in ((9.0, 9.0), (-9.0, -9.0)):
            for zoom in (1.5, 0.5):
                filters = compositor.base_zoom_filters(
                    _media(), _clip(zoom, x, y), self.profile)
                arg = _arg_of(filters, "crop") or _arg_of(filters, "pad")
                values = [int(v) for v in arg.split(":")[2:4]]
                self.assertTrue(all(v >= 0 for v in values), filters)


class ProxySimilarityTest(unittest.TestCase):
    """本番とプロキシが相似であること (既存 compositor の規約)"""

    # 幾何の数値が半分になる。
    # 座標は整数しか取れないため、**丸めで 1px までずれる**ことは許す
    # (本番 135 の半分は 67.5)。寸法は偶数で作っているので厳密に半分になる。
    def _assert_similar(self, production, proxy, name):
        prod_values = [int(v) for v in _arg_of(production, name).split(":")
                       if v.isdigit()]
        proxy_values = [int(v) for v in _arg_of(proxy, name).split(":") if v.isdigit()]
        self.assertEqual(len(prod_values), len(proxy_values), name)
        for index, (prod, small) in enumerate(zip(prod_values, proxy_values)):
            if index < 2:
                self.assertEqual(prod // 2, small, f"{name}[{index}] (寸法)")
            else:
                self.assertLessEqual(abs(prod / 2.0 - small), 1.0,
                                     f"{name}[{index}] (座標)")

    def test_proxy_is_similar(self):
        clip = _clip(1.5, 0.5, -0.25)
        self._assert_similar(
            compositor.base_zoom_filters(_media(), clip, _profile()),
            compositor.base_zoom_filters(_media(), clip, _profile(scale=0.5)),
            "scale")
        self._assert_similar(
            compositor.base_zoom_filters(_media(), clip, _profile()),
            compositor.base_zoom_filters(_media(), clip, _profile(scale=0.5)),
            "crop")

    # 縮小でも相似
    def test_proxy_is_similar_when_shrinking(self):
        clip = _clip(0.5)
        self._assert_similar(
            compositor.base_zoom_filters(_media(), clip, _profile()),
            compositor.base_zoom_filters(_media(), clip, _profile(scale=0.5)),
            "pad")


class CropLayoutTest(unittest.TestCase):
    """縦動画の切り抜きとは併用しないこと (§3.6)"""

    # 切り抜き指定があるときは切り抜きのチェーンだけが返り、拡大率は見ない
    def test_crop_layout_wins(self):
        media = _media(width=1920, height=1080)
        layout = crop.make_layout(
            crop.MODE_SINGLE, [(420, 0, 1080, 1080)], media, 1080, 1920,
            background=crop.BG_BLUR)
        timeline = _timeline(media, width=1080, height=1920,
                             source={crop.KEY: layout})
        profile = _profile(1080, 1920)
        with_zoom = compositor.still_filters(timeline, media, profile, {},
                                             clip=_clip(1.5))
        without_zoom = compositor.still_filters(timeline, media, profile, {},
                                                clip=_clip(1.0))
        # 切り抜きが正規化を兼ねるため 1 本だけ。拡大率の有無で結果が変わらない
        self.assertEqual(len(with_zoom), 1)
        self.assertEqual(with_zoom, without_zoom)


class PreviewMatchesOutputTest(unittest.TestCase):
    """プレビューの配置と出力の配置が一致すること (= 見たとおりに書き出される)

    プレビューは Qt の setScale / setPos で近似表示し、出力は crop / pad で切る。
    **別の式で同じ絵にしなければならない**ため、ここで数値を突き合わせる。
    下の式は preview_panel._on_frame_ready と同じもので、あちらを変えるときは
    ここも必ず合わせること (食い違うと「見たとおりに出ない」が再発する)。
    """

    def setUp(self):
        self.profile = _profile()

    # 出力側の「拡大した映像の左上がキャンバス座標のどこに来るか」
    #
    # crop で切った位置ぶん左 (上) へ出て、pad で置いた位置ぶん右 (下) へ寄る。
    # 片方しか出ないこともあるため、両方を足して求める。
    def _output_origin(self, filters):
        sizes = {}
        for item in filters:
            name, _, arg = item.partition("=")
            if name in ("scale", "crop", "pad"):
                sizes[name] = arg.split(":")
        target = [int(v) for v in sizes["scale"][:2]]
        crop_x, crop_y = ((int(sizes["crop"][2]), int(sizes["crop"][3]))
                          if "crop" in sizes else (0, 0))
        pad_x, pad_y = ((int(sizes["pad"][2]), int(sizes["pad"][3]))
                        if "pad" in sizes else (0, 0))
        return (pad_x - crop_x, pad_y - crop_y), target

    # プレビュー側の配置 (preview_panel._on_frame_ready と同じ式)
    def _preview_origin(self, media, clip, target):
        offset_x, offset_y = compositor.base_offset_px(media, clip, self.profile)
        return ((self.profile.width - target[0]) / 2.0 + offset_x,
                (self.profile.height - target[1]) / 2.0 + offset_y)

    # 比較は「実際に保存される位置」で行う。可動範囲の丸めはモデル側の責任で
    # (§3.4)、範囲外の値がクリップへ入ることはないため。
    def _clamped_clip(self, media, zoom, x, y):
        x, y = commands.clamp_base_offset(_timeline(media), media, zoom, x, y)
        return _clip(zoom, x, y)

    def test_preview_and_output_place_the_frame_alike(self):
        media = _media()
        for zoom, x, y in ((1.5, 0.0, 0.0), (1.5, 0.5, 0.0), (1.5, 0.0, 0.5),
                           (2.0, -0.3, 0.2), (0.5, 0.0, 0.0), (0.5, 0.4, -0.4),
                           (1.1, 0.2, -0.2), (2.9, -1.0, 1.0)):
            clip = self._clamped_clip(media, zoom, x, y)
            filters = compositor.base_zoom_filters(media, clip, self.profile)
            output, target = self._output_origin(filters)
            preview = self._preview_origin(media, clip, target)
            self.assertLessEqual(abs(output[0] - preview[0]), 1.0,
                                 f"z={zoom} ({x},{y}) の X がずれる")
            self.assertLessEqual(abs(output[1] - preview[1]), 1.0,
                                 f"z={zoom} ({x},{y}) の Y がずれる")

    # 縦横比の違う素材でも一致すること
    def test_matches_for_other_aspect_ratios(self):
        for width, height in ((1440, 1080), (1080, 1920), (1280, 720)):
            media = _media(width=width, height=height)
            for zoom in (1.5, 0.8, 1.1):
                clip = self._clamped_clip(media, zoom, 0.3, -0.3)
                filters = compositor.base_zoom_filters(media, clip, self.profile)
                output, target = self._output_origin(filters)
                preview = self._preview_origin(media, clip, target)
                self.assertLessEqual(abs(output[0] - preview[0]), 1.0,
                                     f"{width}x{height} z={zoom} の X がずれる")
                self.assertLessEqual(abs(output[1] - preview[1]), 1.0,
                                     f"{width}x{height} z={zoom} の Y がずれる")


class ClampOffsetTest(unittest.TestCase):
    """黒帯が出る位置をモデル側で丸めること (§3.4)"""

    def setUp(self):
        self.timeline = _timeline()
        self.media = _media()

    # 拡大時の限界は「はみ出しの半分」= 正規化で (fit*z - W) / W
    def test_zoom_in_limit(self):
        x, y = commands.clamp_base_offset(self.timeline, self.media, 1.5, 9.0, 9.0)
        self.assertAlmostEqual(x, 0.5, places=6)
        self.assertAlmostEqual(y, 0.5, places=6)

    # 縮小時の限界は「余白の半分」
    def test_zoom_out_limit(self):
        x, y = commands.clamp_base_offset(self.timeline, self.media, 0.5, 9.0, -9.0)
        self.assertAlmostEqual(x, 0.5, places=6)
        self.assertAlmostEqual(y, -0.5, places=6)

    # 等倍では動かせない (限界が 0)
    def test_identity_has_no_room(self):
        self.assertEqual(
            commands.clamp_base_offset(self.timeline, self.media, 1.0, 0.5, 0.5),
            (0.0, 0.0))

    # 範囲内の指定はそのまま通る
    def test_inside_the_range_is_kept(self):
        x, y = commands.clamp_base_offset(self.timeline, self.media, 1.5, 0.2, -0.3)
        self.assertAlmostEqual(x, 0.2, places=6)
        self.assertAlmostEqual(y, -0.3, places=6)

    # 素材の寸法が分からなければ丸めない (操作を封じるより通すほうが害が小さい)
    def test_unknown_media_size_is_not_clamped(self):
        x, y = commands.clamp_base_offset(
            self.timeline, _media(width=0, height=0), 1.5, 9.0, 9.0)
        self.assertEqual((x, y), (9.0, 9.0))

    # 縦横比が違う素材では軸ごとに限界が違う (4:3 を 110%)
    def test_limits_differ_per_axis(self):
        x, y = commands.clamp_base_offset(
            self.timeline, _media(width=1440, height=1080), 1.1, 9.0, 9.0)
        # 幅 1584 < 1920 なので余白側、高さ 1188 > 1080 なのではみ出し側
        self.assertAlmostEqual(x, (1920 - 1584) / 1920, places=4)
        self.assertAlmostEqual(y, (1188 - 1080) / 1080, places=4)


if __name__ == "__main__":
    unittest.main()
