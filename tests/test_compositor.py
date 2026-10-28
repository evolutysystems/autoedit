# 合成断片の組み立て (src/timeline/compositor.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver6/resolve3.md §8.1):
#   ・本番とプロキシのグラフが**相似**であること (幾何の数値がすべて scale 倍)
#   ・ASS だけは換算しないこと (PlayRes = 本番寸法のまま libass が追従する / §3.2)
#   ・scale=1.0 の文字列が従来 (renderer の自前計算) と**完全一致**すること。
#     これが通る限り「出力側の挙動は 1 文字も変わっていない」と言える (§9 Phase 1)
#   ・タイル境界で enable / setpts がタイル開始ぶんずれること (§8.1 proxy-6)
# FFmpeg は起動しない。文字列の組み立てだけを検証する。
import os
import re
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.modules import comment_decor, subtitle_generator, watermark_overlay
from src.timeline import compositor, crop
from src.timeline.compositor import CanvasProfile
from src.timeline.model import Clip, MediaRef, Timeline, Transform

_FPS = 60
_PROD_W, _PROD_H = 1920, 1080


def _media(media_id="m1", width=1920, height=1080, kind="video", has_audio=True):
    return MediaRef(media_id, kind, __file__, 100.0, width, height, _FPS, has_audio)


def _image(media_id="img1", width=800, height=600):
    return MediaRef(media_id, "image", __file__, None, width, height, 0.0, False)


def _timeline(media=None, width=_PROD_W, height=_PROD_H, source=None):
    media = media or _media()
    base = {"media_id": media.id, "input_path": __file__}
    base.update(source or {})
    return Timeline(fps=_FPS, width=width, height=height, source=base,
                    media_pool=[media])


def _clip(media_id="m1", timeline_start=2.0, duration=3.0, source_in=5.0,
          scale=0.5, x=0.0, y=0.0, opacity=1.0):
    return Clip("c1", media_id, timeline_start, duration, source_in,
                transform=Transform(x=x, y=y, scale=scale, opacity=opacity))


# V1 (ベース映像) のクリップ。**拡大率は等倍** (ver9 resolve6)。
# _clip() の既定 scale=0.5 はオーバーレイ用 (キャンバス幅に対する比率) で、
# ベース映像では「収めた状態の 50%」= 縮小の意味になってしまう。
def _base_clip(**kwargs):
    kwargs.setdefault("scale", 1.0)
    return _clip(**kwargs)


def _comment_item(text="コメント本文", start=0.0, end=2.0):
    return {"start": start, "end": end, "text": text, "use": True, "role": "comment"}


def _subtitle_cfg():
    return subtitle_generator.build_effective_subtitle_cfg({}, {}, None)


# filtergraph からパス (movie='…' / ass='…') を抜いた文字列を返す。
# パスに含まれる数字を幾何の比較へ混ぜないため。
def _strip_paths(text):
    text = re.sub(r"movie='[^']*'", "movie=''", text)
    return re.sub(r"ass='[^']*'", "ass=''", text)


# 幾何を表す数値だけを取り出す (scale / pad / overlay / crop / boxblur / gblur)
# 戻り値: [(フィルタ名, 値), ...] の順序付きリスト
_GEOMETRY = re.compile(
    r"(?:(scale|pad|overlay|crop)=((?:-?\d+(?:\.\d+)?)(?::(?:-?\d+(?:\.\d+)?))?)"
    r"|(boxblur)=(\d+)|(?:(gblur)=sigma=(\d+(?:\.\d+)?)))")


def _geometry_numbers(graph):
    found = []
    for match in _GEOMETRY.finditer(_strip_paths(graph)):
        name = match.group(1) or match.group(3) or match.group(5)
        raw = match.group(2) or match.group(4) or match.group(6)
        for part in str(raw).split(":"):
            found.append((name, float(part)))
    return found


class CanvasProfileTest(unittest.TestCase):

    def test_production_keeps_canvas_and_scale(self):
        profile = CanvasProfile.production(_timeline(), {"output_fps": _FPS})

        self.assertEqual(profile.size(), (_PROD_W, _PROD_H))
        self.assertEqual(profile.fps, _FPS)
        self.assertEqual(profile.scale, 1.0)
        self.assertTrue(profile.is_production())

    # プロキシは解像度だけを落とし、fps は本番と同じにする (§3.2)
    def test_proxy_halves_the_canvas_but_keeps_fps(self):
        timeline = _timeline(width=1080, height=1920)
        profile = CanvasProfile.proxy(timeline, {"output_fps": _FPS}, 0.5)

        self.assertEqual(profile.size(), (540, 960))
        self.assertEqual(profile.fps, _FPS, "fps を変えるとフレーム量子化が崩れる (§3.2)")
        self.assertEqual(profile.scale, 0.5)
        self.assertFalse(profile.is_production())

    # 本番キャンバスの寸法を保持する (絶対 px の設定値を本番基準で計算するため / §3.3)
    def test_proxy_remembers_the_production_canvas(self):
        profile = CanvasProfile.proxy(_timeline(), {"output_fps": _FPS}, 0.5)

        self.assertEqual((profile.production_width, profile.production_height),
                         (_PROD_W, _PROD_H))
        self.assertEqual(profile.px(100), 50)

    # 寸法は偶数へ丸める (yuv420p は奇数を扱えない)
    def test_proxy_rounds_to_even(self):
        profile = CanvasProfile.proxy(_timeline(width=1921, height=1081),
                                      {"output_fps": _FPS}, 0.5)

        self.assertEqual(profile.width % 2, 0)
        self.assertEqual(profile.height % 2, 0)

    def test_proxy_scale_is_clamped(self):
        timeline = _timeline()
        low = CanvasProfile.proxy(timeline, {"output_fps": _FPS}, 0.01)
        high = CanvasProfile.proxy(timeline, {"output_fps": _FPS}, 4.0)

        self.assertEqual(low.scale, compositor.PROXY_SCALE_MIN)
        self.assertEqual(high.scale, compositor.PROXY_SCALE_MAX)


class BaseFiltersTest(unittest.TestCase):
    """§8.1-7: scale=1.0 の文字列が従来の renderer._video_filters と完全一致すること"""

    def setUp(self):
        self.timeline = _timeline()
        self.profile = CanvasProfile(_PROD_W, _PROD_H, _FPS)

    # 正規化チェーンは従来の 5 本・同じ文字列 (concat_processor.concat と同一 / §8.2)
    def test_normalize_chain_is_unchanged(self):
        media = _media(width=1280, height=720)
        filters = compositor.base_filters(
            self.timeline, media, _base_clip(), self.profile)

        self.assertEqual(filters, [
            "scale=1920:1080:force_original_aspect_ratio=decrease",
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
            "setsar=1",
            "fps=60",
            "format=yuv420p",
        ])

    # 素材がキャンバスと同じなら何も足さない (= コマンドが 1 文字も変わらない / R15)
    def test_matching_media_needs_no_filter(self):
        filters = compositor.base_filters(
            self.timeline, _media(), _base_clip(), self.profile)

        self.assertEqual(filters, [])

    # 無音カットのフェードの文字列も従来どおり
    def test_fade_chain_is_unchanged(self):
        filters = compositor.base_filters(
            self.timeline, _media(), _base_clip(), self.profile,
            fade_sec=0.05, duration=3.0)

        self.assertEqual(filters, ["fade=t=in:st=0:d=0.05",
                                   "fade=t=out:st=2.950:d=0.05"])

    # 尺がフェード 2 本ぶんに満たないクリップへはフェードを掛けない
    def test_short_clip_has_no_fade(self):
        filters = compositor.base_filters(
            self.timeline, _media(), _base_clip(), self.profile,
            fade_sec=0.05, duration=0.08)

        self.assertEqual(filters, [])

    # プロキシはプロキシ寸法へ正規化する (ここで縮めると以降が全部軽くなる / §3.3)
    def test_proxy_normalizes_to_the_proxy_canvas(self):
        profile = CanvasProfile.proxy(self.timeline, {"output_fps": _FPS}, 0.5)
        filters = compositor.base_filters(
            self.timeline, _media(), _base_clip(), profile)

        self.assertIn("scale=960:540:force_original_aspect_ratio=decrease", filters)
        self.assertIn("pad=960:540:(ow-iw)/2:(oh-ih)/2", filters)
        self.assertIn("fps=60", filters, "fps は換算しない (§3.2)")


class OverlayChainTest(unittest.TestCase):

    def setUp(self):
        self.profile = CanvasProfile(_PROD_W, _PROD_H, _FPS)
        self.proxy = CanvasProfile.proxy(_timeline(), {"output_fps": _FPS}, 0.5)

    # §8.1-7: 従来 renderer._composite が組んでいた文字列と完全一致すること
    def test_production_chain_is_unchanged(self):
        args, chains, label, step = compositor.overlay_chain(
            _clip(), _media("m2"), self.profile, "[0:v]", 1, 0)

        self.assertEqual(args, ["-ss", "5.000", "-t", "3.000", "-i", __file__])
        self.assertEqual(chains, [
            "[1:v]scale=960:-2,setpts=PTS+2.000/TB[ov0]",
            "[0:v][ov0]overlay=960.0-w/2:540.0-h/2:enable='between(t,2.000,5.000)'[v0]",
        ])
        self.assertEqual((label, step), ("[v0]", 1))

    def test_opacity_is_unchanged(self):
        _args, chains, _label, _step = compositor.overlay_chain(
            _clip(opacity=0.5), _media("m2"), self.profile, "[0:v]", 1, 0)

        self.assertIn("format=rgba,colorchannelmixer=aa=0.500", chains[0])

    # 画像オーバーレイは -loop と -framerate を付け、setpts は付けない
    def test_image_input_args_are_unchanged(self):
        args, chains, _label, _step = compositor.overlay_chain(
            _clip(media_id="img1"), _image(), self.profile, "[0:v]", 1, 0)

        self.assertEqual(args, ["-loop", "1", "-framerate", "60",
                                "-t", "5.000", "-i", __file__])
        self.assertNotIn("setpts", chains[0])

    # プロキシは拡大率と位置が 1/2 になる (= 相似)
    def test_proxy_is_similar(self):
        _a, production, _l, _s = compositor.overlay_chain(
            _clip(), _media("m2"), self.profile, "[0:v]", 1, 0)
        _a2, proxy, _l2, _s2 = compositor.overlay_chain(
            _clip(), _media("m2"), self.proxy, "[0:v]", 1, 0)

        self.assertIn("scale=480:-2", proxy[0])
        self.assertIn("overlay=480.0-w/2:270.0-h/2", proxy[1])
        # 時間の表現は換算しない (どのフレームに何が写るかは一致させる / §2.1)
        self.assertIn("setpts=PTS+2.000/TB", proxy[0])
        self.assertIn("enable='between(t,2.000,5.000)'", proxy[1])
        self._assert_similar(production, proxy, 0.5)

    # 位置がキャンバスの端でも相似であること
    def test_proxy_is_similar_at_the_corner(self):
        clip = _clip(x=0.5, y=-0.5)
        _a, production, _l, _s = compositor.overlay_chain(
            clip, _media("m2"), self.profile, "[0:v]", 1, 0)
        _a2, proxy, _l2, _s2 = compositor.overlay_chain(
            clip, _media("m2"), self.proxy, "[0:v]", 1, 0)

        self._assert_similar(production, proxy, 0.5)

    # 1 枚絵モード: enable と setpts を付けず、その時刻のコマを取る
    def test_still_mode_has_no_enable(self):
        args, chains, _label, _step = compositor.overlay_chain(
            _clip(), _media("m2"), self.profile, "[0:v]", 1, 0,
            with_enable=False, still_sec=3.5, prefix="hq")

        self.assertEqual(args, ["-ss", "6.500", "-i", __file__])   # 5.0 + (3.5-2.0)
        self.assertNotIn("setpts", chains[0])
        self.assertNotIn("enable", chains[1])
        self.assertEqual(chains[1],
                         "[0:v][hqov0]overlay=960.0-w/2:540.0-h/2[hqv0]")

    # §8.1 proxy-6: タイル 2 枚目以降は enable / setpts がタイル開始ぶんずれる
    def test_time_offset_shifts_enable_and_setpts(self):
        _args, chains, _label, _step = compositor.overlay_chain(
            _clip(), _media("m2"), self.proxy, "[0:v]", 1, 0, time_offset=4.0)

        self.assertIn("setpts=PTS+-2.000/TB", chains[0])
        self.assertIn("enable='between(t,-2.000,1.000)'", chains[1])

    def test_time_offset_shifts_the_image_duration(self):
        args, _chains, _label, _step = compositor.overlay_chain(
            _clip(media_id="img1"), _image(), self.proxy, "[0:v]", 1, 0,
            time_offset=4.0)

        self.assertEqual(args[4:6], ["-t", "1.000"])

    # 幾何の数値がすべて ratio 倍になっていること (-1 / -2 の指定は据え置き)
    def _assert_similar(self, production, proxy, ratio):
        left = _geometry_numbers(";".join(production))
        right = _geometry_numbers(";".join(proxy))
        self.assertEqual([name for name, _v in left], [name for name, _v in right])
        self.assertTrue(left, "比較対象の数値が見つかりません")
        for (name, prod), (_n, prox) in zip(left, right):
            if prod < 0:
                self.assertEqual(prox, prod, f"{name}: 負の指定は換算しない")
            else:
                self.assertAlmostEqual(
                    prox, prod * ratio, delta=1.0,
                    msg=f"{name}: {prod} → {prox} (期待 {prod * ratio})")


class SubtitleChainTest(unittest.TestCase):
    """§8.1-2: ASS は本番とプロキシで完全に同一 (PlayRes を換算しない)"""

    def setUp(self):
        self.timeline = _timeline()
        self.eff_cfg = _subtitle_cfg()
        self.font_profile = subtitle_generator.build_font_profile(self.eff_cfg)

    def _write(self, directory, name="x.ass", fonts_dir=None):
        return compositor.subtitle_chain_for_items(
            self.timeline, [_comment_item()], self.eff_cfg, self.font_profile,
            os.path.join(directory, name), fonts_dir)

    # PlayRes は本番寸法。libass が実フレーム寸法へ追従するため相似が保証される。
    def test_playres_is_the_production_canvas(self):
        with tempfile.TemporaryDirectory() as work:
            ass_path, option = self._write(work)
            with open(ass_path, encoding="utf-8") as handle:
                text = handle.read()

        self.assertIn("PlayResX: 1920", text)
        self.assertIn("PlayResY: 1080", text)
        self.assertTrue(option.startswith("ass='"))

    # 本番とプロキシで同じ ASS・同じフィルタ指定になること (換算の余地が無い)
    def test_the_same_items_give_the_same_ass(self):
        with tempfile.TemporaryDirectory() as work:
            first_path, first = self._write(work, "a.ass")
            second_path, second = self._write(work, "b.ass")
            with open(first_path, encoding="utf-8") as handle:
                first_text = handle.read()
            with open(second_path, encoding="utf-8") as handle:
                second_text = handle.read()

        self.assertEqual(first_text, second_text)
        self.assertEqual(first.replace("a.ass", ""), second.replace("b.ass", ""))

    # fontsdir はディレクトリが在るときだけ付く (従来の burn_subtitle と同じ規約)
    def test_fontsdir_is_added_only_when_the_directory_exists(self):
        with tempfile.TemporaryDirectory() as work:
            self.assertIn("fontsdir=", compositor.ass_option("x.ass", work))
            self.assertNotIn("fontsdir=",
                             compositor.ass_option("x.ass", os.path.join(work, "nope")))
            self.assertNotIn("fontsdir=", compositor.ass_option("x.ass", ""))

    # パスのエスケープは ass / subtitles フィルタと同じ 2 段階
    def test_path_escape(self):
        self.assertEqual(compositor.escape_filter_path("C:\\work\\a.ass"),
                         "C:/work/a.ass".replace(":", "\\:"))


class IconChainTest(unittest.TestCase):
    """§8.1-3: コメントアイコンと背景箱の座標・寸法が scale 倍になること"""

    def setUp(self):
        self.eff_cfg = _subtitle_cfg()
        self.profile = CanvasProfile(_PROD_W, _PROD_H, _FPS)
        self.proxy = CanvasProfile.proxy(_timeline(), {"output_fps": _FPS}, 0.5)

    def test_icon_box_scales(self):
        item = _comment_item()
        full = comment_decor.icon_box(item, self.eff_cfg, _PROD_W, _PROD_H)
        half = comment_decor.icon_box(item, self.eff_cfg, _PROD_W, _PROD_H, 0.5)
        if full is None:
            self.skipTest("既定設定ではアイコンを出さない")

        self.assertAlmostEqual(half["x"], full["x"] / 2.0, delta=1.0)
        self.assertAlmostEqual(half["y"], full["y"] / 2.0, delta=1.0)
        self.assertAlmostEqual(half["size"], full["size"] / 2.0, delta=1.0)

    def test_background_box_scales(self):
        item = _comment_item()
        full = comment_decor.background_box(item, self.eff_cfg, _PROD_W, _PROD_H)
        half = comment_decor.background_box(item, self.eff_cfg, _PROD_W, _PROD_H,
                                            scale=0.5)
        if full is None:
            self.skipTest("既定設定では背景を出さない")

        for key in ("x", "y", "w", "h"):
            self.assertAlmostEqual(half[key], full[key] / 2.0, delta=1.0,
                                   msg=f"{key} が相似ではありません")
        self.assertAlmostEqual(half["radius"], full["radius"] / 2.0, delta=1.0)

    # 計算は本番寸法で行う: プロキシ寸法を直接渡した結果とは**別物**であること。
    # (余白が絶対 px のため、小さいキャンバスで計算すると相対的に広がってしまう)
    def test_computing_on_the_proxy_canvas_is_not_the_same(self):
        item = _comment_item()
        scaled = comment_decor.icon_box(item, self.eff_cfg, _PROD_W, _PROD_H, 0.5)
        naive = comment_decor.icon_box(item, self.eff_cfg, _PROD_W // 2, _PROD_H // 2)
        if scaled is None or naive is None:
            self.skipTest("既定設定ではアイコンを出さない")

        self.assertNotEqual((scaled["x"], scaled["y"]), (naive["x"], naive["y"]))

    # compositor 経由のチェーンでも数値が 1/2 になること
    def test_icon_chains_are_similar(self):
        items = [_comment_item()]
        production, count, _groups = compositor.icon_chains(
            items, self.eff_cfg, self.profile, "[v0]", "[v1]")
        proxy, _count2, _groups2 = compositor.icon_chains(
            items, self.eff_cfg, self.proxy, "[v0]", "[v1]")
        if not count:
            self.skipTest("アイコン素材が無いためチェーンが作られない")

        left = _geometry_numbers(";".join(production))
        right = _geometry_numbers(";".join(proxy))
        self.assertEqual([n for n, _v in left], [n for n, _v in right])
        for (name, prod), (_n, prox) in zip(left, right):
            self.assertAlmostEqual(prox, prod / 2.0, delta=1.0, msg=name)


class CropChainTest(unittest.TestCase):
    """§8.1-4: crop.filter_chain の boxblur 半径がキャンバス比で決まること"""

    def setUp(self):
        self.media = _media()
        self.cfg = crop.config({})
        self.layout = crop.make_layout(
            crop.MODE_SINGLE, [(420, 0, 1080, 1080)], self.media, 1080, 1920,
            background=crop.BG_BLUR)

    # 従来の呼び出し (scale 省略) は 1 文字も変わらない
    def test_default_scale_is_unchanged(self):
        chain = crop.filter_chain(self.layout, 1080, 1920, 60, self.cfg)

        self.assertIn("boxblur=20:2", chain)
        self.assertEqual(chain, crop.filter_chain(self.layout, 1080, 1920, 60,
                                                  self.cfg, scale=1.0))

    def test_radius_follows_the_canvas_ratio(self):
        chain = crop.filter_chain(self.layout, 540, 960, 60, self.cfg, scale=0.5)

        self.assertIn("boxblur=10:2", chain)
        self.assertIn("scale=540:960", chain)
        self.assertNotIn("boxblur=20", chain)

    # 1px 未満へ潰すとぼかしが消えるため下限は 1
    def test_radius_never_disappears(self):
        chain = crop.filter_chain(self.layout, 270, 480, 60, self.cfg, scale=0.01)

        self.assertIn("boxblur=1:2", chain)

    # crop 矩形はソース px なので換算しない (crop してから scale するため / §3.3)
    def test_crop_rect_is_not_scaled(self):
        chain = crop.filter_chain(self.layout, 540, 960, 60, self.cfg, scale=0.5)

        self.assertIn("crop=1080:1080:420:0", chain)

    # compositor 経由 (縦動画プロジェクト) でも同じ結果になること
    def test_compositor_passes_the_scale(self):
        timeline = _timeline(media=self.media, width=1080, height=1920,
                             source={crop.KEY: self.layout})
        profile = CanvasProfile.proxy(timeline, {"output_fps": _FPS}, 0.5)
        chain = compositor.crop_chain(timeline, self.media, profile, {})

        self.assertIsNotNone(chain)
        self.assertIn("boxblur=10:2", chain)


class BlurChainTest(unittest.TestCase):
    """§8.1-5: ぼかしの sigma が既存の比例計算でそのまま 1/2 になること"""

    def setUp(self):
        self.profile = CanvasProfile(_PROD_W, _PROD_H, _FPS)
        self.proxy = CanvasProfile.proxy(_timeline(), {"output_fps": _FPS}, 0.5)
        self.settings = {"blur": {"enabled": True}}

    def test_sigma_is_similar(self):
        with tempfile.TemporaryDirectory() as work:
            mask = os.path.join(work, "mask.png")
            open(mask, "wb").close()
            production, _l = compositor.blur_chains(
                self.settings, mask, self.profile, "[0:v]", "[out]")
            proxy, _l2 = compositor.blur_chains(
                self.settings, mask, self.proxy, "[0:v]", "[out]")

        sigma = _sigma(production)
        self.assertIsNotNone(sigma, "gblur が見つかりません")
        self.assertAlmostEqual(_sigma(proxy), sigma / 2.0, delta=0.51)
        # マスクはキャンバス寸法へ戻される (プロキシ寸法になること)
        self.assertIn("scale=960:540", ";".join(proxy))

    def test_no_mask_means_no_chain(self):
        chains, label = compositor.blur_chains(
            self.settings, None, self.profile, "[0:v]", "[out]")

        self.assertEqual((chains, label), ([], "[0:v]"))


def _sigma(chains):
    match = re.search(r"gblur=sigma=(\d+(?:\.\d+)?)", ";".join(chains))
    return float(match.group(1)) if match else None


class WatermarkChainTest(unittest.TestCase):
    """§8.1-6: 透かしの大きさと余白が 1/2 になること"""

    def setUp(self):
        self.profile = CanvasProfile(_PROD_W, _PROD_H, _FPS)
        self.proxy = CanvasProfile.proxy(_timeline(), {"output_fps": _FPS}, 0.5)

    def test_not_required_returns_the_input_label(self):
        args, chains, label = compositor.watermark_chain(
            {}, self.profile, "[v9]", "[wm]", 3, required=False)

        self.assertEqual((args, chains, label), ([], [], "[v9]"))

    def test_geometry_is_similar(self):
        if watermark_overlay.asset_path() is None:
            self.skipTest("透かし素材が無い環境では比較できない")
        _a, production, _l = compositor.watermark_chain(
            {}, self.profile, "[v9]", "[wm]", 3, required=True)
        _a2, proxy, _l2 = compositor.watermark_chain(
            {}, self.proxy, "[v9]", "[wm]", 3, required=True)

        self.assertIn("scale=480:-1", production[0])
        self.assertIn("scale=240:-1", proxy[0])
        # 余白もキャンバス幅の比率で決まる (1920*0.02=38 → 960*0.02=19)
        self.assertIn("W-w-38:H-h-38", production[1])
        self.assertIn("W-w-19:H-h-19", proxy[1])

    def test_input_index_is_used(self):
        if watermark_overlay.asset_path() is None:
            self.skipTest("透かし素材が無い環境では比較できない")
        _args, chains, label = compositor.watermark_chain(
            {}, self.profile, "[v9]", "[wmout]", 7, required=True)

        self.assertTrue(chains[0].startswith("[7:v]"))
        self.assertEqual(label, "[wmout]")


class WholeGraphSimilarityTest(unittest.TestCase):
    """§11-2: 効果を 1 つ足したら、それが compositor を通っているかを機械的に検査する

    base → ぼかし → オーバーレイ → アイコン → 透かし を 1 本のグラフへ組み、
    本番とプロキシで幾何の数値がすべて 1/2 になることを確かめる。
    新しい効果を compositor の外へ書いた場合、この検査は落ちない代わりに
    「プロキシに出ない」という形で現れる。落とすのは各節のテストの役目。
    """

    def _graph(self, profile, mask):
        timeline = _timeline()
        chains = []
        base = compositor.base_filters(
            timeline, _media(width=1280, height=720), _clip(), profile)
        chains.append(f"[0:v]{','.join(base)}[base]")
        current = "[base]"
        blur, current = compositor.blur_chains(
            {"blur": {"enabled": True}}, mask, profile, current, "[blout]")
        chains.extend(blur)
        _args, overlay, current, step = compositor.overlay_chain(
            _clip(), _media("m2"), profile, current, 1, 0)
        chains.extend(overlay)
        icons, _count, _groups = compositor.icon_chains(
            [_comment_item()], _subtitle_cfg(), profile, current, f"[v{step}]")
        chains.extend(icons)
        if icons:
            current = f"[v{step}]"
        wm_args, wm_chains, current = compositor.watermark_chain(
            {}, profile, current, "[wmout]", 2,
            required=watermark_overlay.asset_path() is not None)
        chains.extend(wm_chains)
        self.assertTrue(wm_chains or watermark_overlay.asset_path() is None)
        self.assertTrue(wm_args or watermark_overlay.asset_path() is None)
        return ";".join(chains)

    def test_every_geometry_number_halves(self):
        timeline = _timeline()
        profile = CanvasProfile.production(timeline, {"output_fps": _FPS})
        proxy = CanvasProfile.proxy(timeline, {"output_fps": _FPS}, 0.5)
        with tempfile.TemporaryDirectory() as work:
            mask = os.path.join(work, "mask.png")
            open(mask, "wb").close()
            left = _geometry_numbers(self._graph(profile, mask))
            right = _geometry_numbers(self._graph(proxy, mask))

        self.assertEqual([n for n, _v in left], [n for n, _v in right],
                         "本番とプロキシでフィルタの並びが違います")
        self.assertGreaterEqual(len(left), 8, "比較対象が少なすぎます")
        for (name, prod), (_n, prox) in zip(left, right):
            if prod < 0:
                self.assertEqual(prox, prod, f"{name}: 負の指定は換算しない")
            else:
                self.assertAlmostEqual(
                    prox, prod / 2.0, delta=1.0,
                    msg=f"{name}: {prod} → {prox} が相似ではありません")


if __name__ == "__main__":
    unittest.main()
