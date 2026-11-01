# 縦プロジェクトでのテロップの配置 (ver9 resolve15 §3.5 / §3.6) の単体テスト
# 実行: python -m unittest discover -s tests
#
# 背景:
#   従来は「最初に重なったクリップ」だけを採ってその端で打ち切っていたため、
#   無音カットの境目をまたぐテロップが尻切れになっていた。実データ 3 本で
#   27 件 / 46.6 秒、3 件 / 4.2 秒、4 件 / 10.4 秒 が失われていた (§1.6)。
#
# 重点:
#   ・繋がっているクリップの境目では切らないこと (R4 / §3.5)
#   ・1 フレーム未満のずれ・重なりは縫い目として繋ぐこと
#   ・選んでいない区間を挟んだところでは分けて、それぞれ正しい位置へ置くこと
#   ・切り詰めた・落としたものを知らせること (§3.6)
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.timeline import project_io, vertical_builder
from src.timeline.model import (
    ORIGIN_SILENCE_CUT,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    AudioClip,
    Clip,
    MediaRef,
    SubtitleClip,
    Timeline,
    Track,
)


# 隙間なく並ぶ c1(0〜5) / c2(5〜9) / c3(9〜12)。アーカイブ用と同じ形で、
# テロップから見ると境目が 2 つある。
#   s1  1.0〜 3.0  c1 の中
#   s2  4.0〜 6.0  c1→c2 をまたぐ
#   s3  8.0〜11.0  c2→c3 をまたぐ
#   s4 11.2〜12.0  c3 の中
# (同じトラックのテロップは時間が重ならない = 実際のプロジェクトと同じ条件)
def _timeline(gap=0.0):
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 5.0, 10.0, 15.0, origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c2", "m1", 5.0 + gap, 4.0, 40.0, 44.0,
             origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c3", "m1", 9.0 + gap, 3.0, 60.0, 63.0,
             origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=[
        AudioClip("a1", "c1"), AudioClip("a2", "c2"), AudioClip("a3", "c3"),
    ])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1", clips=[
        SubtitleClip("s1", 1.0, 2.0, "c1 の中"),
        SubtitleClip("s2", 4.0, 2.0, "またぎ 1"),
        SubtitleClip("s3", 8.0, 3.0, "またぎ 2"),
        SubtitleClip("s4", 11.2, 0.8, "c3 の中"),
    ])
    source = {"media_id": "m1", "input_path": os.path.abspath(__file__)}
    return Timeline(fps=60, width=1920, height=1080, source=source,
                    media_pool=media, tracks=[video, audio, subtitle])


# テロップを (文字, 開始, 尺) の一覧にする
def _layout_of(timeline):
    return [(c.text, round(c.timeline_start, 3), round(c.duration, 3))
            for track in timeline.subtitle_tracks() for c in track.clips]


class ContiguousTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _timeline()
        self.vertical, self.warnings = vertical_builder.build(
            self.timeline, ["c1", "c2", "c3"], None, {}, close_gaps=True)

    # 繋がっているクリップの境目では切られないこと (R4 / §3.5)
    def test_subtitles_are_not_cut_at_seams(self):
        self.assertEqual(_layout_of(self.vertical),
                         [("c1 の中", 1.0, 2.0),
                          ("またぎ 1", 4.0, 2.0),      # 従来は 1.0 秒へ尻切れ
                          ("またぎ 2", 8.0, 3.0),      # 従来は 1.0 秒へ尻切れ
                          ("c3 の中", 11.2, 0.8)])

    # 全部そろっていれば警告は出ないこと (§3.6)
    def test_no_warnings(self):
        self.assertEqual(self.warnings, [])

    # 欠片に分かれないので ID も元のまま
    def test_ids_are_kept(self):
        self.assertEqual([c.id for c in self.vertical.subtitle_tracks()[0].clips],
                         ["s1", "s2", "s3", "s4"])

    # 先頭クリップぶん前へ寄っても、映像との相対位置は変わらないこと
    def test_relative_to_video(self):
        timeline = _timeline()
        for clip in timeline.base_video_track().clips:
            clip.timeline_start += 20.0        # Timeline ごと後ろへずらす
        for clip in timeline.subtitle_tracks()[0].clips:
            clip.timeline_start += 20.0

        vertical, _warnings = vertical_builder.build(
            timeline, ["c1", "c2", "c3"], None, {}, close_gaps=True)

        self.assertEqual(_layout_of(vertical), _layout_of(self.vertical))

    # 保存して開き直しても配置が変わらないこと
    def test_round_trip(self):
        dest = os.path.join(tempfile.mkdtemp(), "v.timeline.json")
        project_io.save(self.vertical, dest, generator="vertical", project_path=dest)

        self.assertEqual(_layout_of(project_io.load(dest)),
                         _layout_of(self.vertical))


class SeamToleranceTest(unittest.TestCase):

    # 1 フレーム未満の隙間は縫い目として繋ぐこと (§3.5)
    def test_small_gap_is_joined(self):
        vertical, _warnings = vertical_builder.build(
            _timeline(gap=0.001), ["c1", "c2", "c3"], None, {}, close_gaps=False)

        texts = {text: (start, duration)
                 for text, start, duration in _layout_of(vertical)}
        self.assertAlmostEqual(texts["またぎ 1"][1], 2.0, places=3)

    # 本当の隙間 (1 フレーム以上) は縫い目とみなさないこと
    def test_real_gap_splits(self):
        vertical, warnings = vertical_builder.build(
            _timeline(gap=0.5), ["c1", "c2", "c3"], None, {}, close_gaps=False)

        pieces = [row for row in _layout_of(vertical) if row[0] == "またぎ 1"]
        self.assertEqual(len(pieces), 2)
        self.assertAlmostEqual(sum(row[2] for row in pieces), 1.5, places=3)
        self.assertTrue(any("切り詰め" in text for text in warnings), warnings)


class SelectionGapTest(unittest.TestCase):

    def setUp(self):
        # c2 を選ばない = c1 と c3 の間に切れ目ができる
        self.timeline = _timeline()
        self.vertical, self.warnings = vertical_builder.build(
            self.timeline, ["c1", "c3"], None, {}, close_gaps=True)

    # 選んでいない区間にかかる部分だけが切り詰められること (§3.5)
    def test_subtitle_is_trimmed_at_the_selection_edge(self):
        self.assertEqual(_layout_of(self.vertical),
                         [("c1 の中", 1.0, 2.0),
                          ("またぎ 1", 4.0, 1.0),     # c1 の側だけ残る
                          ("またぎ 2", 5.0, 2.0),     # c3 の側は詰めた位置へ
                          ("c3 の中", 7.2, 0.8)])

    # 切り詰めたことを知らせること (§3.6)
    def test_trimmed_is_reported(self):
        self.assertTrue(any("切り詰め" in text for text in self.warnings),
                        self.warnings)

    # 分かれた欠片に重複 ID を残さないこと (_renumber)
    def test_ids_are_unique(self):
        ids = [c.id for track in self.vertical.subtitle_tracks() for c in track.clips]

        self.assertEqual(len(ids), len(set(ids)))

    # 両側に残るテロップは、空白を詰めると隣り合うので 1 本に繋がること (§3.5)
    def test_pieces_are_joined_when_gaps_are_closed(self):
        timeline = _timeline()
        # c1 の終わりから c3 の始まりまでをまたぐ長いテロップ
        timeline.subtitle_tracks()[0].clips = [SubtitleClip("s9", 4.0, 6.0, "長い")]

        vertical, warnings = vertical_builder.build(
            timeline, ["c1", "c3"], None, {}, close_gaps=True)

        # c1 側 1.0 秒 + c3 側 1.0 秒。間の c2 は選んでいないので消える
        self.assertEqual(_layout_of(vertical), [("長い", 4.0, 2.0)])
        self.assertTrue(any("切り詰め" in text for text in warnings), warnings)

    # 空白を残す指定では、同じテロップが 2 本に分かれること
    def test_pieces_stay_apart_when_gaps_are_kept(self):
        timeline = _timeline()
        timeline.subtitle_tracks()[0].clips = [SubtitleClip("s9", 4.0, 6.0, "長い")]

        vertical, _warnings = vertical_builder.build(
            timeline, ["c1", "c3"], None, {}, close_gaps=False)

        self.assertEqual(_layout_of(vertical), [("長い", 4.0, 1.0), ("長い", 9.0, 1.0)])


class DropTest(unittest.TestCase):

    # 選んだ範囲の外のテロップは落として知らせること (従来どおり)
    def test_dropped_outside(self):
        timeline = _timeline()
        timeline.subtitle_tracks()[0].clips = [SubtitleClip("s9", 6.0, 1.0, "c2 の中")]

        vertical, warnings = vertical_builder.build(
            timeline, ["c1", "c3"], None, {}, close_gaps=True)

        self.assertEqual(_layout_of(vertical), [])
        self.assertTrue(any("引き継ぎませんでした" in text for text in warnings),
                        warnings)

    # 切れ目で短くなりすぎた欠片は捨てること (_MIN_SUBTITLE_SEC)
    def test_tiny_piece_is_dropped(self):
        timeline = _timeline()
        # c1 の終端 (5.0) を 0.01 秒だけまたぐ = c1 側の欠片は見えない
        timeline.subtitle_tracks()[0].clips = [SubtitleClip("s9", 4.99, 1.0, "ほぼ c2")]

        vertical, _warnings = vertical_builder.build(
            timeline, ["c1", "c3"], None, {}, close_gaps=True)

        self.assertEqual(_layout_of(vertical), [])

    # 使わない (use=False) テロップは件数に数えないこと
    def test_unused_is_not_counted(self):
        timeline = _timeline()
        timeline.subtitle_tracks()[0].clips = [
            SubtitleClip("s9", 6.0, 1.0, "c2 の中", use=False)]

        _vertical, warnings = vertical_builder.build(
            timeline, ["c1", "c3"], None, {}, close_gaps=True)

        self.assertEqual(warnings, [])


class MapSpansTest(unittest.TestCase):

    # 区間へ写す関数そのものの振る舞い (§3.5)
    def test_map_spans(self):
        offsets = [(0.0, 0.0, 5.0), (5.0, 5.0, 4.0)]      # 隙間なく繋がる 2 本

        self.assertEqual(vertical_builder._map_spans(4.0, 6.0, offsets),
                         [(4.0, 6.0)])

    def test_map_spans_without_overlap(self):
        self.assertEqual(vertical_builder._map_spans(
            20.0, 21.0, [(0.0, 0.0, 5.0)]), [])

    # 縦 Timeline 上で隣り合う欠片は 1 本に繋ぐ (空白を詰めた場合)
    def test_map_spans_joins_packed_pieces(self):
        offsets = [(0.0, 0.0, 5.0), (9.0, 5.0, 3.0)]      # c2 を抜いて詰めた形

        self.assertEqual(vertical_builder._map_spans(4.0, 10.0, offsets),
                         [(4.0, 6.0)])

    # 縦 Timeline 上でも離れている欠片は分かれたままにする (空白を残した場合)
    def test_map_spans_splits_at_real_gap(self):
        offsets = [(0.0, 0.0, 5.0), (9.0, 9.0, 3.0)]      # c2 を抜いて空白を残した形

        self.assertEqual(vertical_builder._map_spans(4.0, 10.0, offsets),
                         [(4.0, 5.0), (9.0, 10.0)])


if __name__ == "__main__":
    unittest.main()
