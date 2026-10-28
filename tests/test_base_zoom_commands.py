# V1 の拡大率を変える操作と、その反映先の単体テスト (docs/request/ver9/resolve6.md §7.2)
# 実行: python -m unittest discover -s tests
#
# 方針: 「拡大率を変えたら、プレビュー・書き出し・Resolve 出力のすべてが追いかける」
#
# 重点:
#   ・V1 とオーバーレイで**基準が違う**ため、コマンドが互いに混ざらないこと。
#     ZoomBaseClip はオーバーレイに効かず、ResizeOverlay は V1 に効かない。
#   ・**プロキシの鍵が変わること。** ここを忘れると拡大率を変えても古いタイルが
#     使われ、画面がまったく変わらない (resolve6 §4.2)。
#   ・等倍のクリップは FCPXML へ adjust-transform を書かないこと
#     (拡大率を使っていないプロジェクトの出力を 1 バイトも変えない)。
import os
import unittest
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.export import fcpxml_builder, resolve_export
from src.export.config import resolve_config
from src.timeline import commands, project_io, proxy_render
from src.timeline.compositor import CanvasProfile
from src.timeline.model import (
    BASE_AUDIO_TRACK_ID,
    BASE_VIDEO_TRACK_ID,
    TRACK_AUDIO,
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


# V1 1 本 + オーバーレイ 1 本の Timeline を組む
def _timeline(media=None):
    media = media or _media()
    timeline = Timeline(fps=_FPS, width=_W, height=_H,
                        source={"media_id": media.id, "input_path": __file__},
                        media_pool=[media])
    base = Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, name="Video 1", is_base=True)
    over = Track("V2", TRACK_VIDEO, 2, name="Video 2")
    audio = Track(BASE_AUDIO_TRACK_ID, TRACK_AUDIO, 1, name="Audio 1",
                  link_track=BASE_VIDEO_TRACK_ID)
    base.clips.append(Clip("c1", media.id, 0.0, 5.0, 0.0, source_out=5.0))
    over.clips.append(Clip("o1", media.id, 0.0, 5.0, 0.0, source_out=5.0))
    timeline.tracks = [base, over, audio]
    return timeline


class ZoomBaseClipTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _timeline()
        self.clip = self.timeline.clip_by_id("c1")

    # 本題: V1 クリップの拡大率が変わる
    def test_zoom_is_applied(self):
        self.assertTrue(commands.ZoomBaseClip("c1", 1.5).apply(self.timeline))
        self.assertAlmostEqual(self.clip.transform.scale, 1.5, places=6)

    # 設定の範囲へ丸める
    def test_zoom_is_clamped(self):
        commands.ZoomBaseClip("c1", 99.0, 0.1, 3.0).apply(self.timeline)
        self.assertAlmostEqual(self.clip.transform.scale, 3.0, places=6)
        commands.ZoomBaseClip("c1", 0.001, 0.1, 3.0).apply(self.timeline)
        self.assertAlmostEqual(self.clip.transform.scale, 0.1, places=6)

    # 同じ値なら何もしない (履歴を汚さない)
    def test_same_value_does_nothing(self):
        commands.ZoomBaseClip("c1", 1.5).apply(self.timeline)
        self.assertFalse(commands.ZoomBaseClip("c1", 1.5).apply(self.timeline))

    # 本題: 拡大率を下げると、可動範囲の外へ出た位置も丸め直される。
    # これを忘れると、縮めたときに前の位置が残って黒帯が出る。
    def test_position_is_reclamped_when_zoom_shrinks(self):
        commands.ZoomBaseClip("c1", 2.0).apply(self.timeline)
        commands.MoveBaseClip("c1", 1.0, 0.0).apply(self.timeline)
        self.assertAlmostEqual(self.clip.transform.x, 1.0, places=6)
        commands.ZoomBaseClip("c1", 1.2).apply(self.timeline)
        self.assertAlmostEqual(self.clip.transform.x, 0.2, places=6)

    # 等倍へ戻すと位置も中央へ戻る
    def test_back_to_identity_resets_the_position(self):
        commands.ZoomBaseClip("c1", 1.5).apply(self.timeline)
        commands.MoveBaseClip("c1", 0.5, -0.5).apply(self.timeline)
        commands.ZoomBaseClip("c1", 1.0).apply(self.timeline)
        self.assertEqual((self.clip.transform.x, self.clip.transform.y), (0.0, 0.0))

    # オーバーレイには効かない (基準が違うため混ぜない)
    def test_overlay_is_not_affected(self):
        self.assertFalse(commands.ZoomBaseClip("o1", 1.5).apply(self.timeline))

    # 既存の ResizeOverlay は V1 を弾いたまま (逆方向の取り違えも防ぐ)
    def test_resize_overlay_still_rejects_the_base_clip(self):
        self.assertFalse(commands.ResizeOverlay("c1", 1.5).apply(self.timeline))
        self.assertTrue(commands.ResizeOverlay("o1", 1.5).apply(self.timeline))

    # 無い id は何もしない
    def test_unknown_clip(self):
        self.assertFalse(commands.ZoomBaseClip("nope", 1.5).apply(self.timeline))


class MoveBaseClipTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _timeline()
        self.clip = self.timeline.clip_by_id("c1")

    # 等倍では動かせない (はみ出しも余白も無く、誤操作で履歴を汚さない)
    def test_identity_cannot_move(self):
        self.assertFalse(commands.MoveBaseClip("c1", 0.5, 0.5).apply(self.timeline))
        self.assertEqual((self.clip.transform.x, self.clip.transform.y), (0.0, 0.0))

    # 拡大中は動かせる
    def test_move_while_zoomed(self):
        commands.ZoomBaseClip("c1", 1.5).apply(self.timeline)
        self.assertTrue(commands.MoveBaseClip("c1", 0.3, -0.2).apply(self.timeline))
        self.assertAlmostEqual(self.clip.transform.x, 0.3, places=6)
        self.assertAlmostEqual(self.clip.transform.y, -0.2, places=6)

    # 可動範囲を越える指定は丸める (黒帯を出さない)
    def test_move_is_clamped(self):
        commands.ZoomBaseClip("c1", 1.5).apply(self.timeline)
        commands.MoveBaseClip("c1", 5.0, -5.0).apply(self.timeline)
        self.assertAlmostEqual(self.clip.transform.x, 0.5, places=6)
        self.assertAlmostEqual(self.clip.transform.y, -0.5, places=6)

    # 縮小中も動かせる (余白の中を動く)
    def test_move_while_shrunk(self):
        commands.ZoomBaseClip("c1", 0.5).apply(self.timeline)
        self.assertTrue(commands.MoveBaseClip("c1", 0.4, 0.0).apply(self.timeline))
        self.assertAlmostEqual(self.clip.transform.x, 0.4, places=6)

    # 同じ位置なら何もしない
    def test_same_position_does_nothing(self):
        commands.ZoomBaseClip("c1", 1.5).apply(self.timeline)
        commands.MoveBaseClip("c1", 0.3, 0.0).apply(self.timeline)
        self.assertFalse(commands.MoveBaseClip("c1", 0.3, 0.0).apply(self.timeline))

    # オーバーレイはこのコマンドの対象外 (MoveOverlay が担当する)
    def test_overlay_is_not_affected(self):
        self.assertFalse(commands.MoveBaseClip("o1", 0.5, 0.0).apply(self.timeline))


class ProxyKeyTest(unittest.TestCase):
    """拡大率を変えたらプロキシの鍵が変わること (resolve6 §4.2)"""

    def setUp(self):
        self.timeline = _timeline()
        settings = {"ffmpeg": {"output_width": _W, "output_height": _H,
                               "output_fps": _FPS}}
        profile = CanvasProfile.proxy(self.timeline, settings["ffmpeg"], 0.5)
        self.env = proxy_render.ProxyEnvironment(
            settings, profile,
            {"tile_sec": 4.0, "preset": "ultrafast", "crf": 28,
             "audio_bitrate": "96k"},
            ".")

    def _key(self):
        return proxy_render.tile_key(self.timeline, self.env, 0)

    # 本題: 拡大率で鍵が変わる
    def test_zoom_changes_the_key(self):
        before = self._key()
        commands.ZoomBaseClip("c1", 1.5).apply(self.timeline)
        self.assertNotEqual(before, self._key())

    # 本題: 位置でも鍵が変わる
    def test_position_changes_the_key(self):
        commands.ZoomBaseClip("c1", 1.5).apply(self.timeline)
        before = self._key()
        commands.MoveBaseClip("c1", 0.3, 0.0).apply(self.timeline)
        self.assertNotEqual(before, self._key())

    # 元へ戻せば鍵も戻る (Undo でキャッシュが効き直す)
    def test_key_returns_when_zoom_returns(self):
        before = self._key()
        commands.ZoomBaseClip("c1", 1.5).apply(self.timeline)
        commands.ZoomBaseClip("c1", 1.0).apply(self.timeline)
        self.assertEqual(before, self._key())


class ProjectRoundTripTest(unittest.TestCase):
    """拡大率と位置がプロジェクトの往復で保たれること"""

    def test_zoom_survives_save_and_load(self):
        timeline = _timeline()
        commands.ZoomBaseClip("c1", 1.75).apply(timeline)
        commands.MoveBaseClip("c1", 0.25, -0.125).apply(timeline)
        restored = project_io.from_json(
            project_io.to_json(timeline), validate_timeline=False)
        clip = restored.clip_by_id("c1")
        self.assertAlmostEqual(clip.transform.scale, 1.75, places=4)
        self.assertAlmostEqual(clip.transform.x, 0.25, places=4)
        self.assertAlmostEqual(clip.transform.y, -0.125, places=4)

    # コピーでも複写される (クリップの複製で拡大率が落ちない)
    def test_zoom_survives_copy(self):
        timeline = _timeline()
        commands.ZoomBaseClip("c1", 1.5).apply(timeline)
        copied = timeline.clip_by_id("c1").copy()
        self.assertAlmostEqual(copied.transform.scale, 1.5, places=6)


class ResolveExportTest(unittest.TestCase):
    """FCPXML へ adjust-transform を書くこと (resolve6 §4.8)"""

    def setUp(self):
        self.canvas = (_W, _H)
        self.title_cfg = resolve_config({})["title"]

    def _transform_for(self, zoom, x=0.0, y=0.0):
        clip = Clip("c1", "m1", 0.0, 5.0, 0.0, source_out=5.0)
        clip.transform = Transform(x=x, y=y, scale=zoom)
        return resolve_export._base_transform_for(clip, self.canvas, self.title_cfg)

    # 等倍かつ位置なしでは何も出さない (既存の出力を 1 バイトも変えない)
    def test_identity_is_not_written(self):
        self.assertIsNone(self._transform_for(1.0))

    # 拡大率はそのまま渡す (asset-clip は収めた状態が 1.0 に当たる)
    def test_zoom_is_passed_through(self):
        transform = self._transform_for(1.5)
        self.assertAlmostEqual(transform["scale"], 1.5, places=6)

    # 位置はタイトル・透かしと同じ規約 (中心原点・Y は上が正・±1 正規化)
    def test_position_uses_the_export_space(self):
        transform = self._transform_for(1.5, 0.5, -0.25)
        self.assertAlmostEqual(transform["position"][0], 0.5, places=4)
        self.assertAlmostEqual(transform["position"][1], -0.25, places=4)

    # 設定で止められること (Resolve の解釈が食い違ったときの逃げ道)
    def test_can_be_turned_off(self):
        self.assertTrue(resolve_config({})["write_base_transform"])
        self.assertFalse(resolve_config(
            {"export": {"resolve": {"write_base_transform": False}}}
        )["write_base_transform"])

    # 生成される XML に adjust-transform が入り、等倍のクリップには入らない
    def test_fcpxml_contains_adjust_transform(self):
        spec = {
            "fps": _FPS, "width": _W, "height": _H,
            "source": {"path": __file__, "name": "src", "duration": 100.0},
            "clips": [
                {"start": 0.0, "duration": 2.0, "name": "cut1", "offset": 0.0},
                {"start": 2.0, "duration": 2.0, "name": "cut2", "offset": 2.0,
                 "transform": {"scale": 1.5, "position": (0.5, -0.25)}},
            ],
        }
        root = ET.fromstring(fcpxml_builder.build_fcpxml(spec))
        found = {}
        for clip in root.iter("asset-clip"):
            element = clip.find("adjust-transform")
            found[clip.get("name")] = None if element is None else element.attrib
        self.assertIsNone(found["cut1"])
        self.assertEqual(found["cut2"]["scale"], "1.5000 1.5000")
        self.assertEqual(found["cut2"]["position"], "0.5000 -0.2500")

    # クリップ列へ乗せるとき、等倍のものには鍵自体を作らない
    def test_clips_from_base_clips_skips_identity(self):
        timeline = _timeline()
        commands.ZoomBaseClip("c1", 1.5).apply(timeline)
        base = timeline.base_video_track()
        base.clips.append(Clip("c2", "m1", 5.0, 5.0, 5.0, source_out=10.0))
        clips = resolve_export._clips_from_base_clips(
            base.clips, "cut",
            transform_cb=lambda c: resolve_export._base_transform_for(
                c, self.canvas, self.title_cfg))
        self.assertIn("transform", clips[0])
        self.assertNotIn("transform", clips[1])


if __name__ == "__main__":
    unittest.main()
