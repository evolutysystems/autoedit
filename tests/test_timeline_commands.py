# 編集操作コマンド (src/timeline/commands.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (resolve.md §14 Phase 6 の完了条件):
#   ・全操作の後で A1 の時刻が V1 と一致していること (R18)
#   ・Split / Delete のリンク張り直しが正しいこと
#   ・「削除」と「リップル削除」が別の結果になること (R17 / 回答 Q5)
import unittest

from src.timeline import commands
from src.timeline.model import (
    AudioClip,
    Clip,
    MediaRef,
    ORIGIN_SILENCE_CUT,
    SubtitleClip,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    Timeline,
    Track,
)

_MIN = 0.05


# 本編 3 クリップ (各 10 秒・隙間なし) の Timeline を組む
def _build():
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 10.0, 0.0, 10.0, origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c2", "m1", 10.0, 10.0, 20.0, 30.0, origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c3", "m1", 20.0, 10.0, 50.0, 60.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=[
        AudioClip("a1", "c1"), AudioClip("a2", "c2"), AudioClip("a3", "c3"),
    ])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1", clips=[
        SubtitleClip("s1", 1.0, 3.0, "字幕1"),
    ])
    return Timeline(fps=60, source={"media_id": "m1", "duration_sec": 100.0},
                    media_pool=media, tracks=[video, audio, subtitle])


# A1 の全クリップが V1 の対応クリップと完全に一致していることを確かめる (R18 の不変条件)
def _assert_audio_in_sync(case, timeline):
    audio_track = timeline.base_audio_track()
    for audio in audio_track.clips:
        video = timeline.clip_by_id(audio.link_clip)
        case.assertIsNotNone(video, f"{audio.id} のリンク先が失われている")
        case.assertAlmostEqual(audio.timeline_start(timeline), video.timeline_start, places=6)
        case.assertAlmostEqual(audio.duration(timeline), video.duration, places=6)
        case.assertAlmostEqual(audio.source_in(timeline), video.source_in, places=6)
    # 映像クリップの数と音声クリップの数が一致する (取りこぼし・余りが無い)
    case.assertEqual(len(audio_track.clips), len(timeline.base_video_track().clips))


class MoveTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build()
        self.stack = commands.CommandStack()

    # 空きへ移動できる
    def test_move_into_free_space(self):
        self.timeline.base_video_track().remove_clip("c2")
        self.timeline.base_audio_track().remove_clip("a2")
        ok = self.stack.push(self.timeline, commands.MoveClip("c3", 12.0, _MIN))
        self.assertTrue(ok)
        self.assertAlmostEqual(self.timeline.clip_by_id("c3").timeline_start, 12.0)

    # 他クリップと重なる位置へは置けない (近い側の境界へ寄る)
    def test_move_resolves_collision(self):
        self.stack.push(self.timeline, commands.MoveClip("c3", 12.0, _MIN))
        clip = self.timeline.clip_by_id("c3")
        self.assertNotAlmostEqual(clip.timeline_start, 12.0)
        # どの 2 クリップも重ならない
        clips = self.timeline.base_clips()
        for prev, nxt in zip(clips, clips[1:]):
            self.assertLessEqual(prev.timeline_end, nxt.timeline_start + 1e-6)

    # 移動後も音声が同期している (追随処理を書いていないのに一致する = R18)
    def test_audio_follows_move(self):
        self.timeline.base_video_track().remove_clip("c2")
        self.timeline.base_audio_track().remove_clip("a2")
        self.stack.push(self.timeline, commands.MoveClip("c3", 12.0, _MIN))
        _assert_audio_in_sync(self, self.timeline)


class TrimTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build()
        self.stack = commands.CommandStack()

    # 右端を縮めると duration と source_out が同時に変わる
    def test_trim_right(self):
        self.stack.push(self.timeline, commands.TrimClip("c1", commands.EDGE_RIGHT, 6.0, _MIN))
        clip = self.timeline.clip_by_id("c1")
        self.assertAlmostEqual(clip.duration, 6.0)
        self.assertAlmostEqual(clip.source_out, 6.0)
        _assert_audio_in_sync(self, self.timeline)

    # 右端は直後クリップを超えられない
    def test_trim_right_stops_at_next_clip(self):
        self.stack.push(self.timeline, commands.TrimClip("c1", commands.EDGE_RIGHT, 15.0, _MIN))
        self.assertAlmostEqual(self.timeline.clip_by_id("c1").timeline_end, 10.0)

    # 左端を縮めると timeline_start と source_in が同量動く
    def test_trim_left(self):
        self.stack.push(self.timeline, commands.TrimClip("c2", commands.EDGE_LEFT, 13.0, _MIN))
        clip = self.timeline.clip_by_id("c2")
        self.assertAlmostEqual(clip.timeline_start, 13.0)
        self.assertAlmostEqual(clip.duration, 7.0)
        self.assertAlmostEqual(clip.source_in, 23.0)
        _assert_audio_in_sync(self, self.timeline)

    # 素材の先頭より前へは伸ばせない
    def test_trim_left_stops_at_source_head(self):
        self.stack.push(self.timeline, commands.TrimClip("c1", commands.EDGE_LEFT, -5.0, _MIN))
        self.assertAlmostEqual(self.timeline.clip_by_id("c1").timeline_start, 0.0)

    # 素材の終端より後ろへは伸ばせない
    def test_trim_right_stops_at_source_tail(self):
        timeline = _build()
        # 単独クリップにして直後クリップの制限を外す
        timeline.base_video_track().clips = [timeline.clip_by_id("c3")]
        timeline.base_audio_track().clips = [timeline.audio_clip_for("c3")]
        # c3 は source 50.0〜60.0、素材は 100.0 まで → 最大 +40 秒
        commands.CommandStack().push(
            timeline, commands.TrimClip("c3", commands.EDGE_RIGHT, 999.0, _MIN))
        self.assertAlmostEqual(timeline.clip_by_id("c3").source_out, 100.0)

    # 最小尺を割るトリムはできない
    def test_trim_respects_min_clip_sec(self):
        self.stack.push(self.timeline, commands.TrimClip("c1", commands.EDGE_RIGHT, 0.001, _MIN))
        self.assertGreaterEqual(self.timeline.clip_by_id("c1").duration, _MIN - 1e-9)


class SplitTest(unittest.TestCase):
    """R18 の明示的な追随①: 分割ではリンク音声も 2 つへ分ける"""

    def setUp(self):
        self.timeline = _build()
        self.stack = commands.CommandStack()

    # 分割すると 2 クリップになり、隙間なく並ぶ
    def test_split_creates_two_clips(self):
        self.assertTrue(self.stack.push(self.timeline, commands.SplitClip("c2", 15.0, _MIN)))
        clips = self.timeline.base_clips()
        self.assertEqual(len(clips), 4)
        first = self.timeline.clip_by_id("c2")
        self.assertAlmostEqual(first.duration, 5.0)
        self.assertAlmostEqual(first.source_out, 25.0)
        # 後半は source_in が引き継がれる
        latter = [c for c in clips if abs(c.timeline_start - 15.0) < 1e-6][0]
        self.assertAlmostEqual(latter.source_in, 25.0)
        self.assertAlmostEqual(latter.source_out, 30.0)

    # 音声クリップも 2 つになり、両方が正しくリンクされる
    def test_split_splits_linked_audio(self):
        self.stack.push(self.timeline, commands.SplitClip("c2", 15.0, _MIN))
        _assert_audio_in_sync(self, self.timeline)
        self.assertEqual(len(self.timeline.base_audio_track().clips), 4)

    # 端すぎる位置では分割しない
    def test_split_rejects_edge_position(self):
        self.assertFalse(self.stack.push(self.timeline, commands.SplitClip("c2", 10.01, _MIN)))
        self.assertEqual(len(self.timeline.base_clips()), 3)

    # 字幕クリップも分割できる (音声リンクは無い)
    def test_split_subtitle(self):
        self.assertTrue(self.stack.push(self.timeline, commands.SplitClip("s1", 2.0, _MIN)))
        self.assertEqual(len(self.timeline.subtitle_clips()), 2)


class DeleteTest(unittest.TestCase):
    """R17 / 回答 Q5: 削除とリップル削除は別の結果になる"""

    def setUp(self):
        self.timeline = _build()
        self.stack = commands.CommandStack()

    # 通常削除は空白を残す
    def test_delete_leaves_gap(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=False))
        self.assertEqual([c.id for c in self.timeline.base_clips()], ["c1", "c3"])
        self.assertAlmostEqual(self.timeline.clip_by_id("c3").timeline_start, 20.0)
        gaps = self.timeline.base_gaps()
        self.assertEqual(len(gaps), 1)
        self.assertAlmostEqual(gaps[0][0], 10.0)
        self.assertAlmostEqual(gaps[0][1], 20.0)

    # リップル削除は後続を詰めるので空白が残らない
    def test_ripple_delete_closes_gap(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=True))
        self.assertEqual([c.id for c in self.timeline.base_clips()], ["c1", "c3"])
        self.assertAlmostEqual(self.timeline.clip_by_id("c3").timeline_start, 10.0)
        self.assertEqual(self.timeline.base_gaps(), [])

    # 2 種類の結果が実際に異なる (これが別コマンドである理由)
    def test_two_delete_modes_differ(self):
        plain = _build()
        commands.CommandStack().push(plain, commands.DeleteClip(["c2"], ripple=False))
        ripple = _build()
        commands.CommandStack().push(ripple, commands.DeleteClip(["c2"], ripple=True))
        self.assertNotAlmostEqual(plain.duration_sec(), ripple.duration_sec())

    # 削除ではリンク音声も一緒に消える (R18 の明示的な追随②)
    def test_delete_removes_linked_audio(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=False))
        self.assertIsNone(self.timeline.audio_clip_for("c2"))
        _assert_audio_in_sync(self, self.timeline)

    # リップル削除の後も音声が同期している
    def test_ripple_delete_keeps_audio_in_sync(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=True))
        _assert_audio_in_sync(self, self.timeline)

    # 複数選択をまとめて削除できる
    def test_delete_multiple(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c1", "c3"], ripple=False))
        self.assertEqual([c.id for c in self.timeline.base_clips()], ["c2"])
        _assert_audio_in_sync(self, self.timeline)

    # 複数のリップル削除でも位置が壊れない (後ろから処理する)
    def test_ripple_delete_multiple(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c1", "c2"], ripple=True))
        self.assertEqual([c.id for c in self.timeline.base_clips()], ["c3"])
        self.assertAlmostEqual(self.timeline.clip_by_id("c3").timeline_start, 0.0)

    # BackSpace 相当 (削除するだけ) は設定に関わらず詰めない (R12)
    def test_plain_delete_is_independent_of_setting(self):
        for ripple_default in (True, False):
            timeline = _build()
            stack = commands.CommandStack()
            # BackSpace のハンドラは常に ripple=False を渡す
            stack.push(timeline, commands.DeleteClip(["c2"], ripple=False))
            self.assertAlmostEqual(
                timeline.clip_by_id("c3").timeline_start, 20.0,
                f"ripple_delete={ripple_default} でも詰めてはいけない")
            self.assertEqual(len(timeline.base_gaps()), 1)

    # 削除するだけでもリンク音声は一緒に消える (R18)
    def test_plain_delete_removes_linked_audio(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=False))
        self.assertIsNone(self.timeline.audio_clip_for("c2"))
        _assert_audio_in_sync(self, self.timeline)

    # 選択が空なら何も起きない (BackSpace の空押し)
    def test_delete_with_empty_selection_is_noop(self):
        self.assertFalse(self.stack.push(self.timeline, commands.DeleteClip([])))
        self.assertEqual(len(self.timeline.base_clips()), 3)


class RippleRangeTest(unittest.TestCase):
    """resolve2 §5.4: リップルは区間を全トラックから抜いて詰める (R8 / 回答 Q2・Q8)"""

    def setUp(self):
        # V1: c1[0,10] c2[10,20] c3[20,30] / V2: o1[5,25] (区間を跨ぐ)
        # S1: s1[1,4](前) s2[11,13](区間内) s3[22,25](後ろ)
        self.timeline = _build()
        self.timeline.tracks.append(Track("V2", TRACK_VIDEO, 2, name="Video 2", clips=[
            Clip("o1", "m1", 5.0, 20.0, 0.0, 20.0, z_order=10,
                 origin={"type": "user_media"}),
        ]))
        subtitle_track = self.timeline.base_subtitle_track()
        subtitle_track.clips.append(SubtitleClip("s2", 11.0, 2.0, "区間内"))
        subtitle_track.clips.append(SubtitleClip("s3", 22.0, 3.0, "区間の後ろ"))
        self.stack = commands.CommandStack()

    # 区間より後ろの字幕・オーバーレイも一緒に詰まる (規則 2)
    def test_ripple_shifts_all_tracks(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=True))
        self.assertAlmostEqual(self.timeline.clip_by_id("c3").timeline_start, 10.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("s3").timeline_start, 12.0)

    # 区間より前は動かない (規則 1)
    def test_ripple_keeps_earlier_clips(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=True))
        self.assertAlmostEqual(self.timeline.clip_by_id("s1").timeline_start, 1.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("c1").timeline_start, 0.0)

    # 区間に完全に含まれる字幕は消える (規則 3)
    def test_ripple_deletes_clips_inside_range(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=True))
        self.assertIsNone(self.timeline.clip_by_id("s2"))

    # 区間を跨ぐクリップは分割せず尺を縮める (規則 6 / 回答 Q8)
    def test_ripple_shortens_spanning_clip_without_split(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=True))
        overlay_track = self.timeline.track_by_id("V2")
        self.assertEqual(len(overlay_track.clips), 1, "分割してはいけない")
        overlay = self.timeline.clip_by_id("o1")
        self.assertAlmostEqual(overlay.timeline_start, 5.0)
        self.assertAlmostEqual(overlay.duration, 10.0)     # 20 - delta(10)
        self.assertAlmostEqual(overlay.source_out, 10.0)

    # 頭だけかかるクリップは右端が区間の開始まで縮む (規則 4)
    def test_ripple_trims_clip_overlapping_head(self):
        overlay = Clip("o2", "m1", 5.0, 8.0, 0.0, 8.0, origin={"type": "user_media"})
        self.timeline.track_by_id("V2").clips = [overlay]
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=True))
        self.assertAlmostEqual(overlay.timeline_start, 5.0)
        self.assertAlmostEqual(overlay.timeline_end, 10.0)
        self.assertAlmostEqual(overlay.source_out, 5.0)

    # 尻だけかかるクリップは左端が縮み、区間の開始位置へ詰まる (規則 5)
    def test_ripple_trims_clip_overlapping_tail(self):
        overlay = Clip("o3", "m1", 15.0, 10.0, 0.0, 10.0, origin={"type": "user_media"})
        self.timeline.track_by_id("V2").clips = [overlay]
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=True))
        self.assertAlmostEqual(overlay.timeline_start, 10.0)
        self.assertAlmostEqual(overlay.duration, 5.0)
        self.assertAlmostEqual(overlay.source_in, 5.0)

    # 音声トラックはリップルの対象外だが V1 からの導出で一致し続ける (R18)
    def test_ripple_keeps_audio_in_sync(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=True))
        _assert_audio_in_sync(self, self.timeline)

    # 空白を残す削除では他トラックが動かない (リップルとの違い)
    def test_plain_delete_does_not_shift_other_tracks(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=False))
        self.assertAlmostEqual(self.timeline.clip_by_id("s3").timeline_start, 22.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("c3").timeline_start, 20.0)
        self.assertIsNotNone(self.timeline.clip_by_id("s2"), "空白を残す削除では消えない")

    # sync_all=False なら旧挙動 (操作したトラックのみ) に戻せる
    def test_sync_all_false_restores_old_behaviour(self):
        self.stack.push(self.timeline,
                        commands.DeleteClip(["c2"], ripple=True, sync_all=False))
        self.assertAlmostEqual(self.timeline.clip_by_id("c3").timeline_start, 10.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("s3").timeline_start, 22.0)

    # Undo で全トラックが元へ戻る
    def test_undo_restores_all_tracks(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=True))
        self.stack.undo(self.timeline)
        self.assertAlmostEqual(self.timeline.clip_by_id("c3").timeline_start, 20.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("s3").timeline_start, 22.0)
        self.assertIsNotNone(self.timeline.clip_by_id("s2"))
        self.assertAlmostEqual(self.timeline.clip_by_id("o1").duration, 20.0)
        _assert_audio_in_sync(self, self.timeline)


class RippleTrimToPlayheadTest(unittest.TestCase):
    """resolve2 §5.3: A / D = 再生ヘッドを境にした部分リップル削除 (R4・R6)"""

    def setUp(self):
        self.timeline = _build()
        self.timeline.base_subtitle_track().clips.append(
            SubtitleClip("s9", 25.0, 2.0, "後ろの字幕"))
        self.stack = commands.CommandStack()

    # A: 再生ヘッドより前を消す。source_in が進み、後続が詰まる。
    def test_trim_before_playhead(self):
        ok = self.stack.push(self.timeline, commands.RippleTrimToPlayhead(
            "c2", 15.0, "before", _MIN))
        self.assertTrue(ok)
        clip = self.timeline.clip_by_id("c2")
        self.assertAlmostEqual(clip.timeline_start, 10.0)
        self.assertAlmostEqual(clip.duration, 5.0)
        self.assertAlmostEqual(clip.source_in, 25.0)   # 20.0 + 5.0
        self.assertAlmostEqual(self.timeline.clip_by_id("c3").timeline_start, 15.0)
        _assert_audio_in_sync(self, self.timeline)

    # D: 再生ヘッドより後ろを消す。source_out が戻り、後続が詰まる。
    def test_trim_after_playhead(self):
        ok = self.stack.push(self.timeline, commands.RippleTrimToPlayhead(
            "c2", 15.0, "after", _MIN))
        self.assertTrue(ok)
        clip = self.timeline.clip_by_id("c2")
        self.assertAlmostEqual(clip.timeline_start, 10.0)
        self.assertAlmostEqual(clip.duration, 5.0)
        self.assertAlmostEqual(clip.source_in, 20.0)
        self.assertAlmostEqual(clip.source_out, 25.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("c3").timeline_start, 15.0)
        _assert_audio_in_sync(self, self.timeline)

    # 全トラックが一緒に詰まる (R8)
    def test_trim_shifts_all_tracks(self):
        self.stack.push(self.timeline, commands.RippleTrimToPlayhead(
            "c2", 15.0, "after", _MIN))
        self.assertAlmostEqual(self.timeline.clip_by_id("s9").timeline_start, 20.0)

    # 再生ヘッドがクリップの外なら何もしない
    def test_playhead_outside_clip_is_noop(self):
        self.assertFalse(self.stack.push(self.timeline, commands.RippleTrimToPlayhead(
            "c2", 25.0, "before", _MIN)))
        self.assertEqual(len(self.timeline.base_clips()), 3)

    # 再生ヘッドがクリップの端ちょうどなら何もしない
    def test_playhead_at_edge_is_noop(self):
        self.assertFalse(self.stack.push(self.timeline, commands.RippleTrimToPlayhead(
            "c2", 10.0, "before", _MIN)))

    # 残りが最小尺を割る場合はクリップごとリップル削除する (回答 Q4)
    def test_remainder_below_min_deletes_whole_clip(self):
        self.stack.push(self.timeline, commands.RippleTrimToPlayhead(
            "c2", 19.99, "before", _MIN))
        self.assertIsNone(self.timeline.clip_by_id("c2"))
        self.assertEqual([c.id for c in self.timeline.base_clips()], ["c1", "c3"])
        self.assertAlmostEqual(self.timeline.clip_by_id("c3").timeline_start, 10.0)
        _assert_audio_in_sync(self, self.timeline)

    # 字幕クリップにも効く (回答 Q7)
    def test_works_on_subtitle_clip(self):
        ok = self.stack.push(self.timeline, commands.RippleTrimToPlayhead(
            "s9", 26.0, "after", _MIN))
        self.assertTrue(ok)
        self.assertAlmostEqual(self.timeline.clip_by_id("s9").duration, 1.0)

    # 1 回の Undo で全トラックぶん戻る
    def test_undo_restores_everything(self):
        self.stack.push(self.timeline, commands.RippleTrimToPlayhead(
            "c2", 15.0, "after", _MIN))
        self.stack.undo(self.timeline)
        clip = self.timeline.clip_by_id("c2")
        self.assertAlmostEqual(clip.duration, 10.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("c3").timeline_start, 20.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("s9").timeline_start, 25.0)
        _assert_audio_in_sync(self, self.timeline)


class LayerTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build()
        self.timeline.tracks.append(Track("V2", TRACK_VIDEO, 2, name="Video 2", clips=[
            Clip("o1", "m1", 0.0, 5.0, 0.0, 5.0, z_order=10, origin={"type": "user_media"}),
            Clip("o2", "m1", 6.0, 5.0, 0.0, 5.0, z_order=20, origin={"type": "user_media"}),
        ]))
        self.stack = commands.CommandStack()

    def _order(self):
        return [e.id for e in self.timeline.overlay_elements(include_disabled=True)]

    # 初期順は z_order 昇順 (字幕は既定 100)
    def test_initial_order(self):
        self.assertEqual(self._order(), ["o1", "o2", "s1"])

    def test_bring_to_front(self):
        self.stack.push(self.timeline, commands.ChangeZOrder("o1", commands.LAYER_TOP))
        self.assertEqual(self._order()[-1], "o1")

    def test_send_to_back(self):
        self.stack.push(self.timeline, commands.ChangeZOrder("s1", commands.LAYER_BOTTOM))
        self.assertEqual(self._order()[0], "s1")

    def test_bring_forward_one_step(self):
        self.stack.push(self.timeline, commands.ChangeZOrder("o1", commands.LAYER_UP))
        self.assertEqual(self._order(), ["o2", "o1", "s1"])

    def test_send_backward_one_step(self):
        self.stack.push(self.timeline, commands.ChangeZOrder("s1", commands.LAYER_DOWN))
        self.assertEqual(self._order(), ["o1", "s1", "o2"])

    # 既に最前面なら変化しない (履歴も汚さない)
    def test_no_change_at_top(self):
        self.assertFalse(
            self.stack.push(self.timeline, commands.ChangeZOrder("s1", commands.LAYER_TOP)))
        self.assertFalse(self.stack.can_undo())

    # ベース (V1) のクリップはレイヤー変更の対象外
    def test_base_clip_is_not_reorderable(self):
        self.assertFalse(
            self.stack.push(self.timeline, commands.ChangeZOrder("c1", commands.LAYER_TOP)))


class AddMediaTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build()
        self.stack = commands.CommandStack()
        # 元動画 (m1) とは別パスにする。同一パスの素材はプールで再利用されるため。
        self.image = MediaRef("", "image", "D:/assets/logo.png", None, 800, 600, 0, False)
        self.video = MediaRef("", "video", "D:/assets/sub.mp4", 30.0, 1280, 720, 30, True)

    # 画像を追加すると新しい映像トラックができる (V1 は使わない)
    def test_add_image_creates_new_track(self):
        command = commands.AddMediaClip(self.image, 2.0, 5.0)
        self.assertTrue(self.stack.push(self.timeline, command))
        self.assertEqual(command.created_track_id, "V2")
        # 画像は音声を持たないので音声トラックは増えない
        self.assertEqual(len(self.timeline.audio_tracks()), 1)

    # 音声付き動画を追加すると音声トラックも対で作られる
    def test_add_video_creates_audio_track(self):
        command = commands.AddMediaClip(self.video, 2.0, 5.0)
        self.stack.push(self.timeline, command)
        audio_track = self.timeline.audio_track_for(command.created_track_id)
        self.assertIsNotNone(audio_track)
        self.assertEqual(len(audio_track.clips), 1)

    # 同じ位置へ 2 つ置くとさらに別トラックへ回る
    def test_overlapping_media_goes_to_another_track(self):
        first = commands.AddMediaClip(self.image, 2.0, 5.0)
        self.stack.push(self.timeline, first)
        second = commands.AddMediaClip(self.image, 3.0, 5.0)
        self.stack.push(self.timeline, second)
        self.assertNotEqual(first.created_track_id, second.created_track_id)

    # 重ならなければ同じトラックへ載る
    def test_non_overlapping_media_reuses_track(self):
        first = commands.AddMediaClip(self.image, 0.0, 2.0)
        self.stack.push(self.timeline, first)
        second = commands.AddMediaClip(self.image, 5.0, 2.0)
        self.stack.push(self.timeline, second)
        self.assertEqual(first.created_track_id, second.created_track_id)

    # トラック上限を超えると追加を拒否する
    def test_track_limit_is_enforced(self):
        for i in range(3):
            command = commands.AddMediaClip(self.image, 0.0, 5.0, max_video_tracks=3)
            self.stack.push(self.timeline, command)
        # V1 + V2 + V3 で上限。4 本目は作れない
        self.assertEqual(len(self.timeline.video_tracks()), 3)

    # 追加したクリップは既存オーバーレイより前面に来る
    def test_added_clip_is_frontmost(self):
        command = commands.AddMediaClip(self.image, 2.0, 5.0)
        self.stack.push(self.timeline, command)
        self.assertEqual(
            self.timeline.overlay_elements(include_disabled=True)[-1].id,
            command.created_clip_id)


# 右クリック「字幕追加」(AddSubtitleClip)
# S1 には s1 (1.0〜4.0 秒) が 1 つだけ載っている状態から始める。
class AddSubtitleTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build()
        self.stack = commands.CommandStack()

    def _subtitle_track(self):
        return self.timeline.base_subtitle_track()

    # 空いている位置へ既定の尺で置ける
    def test_add_into_free_space(self):
        command = commands.AddSubtitleClip(10.0, 2.0, text="あ", min_clip_sec=_MIN)
        self.assertTrue(self.stack.push(self.timeline, command))
        clip = self.timeline.clip_by_id(command.created_clip_id)
        self.assertIsInstance(clip, SubtitleClip)
        self.assertEqual(self._subtitle_track().clip_by_id(clip.id), clip)
        self.assertAlmostEqual(clip.timeline_start, 10.0)
        self.assertAlmostEqual(clip.duration, 2.0)
        self.assertEqual(clip.text, "あ")

    # 次の字幕に届く位置では、その手前まで縮めて置く (重ねない)
    def test_duration_is_clamped_by_next_clip(self):
        command = commands.AddSubtitleClip(0.0, 2.0, min_clip_sec=_MIN)
        self.assertTrue(self.stack.push(self.timeline, command))
        clip = self.timeline.clip_by_id(command.created_clip_id)
        self.assertAlmostEqual(clip.timeline_end, 1.0)

    # 既存字幕の中では追加しない (履歴も汚さない)
    def test_reject_inside_existing_clip(self):
        self.assertFalse(self.stack.push(
            self.timeline, commands.AddSubtitleClip(2.0, 2.0, min_clip_sec=_MIN)))
        self.assertEqual(len(self._subtitle_track().clips), 1)
        self.assertFalse(self.stack.can_undo())

    # 最小尺ぶんの空きも無ければ追加しない
    def test_reject_when_gap_is_too_small(self):
        self.assertFalse(self.stack.push(
            self.timeline, commands.AddSubtitleClip(0.99, 2.0, min_clip_sec=_MIN)))

    # 字幕トラックが無ければ S1 を作って載せる
    def test_creates_subtitle_track_when_missing(self):
        self.timeline.tracks = [t for t in self.timeline.tracks if not t.is_subtitle()]
        command = commands.AddSubtitleClip(5.0, 2.0, min_clip_sec=_MIN)
        self.assertTrue(self.stack.push(self.timeline, command))
        self.assertEqual(command.created_track_id, "S1")
        self.assertEqual(len(self.timeline.subtitle_tracks()), 1)

    # 追加は Undo で消える (トラックの中身が元へ戻る)
    def test_undo_removes_added_clip(self):
        command = commands.AddSubtitleClip(10.0, 2.0, min_clip_sec=_MIN)
        self.stack.push(self.timeline, command)
        self.stack.undo(self.timeline)
        self.assertIsNone(self.timeline.clip_by_id(command.created_clip_id))
        self.assertEqual(len(self._subtitle_track().clips), 1)

    # 追加後も時系列順が保たれる (normalize が効く)
    def test_clips_stay_sorted(self):
        self.stack.push(self.timeline, commands.AddSubtitleClip(10.0, 2.0, min_clip_sec=_MIN))
        self.stack.push(self.timeline, commands.AddSubtitleClip(6.0, 2.0, min_clip_sec=_MIN))
        starts = [c.timeline_start for c in self._subtitle_track().clips]
        self.assertEqual(starts, sorted(starts))


class UndoRedoTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build()
        self.stack = commands.CommandStack()

    # 分割 → Undo で元に戻る (音声も含めて)
    def test_undo_split(self):
        self.stack.push(self.timeline, commands.SplitClip("c2", 15.0, _MIN))
        self.assertEqual(len(self.timeline.base_clips()), 4)
        self.stack.undo(self.timeline)
        self.assertEqual(len(self.timeline.base_clips()), 3)
        _assert_audio_in_sync(self, self.timeline)

    # リップル削除 → Undo で位置まで戻る
    def test_undo_ripple_delete_restores_positions(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=True))
        self.stack.undo(self.timeline)
        self.assertEqual([c.id for c in self.timeline.base_clips()], ["c1", "c2", "c3"])
        self.assertAlmostEqual(self.timeline.clip_by_id("c3").timeline_start, 20.0)
        _assert_audio_in_sync(self, self.timeline)

    # Undo → Redo で再適用される
    def test_redo(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=True))
        self.stack.undo(self.timeline)
        self.stack.redo(self.timeline)
        self.assertEqual([c.id for c in self.timeline.base_clips()], ["c1", "c3"])
        _assert_audio_in_sync(self, self.timeline)

    # 新しい操作を積むと Redo は捨てられる
    def test_new_command_clears_redo(self):
        self.stack.push(self.timeline, commands.DeleteClip(["c2"], ripple=True))
        self.stack.undo(self.timeline)
        self.assertTrue(self.stack.can_redo())
        self.stack.push(self.timeline, commands.DeleteClip(["c1"], ripple=False))
        self.assertFalse(self.stack.can_redo())

    # 変化しない操作は履歴に積まれない
    def test_no_op_is_not_recorded(self):
        self.assertFalse(self.stack.push(self.timeline, commands.MoveClip("c1", 0.0, _MIN)))
        self.assertFalse(self.stack.can_undo())

    # メディア追加も Undo できる (トラック追加ごと戻る)
    def test_undo_add_media(self):
        image = MediaRef("", "image", "D:/assets/logo.png", None, 800, 600, 0, False)
        self.stack.push(self.timeline, commands.AddMediaClip(image, 2.0, 5.0))
        self.assertEqual(len(self.timeline.video_tracks()), 2)
        self.stack.undo(self.timeline)
        self.assertEqual(len(self.timeline.video_tracks()), 1)


class AudioSettingTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build()
        self.stack = commands.CommandStack()

    # ゲイン変更は A1 固有の値で V1 に影響しない
    def test_gain_does_not_touch_video(self):
        before = self.timeline.clip_by_id("c1").duration
        self.stack.push(self.timeline, commands.SetAudioGain("a1", -6.0))
        self.assertAlmostEqual(self.timeline.clip_by_id("a1").gain_db, -6.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("c1").duration, before)

    def test_mute_toggle(self):
        self.stack.push(self.timeline, commands.SetAudioMuted("a1", True))
        self.assertTrue(self.timeline.clip_by_id("a1").muted)
        self.stack.undo(self.timeline)
        self.assertFalse(self.timeline.clip_by_id("a1").muted)


if __name__ == "__main__":
    unittest.main()
