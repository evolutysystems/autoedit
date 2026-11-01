# 切り抜きの枠を全てのクリップへ当てる (ver9 resolve16) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver9/resolve16.md §6.1):
#   ・素材が複数本ある縦プロジェクトで、全クリップに crop が当たること (黒帯が無いこと)
#   ・寸法が違う素材へは、枠の縦横比を保ったまま換算して当てること
#   ・apply_to_all_media=false で v1.7.1 と同じ振る舞いへ戻せること
#   ・材料が無いとき (素材が不明・寸法が 0) は従来の正規化へ倒れること
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.timeline import clipboard, commands, compositor, crop
from src.timeline.compositor import CanvasProfile
from src.timeline.model import (ORIGIN_SILENCE_CUT, TRACK_AUDIO, TRACK_VIDEO,
                                AudioClip, Clip, MediaRef, Timeline, Track)

_CANVAS = (1080, 1920)
_FPS = 60
# アーカイブ切り抜きの中間ファイルと同じ寸法 (枠を決めた素材)
_SRC = (960, 540)
# 枠 (ソース px)。縦横比 540 / 492 ≒ 1.0976
_FRAME = (210, 24, 540, 492)


def _media(media_id, path, width=_SRC[0], height=_SRC[1]):
    return MediaRef(media_id, "video", path, 100.0, width, height, _FPS, True)


# 素材 N 本・各 1 クリップの縦 Timeline を作り、枠は先頭の素材で決める
def _vertical(medias, mode=crop.MODE_SINGLE, frames=(_FRAME,)):
    clips = []
    audio = []
    for index, media in enumerate(medias):
        clip = Clip(f"c{index + 1}", media.id, index * 5.0, 5.0, 0.0, 5.0,
                    origin={"type": ORIGIN_SILENCE_CUT})
        clips.append(clip)
        audio.append(AudioClip(f"a{index + 1}", clip.id))

    video_track = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=clips)
    audio_track = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=audio)
    timeline = Timeline(fps=_FPS, width=_CANVAS[0], height=_CANVAS[1],
                        orientation="portrait", media_pool=list(medias),
                        tracks=[video_track, audio_track])
    crop.store(timeline, crop.make_layout(mode, list(frames), medias[0],
                                          *_CANVAS, background=crop.BG_BLUR))
    return timeline


# クリップごとのフィルタ文字列 (切り抜きが当たらなければ None)
def _chains(timeline, settings=None):
    profile = CanvasProfile(*_CANVAS, _FPS)
    chains = []
    for clip in sorted(timeline.base_video_track().clips, key=lambda c: c.timeline_start):
        media = timeline.media_by_id(clip.media_id)
        chains.append(compositor.crop_chain(timeline, media, profile, settings or {}))
    return chains


class AllMediaTest(unittest.TestCase):

    def setUp(self):
        # 同じ寸法の素材 2 本 + 別寸法の素材 1 本 (アーカイブのセクションごとの素材)
        self.medias = [_media("m1", __file__),
                       _media("m2", __file__ + ".2"),
                       _media("m3", __file__ + ".3", 1200, 750)]
        self.timeline = _vertical(self.medias)

    # 1. 全クリップに枠が当たり、黒帯の正規化が 1 件も残らないこと (R2)
    def test_every_clip_gets_the_frame(self):
        chains = _chains(self.timeline)

        self.assertEqual(len(chains), 3)
        for index, chain in enumerate(chains):
            self.assertIsNotNone(chain, f"{index} 件目に切り抜きが当たっていません")
            self.assertIn("crop=", chain)
            self.assertIn("boxblur", chain)
            self.assertNotIn("pad=1080:1920", chain)

    # 2. 寸法が同じなら、別 ID の素材でも文字列が完全に一致すること
    def test_same_size_media_is_identical(self):
        chains = _chains(self.timeline)

        self.assertEqual(chains[0], chains[1])
        self.assertIn("crop=540:492:210:24", chains[0])

    # 3. 寸法 2 倍・比率が同じ素材は枠も 2 倍へ換算されること
    def test_double_size_media(self):
        timeline = _vertical([self.medias[0], _media("m2", __file__ + ".2", 1920, 1080)])

        self.assertIn("crop=1080:984:420:48", _chains(timeline)[1])

    # 4. 比率が違う素材でも枠の縦横比を保って換算すること (全面ぼかしへ逃げない)
    def test_other_aspect_media(self):
        chain = _chains(self.timeline)[2]

        self.assertIn("crop=674:614:263:68", chain)
        self.assertAlmostEqual(674 / 614.0, _FRAME[2] / float(_FRAME[3]), places=2)

    # 5. 換算後の枠は必ず素材の中へ収まり、幅・高さが偶数であること
    def test_fitted_frame_stays_inside_the_media(self):
        cfg = crop.config({})
        layout = crop.load(self.timeline)
        for width, height in ((1920, 1080), (1200, 750), (640, 360), (720, 1280), (641, 361)):
            media = _media("mx", __file__ + ".x", width, height)
            applied = crop.resolve_layout(layout, media, *_CANVAS, cfg)
            self.assertIsNotNone(applied, f"{width}x{height} へ当てられていません")
            self.assertTrue(crop.is_valid(applied, media, *_CANVAS))
            for frame in applied["frames"]:
                x, y, w, h = frame["src"]
                self.assertEqual((w % 2, h % 2), (0, 0), f"{width}x{height} が奇数です")
                self.assertLessEqual(x + w, width)
                self.assertLessEqual(y + h, height)

    # 6. apply_to_all_media=false なら v1.7.1 と同じく枠を決めた素材だけに当たること
    def test_opt_out_keeps_old_behaviour(self):
        settings = {"vertical": {"crop": {"apply_to_all_media": False}}}
        chains = _chains(self.timeline, settings)

        self.assertIn("crop=540:492:210:24", chains[0])
        self.assertIsNone(chains[1])
        self.assertIsNone(chains[2])

    # 7. split の指定でも、2 枠とも置き場所の縦横比を保って換算されること
    def test_split_keeps_dest_aspect(self):
        # 素材 (960x540) へ収まる最大の枠。置き場所 1080x640 / 1080x1280 の比に合わせる
        frames = ((25, 0, 910, 538), (253, 0, 454, 538))
        timeline = _vertical([self.medias[0], self.medias[2]],
                             mode=crop.MODE_SPLIT, frames=frames)
        layout = crop.load(timeline)
        applied = crop.resolve_layout(layout, self.medias[2], *_CANVAS, crop.config({}))

        self.assertIsNotNone(applied)
        for index, frame in enumerate(applied["frames"]):
            _x, _y, w, h = frame["src"]
            wanted = crop.aspect(crop.MODE_SPLIT, index, *_CANVAS)
            self.assertAlmostEqual(w / float(h), wanted, places=1)

    # 8. 材料が無いとき (素材が不明・寸法が 0) は壊れた crop を作らないこと
    def test_missing_media_falls_back(self):
        profile = CanvasProfile(*_CANVAS, _FPS)

        self.assertIsNone(compositor.crop_chain(self.timeline, None, profile, {}))
        self.assertIsNone(compositor.crop_chain(
            self.timeline, _media("m9", __file__ + ".9", 0, 0), profile, {}))

    # 9. 警告は素材ごとに 1 回だけ (クリップごとに出すとログが読めなくなる)
    def test_warning_is_once_per_media(self):
        compositor._warned_crop.clear()
        settings = {"vertical": {"crop": {"apply_to_all_media": False}}}
        with self.assertLogs("src.timeline.compositor", level="WARNING") as logs:
            _chains(self.timeline, settings)
            _chains(self.timeline, settings)

        self.assertEqual(len(logs.output), 2, "素材 2 本ぶん以上の警告が出ています")

    # 10. 設定の既定は「全ての素材へ当てる」
    def test_default_is_on(self):
        self.assertTrue(crop.config({})["apply_to_all_media"])


class PastedClipTest(unittest.TestCase):

    def setUp(self):
        # パスで素材を引き当てるため、実体のあるファイルを使う
        self.work = tempfile.mkdtemp()
        self.paths = []
        for name in ("a.mp4", "b.mp4", "c.mp4"):
            path = os.path.join(self.work, name)
            with open(path, "wb") as handle:
                handle.write(b"x")
            self.paths.append(path)

    def _timeline(self, second_path):
        return _vertical([_media("m1", self.paths[0]), _media("m2", second_path)])

    # 11. クリップをコピーして先頭へ貼っても枠が当たること (resolve16 §1.8 (2))
    def test_pasted_clip_gets_the_frame(self):
        timeline = self._timeline(self.paths[1])
        payload = clipboard.build_payload(timeline, ["c2"])
        commands.PasteClips(payload, 0.0).apply(timeline)

        chains = _chains(timeline)
        self.assertEqual(len(chains), 3)
        for chain in chains:
            self.assertIsNotNone(chain)
            self.assertIn("crop=540:492:210:24", chain)

    # 12. 別プロジェクトから貼って素材が 1 本増えても枠が当たること (resolve16 §1.8 (3))
    def test_pasted_media_gets_the_frame(self):
        source = self._timeline(self.paths[1])
        payload = clipboard.build_payload(source, ["c2"])
        dest = self._timeline(self.paths[2])        # 同じ絵でもパスが違う = 別の素材になる
        commands.PasteClips(payload, 0.0).apply(dest)

        self.assertEqual(len(dest.media_pool), 3, "素材が増える前提が崩れています")
        for chain in _chains(dest):
            self.assertIsNotNone(chain)
            self.assertIn("crop=540:492:210:24", chain)


if __name__ == "__main__":
    unittest.main()
