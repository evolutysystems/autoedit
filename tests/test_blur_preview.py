# プレビューへのぼかし反映 (src/blur/preview.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve4.md §3.4 / §5.6 + ver5 resolve8 §8.1 G):
#   ・ぼかす対象が無いフレームは**入力をそのまま返す** (処理していない)
#   ・ぼかすのはマスクの白い所だけ。外は 1 バイトも変わらないこと
#   ・レターボックスのある素材でも、ぼける位置が囲みと一致すること
#   ・BlurPlan を 1 回しか作らないこと (スクラブのたびに作り直すと 1 枚 20ms 余計にかかる)
import os
import unittest

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.blur import decisions as blur_decisions
from src.blur import preview as blur_preview
from src.blur.config import config
from src.timeline.model import (
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    AudioClip,
    Clip,
    MediaRef,
    Timeline,
    Track,
)


# media_size を変えると、キャンバス (1920x1080) に対してレターボックスが付く
def _build_timeline(media_size=(1920, 1080)):
    width, height = media_size
    media = [MediaRef("m1", "video", __file__, 100.0, width, height, 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 10.0, 0.0, 10.0),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
                  clips=[AudioClip("a1", "c1")])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1")
    return Timeline(fps=60, width=1920, height=1080,
                    source={"media_id": "m1", "input_path": __file__},
                    media_pool=media, tracks=[video, audio, subtitle])


# 囲み 1 件の指定 (正規化キャンバス座標)。span は 0〜8 秒に限る
def _area_state(timeline, rect=(0.1, 0.18, 0.16, 0.56), mode=None, end=8.0):
    mode = mode or blur_decisions.BLUR
    span = {"start": 0.0, "end": end, "clip_id": "c1", "label": "クリップ 1"}
    spec = blur_decisions.make_area_spec("b1", mode, "m1", span, 0.0, rect)
    return blur_decisions.with_spec(blur_decisions._empty(), spec)   # noqa: SLF001


def _noise_frame(width, height, seed=0):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, (height, width, 3), dtype=np.uint8).tobytes()


class BlurPreviewTest(unittest.TestCase):

    def setUp(self):
        if not blur_preview.is_available():
            self.skipTest("PIL が使えないためぼかしを当てられません")
        self.timeline = _build_timeline()
        self.cfg = config({"blur": {"enabled": True}})
        blur_decisions.store(self.timeline, _area_state(self.timeline))
        self.preview = blur_preview.BlurPreview(
            self.timeline, None, blur_decisions.load(self.timeline), self.cfg)

    def _apply(self, source_sec, width=1920, height=1080, seed=0):
        data = _noise_frame(width, height, seed)
        return data, self.preview.apply(data, width, height, "m1", source_sec)

    # G1: 指定が効いていない時刻は入力をそのまま返すこと
    def test_frame_without_targets_is_returned_untouched(self):
        data, result = self._apply(9.5)         # span (0〜8 秒) の外
        self.assertTrue(result is data, "ぼかす対象が無いフレームを作り直しています")

    # G3: 囲みの中だけが変わること
    def test_only_the_masked_area_changes(self):
        data, result = self._apply(4.0)
        self.assertFalse(result is data, "ぼかしが当たっていません")
        before = np.frombuffer(data, dtype=np.uint8).reshape(1080, 1920, 3)
        after = np.frombuffer(result, dtype=np.uint8).reshape(1080, 1920, 3)

        changed = np.any(before != after, axis=2)
        self.assertTrue(changed.any(), "どこもぼけていません")
        rows = np.where(changed.any(axis=1))[0]
        cols = np.where(changed.any(axis=0))[0]
        # 囲みは (0.1, 0.18)-(0.26, 0.74) = 素材座標 (192,194)-(499,799)。
        # 画面の反対側 (右下) は 1 バイトも変わらない
        self.assertLess(cols.max(), 1000)
        self.assertLess(rows.max(), 1000)
        self.assertTrue((before[900:, 1200:] == after[900:, 1200:]).all())

    # G2: 全面ぼかしなら画面全体が変わること
    def test_frame_spec_blurs_everything(self):
        span = {"start": 0.0, "end": 8.0, "clip_id": "c1", "label": "クリップ 1"}
        blur_decisions.store(self.timeline, blur_decisions.with_spec(
            blur_decisions._empty(),            # noqa: SLF001
            blur_decisions.make_frame_spec("b1", "m1", span)))
        self.preview.invalidate(blur_decisions.load(self.timeline))
        data, result = self._apply(4.0)
        before = np.frombuffer(data, dtype=np.uint8).reshape(1080, 1920, 3)
        after = np.frombuffer(result, dtype=np.uint8).reshape(1080, 1920, 3)
        changed = np.any(before != after, axis=2)
        self.assertGreater(changed.mean(), 0.9, "画面全体がぼけていません")

    # G4: レターボックスのある素材でも、ぼける位置が合うこと
    def test_letterboxed_media_blurs_the_same_place(self):
        # 4:3 の素材を 16:9 のキャンバスへ載せる = 左右に帯が付く
        timeline = _build_timeline((640, 480))
        # キャンバス上で中央 (0.45〜0.55, 0.4〜0.6) を囲む = 素材の中央
        blur_decisions.store(timeline, _area_state(timeline, (0.45, 0.4, 0.1, 0.2)))
        preview = blur_preview.BlurPreview(
            timeline, None, blur_decisions.load(timeline), self.cfg)
        data = _noise_frame(640, 480)
        result = preview.apply(data, 640, 480, "m1", 4.0)
        self.assertFalse(result is data, "ぼかしが当たっていません")

        before = np.frombuffer(data, dtype=np.uint8).reshape(480, 640, 3)
        after = np.frombuffer(result, dtype=np.uint8).reshape(480, 640, 3)
        changed = np.any(before != after, axis=2)
        cols = np.where(changed.any(axis=0))[0]
        rows = np.where(changed.any(axis=1))[0]
        # レターボックス (キャンバス上で左右 240px ぶん) を取り違えていれば
        # 中心が 100px 以上ずれるため、中心で確かめる
        center_x = (int(cols.min()) + int(cols.max())) / 2.0
        center_y = (int(rows.min()) + int(rows.max())) / 2.0
        self.assertLess(abs(center_x - 320), 20, "ぼける位置が横にずれています")
        self.assertLess(abs(center_y - 240), 20, "ぼける位置が縦にずれています")

    def test_pixelate_and_gaussian_differ(self):
        gaussian = self._apply(4.0)[1]
        pixel_cfg = config({"blur": {"enabled": True, "render": {"mode": "pixelate"}}})
        pixelate = blur_preview.BlurPreview(
            self.timeline, None, blur_decisions.load(self.timeline), pixel_cfg
        ).apply(_noise_frame(1920, 1080), 1920, 1080, "m1", 4.0)
        self.assertTrue(gaussian != pixelate, "ガウスとモザイクの結果が同じです")

    def test_mask_is_built(self):
        self.assertIsNotNone(self.preview.mask("m1", 4.0))

    def test_empty_frame_is_returned_untouched(self):
        self.assertEqual(self.preview.apply(b"", 0, 0, "m1", 4.0), b"")

    def test_plan_is_built_once_until_invalidated(self):
        first = self.preview.plan()
        self.preview.apply(_noise_frame(1920, 1080), 1920, 1080, "m1", 4.0)
        self.assertIs(self.preview.plan(), first, "スクラブのたびに BlurPlan を作り直しています")

        self.preview.invalidate(blur_decisions.load(self.timeline))
        self.assertIsNot(self.preview.plan(), first)

    # 「ボカさない」だけならぼけないこと (書き出しと同じ判定を通っている)
    def test_keep_alone_does_not_blur(self):
        blur_decisions.store(self.timeline, _area_state(self.timeline,
                                                        mode=blur_decisions.KEEP))
        self.preview.invalidate(blur_decisions.load(self.timeline))
        data, result = self._apply(4.0)
        self.assertTrue(result is data, "「ボカさない」だけでぼけています")

    # 追従結果を差し替えたら作り直すこと
    def test_set_tracks_invalidates(self):
        first = self.preview.plan()
        self.preview.set_tracks({"schema": 5, "tracks": {}})
        self.assertIsNot(self.preview.plan(), first)


if __name__ == "__main__":
    unittest.main()
