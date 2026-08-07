# Timeline データモデル (src/timeline/model.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点: 音声クリップが時刻を持たず V1 から導出されること (R18 / resolve.md §6.3.4)。
import unittest

from src.timeline.model import (
    AudioClip,
    Clip,
    MediaRef,
    ORIGIN_ENDING,
    ORIGIN_OPENING,
    ORIGIN_SILENCE_CUT,
    SubtitleClip,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    Timeline,
    Track,
    Transform,
)


# OP + 本編 2 クリップ + ED を持つ Timeline を組む (resolve.md §6.2.2 の例に対応)
def _build_timeline():
    media = [
        MediaRef("m1", "video", "C:/work/loudness.mp4", 3612.345, 1920, 1080, 60, True),
        MediaRef("m3", "video", "D:/assets/op.mp4", 4.0, 1920, 1080, 30, True),
        MediaRef("m4", "video", "D:/assets/ed.mp4", 6.5, 1280, 720, 30, True),
    ]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c0", "m3", 0.0, 4.0, 0.0, 4.0, origin={"type": ORIGIN_OPENING}),
        Clip("c1", "m1", 4.0, 12.48, 0.0, 12.48,
             origin={"type": ORIGIN_SILENCE_CUT, "segment_index": 0}),
        Clip("c2", "m1", 16.48, 33.68, 15.22, 48.9,
             origin={"type": ORIGIN_SILENCE_CUT, "segment_index": 1}),
        Clip("c9", "m4", 50.16, 6.5, 0.0, 6.5, origin={"type": ORIGIN_ENDING}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=[
        AudioClip("a0", "c0"), AudioClip("a1", "c1"),
        AudioClip("a2", "c2"), AudioClip("a9", "c9"),
    ])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1", clips=[
        SubtitleClip("s1", 4.35, 2.1, "今日はこの話をします",
                     origin={"type": "asr", "source_start": 0.35, "source_end": 2.45}),
    ])
    return Timeline(fps=60, width=1920, height=1080,
                    source={"media_id": "m1", "duration_sec": 3612.345},
                    media_pool=media, tracks=[video, audio, subtitle])


class TimelineModelTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build_timeline()

    # 全長は非音声トラックの最終終端 (OP + 本編 + ED)
    def test_duration_is_last_clip_end(self):
        self.assertAlmostEqual(self.timeline.duration_sec(), 56.66, places=3)

    # ベーストラックは is_base で解決でき、有効クリップが時系列順に返る
    def test_base_clips_are_ordered(self):
        ids = [c.id for c in self.timeline.base_clips()]
        self.assertEqual(ids, ["c0", "c1", "c2", "c9"])

    # 無効化したクリップは base_clips から外れる
    def test_disabled_clip_is_excluded(self):
        self.timeline.clip_by_id("c1").enabled = False
        self.assertEqual([c.id for c in self.timeline.base_clips()], ["c0", "c2", "c9"])

    # OP/ED は origin で識別できる (UI の色分け用)
    def test_origin_identifies_opening_ending(self):
        self.assertTrue(self.timeline.clip_by_id("c0").is_opening_or_ending())
        self.assertTrue(self.timeline.clip_by_id("c9").is_opening_or_ending())
        self.assertFalse(self.timeline.clip_by_id("c1").is_opening_or_ending())

    # ID は既存と衝突せず払い出される
    def test_next_id_avoids_collision(self):
        self.assertEqual(self.timeline.next_id("c"), "c3")
        # 連続して払い出しても重複しない
        self.assertNotEqual(self.timeline.next_id("c"), "c3")


class AudioLinkTest(unittest.TestCase):
    """R18: 音声クリップは時刻を持たず V1 から導出する (resolve.md §6.3.4)"""

    def setUp(self):
        self.timeline = _build_timeline()

    # 導出値が映像クリップと一致する
    def test_audio_time_is_derived(self):
        audio = self.timeline.audio_clip_for("c2")
        self.assertEqual(audio.id, "a2")
        self.assertAlmostEqual(audio.timeline_start(self.timeline), 16.48)
        self.assertAlmostEqual(audio.duration(self.timeline), 33.68)
        self.assertAlmostEqual(audio.source_in(self.timeline), 15.22)

    # 映像を移動すると、音声側へ何もしなくても導出値が追従する
    def test_audio_follows_move_without_propagation(self):
        audio = self.timeline.audio_clip_for("c2")
        self.timeline.clip_by_id("c2").timeline_start = 20.0
        self.assertAlmostEqual(audio.timeline_start(self.timeline), 20.0)

    # 映像をトリムしても同様に追従する
    def test_audio_follows_trim_without_propagation(self):
        audio = self.timeline.audio_clip_for("c2")
        clip = self.timeline.clip_by_id("c2")
        clip.duration = 10.0
        clip.source_out = clip.source_in + 10.0
        self.assertAlmostEqual(audio.duration(self.timeline), 10.0)

    # 音声クリップの JSON には時刻フィールドが含まれない (§6.2.3)
    def test_audio_dict_has_no_time_fields(self):
        data = self.timeline.audio_clip_for("c1").to_dict()
        self.assertEqual(set(data), {"id", "link_clip", "gain_db", "muted"})

    # リンク欠落時は導出値が None になる (レンダリング側で無音扱いにする)
    def test_broken_link_yields_none(self):
        orphan = AudioClip("aX", "no_such_clip")
        self.assertIsNone(orphan.timeline_start(self.timeline))


class OverlayOrderTest(unittest.TestCase):
    """§8.4: オーバーレイ要素は z_order 昇順で並ぶ (V1 は含まない)"""

    def setUp(self):
        self.timeline = _build_timeline()
        overlay = Track("V2", TRACK_VIDEO, 2, name="Video 2", clips=[
            Clip("c3", "m1", 5.0, 5.0, 0.0, 5.0, z_order=10,
                 origin={"type": "user_media"}),
            Clip("c4", "m1", 12.0, 3.0, 0.0, 3.0, z_order=200,
                 origin={"type": "user_media"}),
        ])
        self.timeline.tracks.append(overlay)

    # 字幕 (既定 100) を挟んで z_order 順に並ぶ
    def test_elements_sorted_by_z_order(self):
        ids = [c.id for c in self.timeline.overlay_elements()]
        self.assertEqual(ids, ["c3", "s1", "c4"])

    # ベーストラックのクリップはオーバーレイに含まれない
    def test_base_clips_are_not_overlays(self):
        ids = {c.id for c in self.timeline.overlay_elements()}
        self.assertNotIn("c1", ids)


class GapTest(unittest.TestCase):
    """§8.2: 空白 (ギャップ) の検出"""

    # 「削除 (空白を残す)」の跡がギャップとして検出される
    def test_gap_detected_after_non_ripple_delete(self):
        timeline = _build_timeline()
        timeline.base_video_track().remove_clip("c1")
        gaps = timeline.base_gaps()
        self.assertEqual(len(gaps), 1)
        self.assertAlmostEqual(gaps[0][0], 4.0)
        self.assertAlmostEqual(gaps[0][1], 16.48)

    # 隙間なく並んでいればギャップは無い
    def test_no_gap_when_contiguous(self):
        self.assertEqual(_build_timeline().base_gaps(), [])


class TransformTest(unittest.TestCase):

    # 字幕の位置は未指定 (None) を保持する = 設定の alignment に従う
    def test_subtitle_position_defaults_to_none(self):
        clip = SubtitleClip("s9", 0.0, 1.0, "text")
        self.assertFalse(clip.transform.is_positioned())
        self.assertEqual(clip.to_dict()["transform"], {"x": None, "y": None})

    # 位置を与えると is_positioned が真になる (\pos 付与の判定)
    def test_subtitle_position_explicit(self):
        clip = SubtitleClip("s9", 0.0, 1.0, "text", transform=Transform(x=0.25, y=-0.5))
        self.assertTrue(clip.transform.is_positioned())

    # 既存資産が扱う item 形式へ変換できる
    def test_to_item_matches_existing_shape(self):
        item = SubtitleClip("s9", 1.5, 2.0, "本文", role="comment",
                            font="Meiryo", font_size=80).to_item()
        self.assertEqual(item["start"], 1.5)
        self.assertEqual(item["end"], 3.5)
        self.assertEqual(item["role"], "comment")
        self.assertEqual(item["font_size"], 80)
        self.assertTrue(item["use"])


class QuantizeTest(unittest.TestCase):
    """§3-3: フレーム量子化は絶対値に対して独立に行い誤差を累積させない"""

    def test_quantize_is_independent_per_value(self):
        timeline = Timeline(fps=60)
        # 1/60 秒に満たないズレは同じフレームへ落ちる
        self.assertEqual(timeline.to_frames(1.0), 60)
        self.assertEqual(timeline.to_frames(1.008), 60)
        # 長時間側でも絶対値から直接求めるため誤差が積み上がらない
        self.assertEqual(timeline.to_frames(3600.0), 216000)
        self.assertAlmostEqual(timeline.quantize(3600.004), 3600.0, places=6)


if __name__ == "__main__":
    unittest.main()
