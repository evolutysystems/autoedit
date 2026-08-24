# Timeline ノードのコピー＆ペースト (src/timeline/clipboard.py / commands.PasteClips) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver3/resolve10.md §8):
#   ・干渉しないなら 1 クリップも動かさない (要望 P4)
#   ・干渉したときは「必要な分だけ」右へずらす = 穴が空かない (要望 P3)
#   ・V1 へ貼るときは字幕・オーバーレイも同量ずれる (要望 P14)
#   ・貼り付け後も A1 が V1 と一致していること (R18)
import os
import unittest

from src.timeline import builder, clipboard, commands
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

# 実在する 2 つのパス (素材の再登録テストで別素材として扱うため別ファイルにする)
_VIDEO_PATH = __file__
_IMAGE_PATH = os.path.join(os.path.dirname(__file__), "__init__.py")


# V1 に本編 3 クリップ (各 10 秒・隙間なし)、V2 にオーバーレイ 2 枚、S1 に字幕 2 本。
#   V1: c1[0,10) c2[10,20) c3[20,30)
#   V2: o1[5,8)  o2[30,33)
#   S1: s1[1,4)  s2[12,15)
def _build():
    media = [
        MediaRef("m1", "video", _VIDEO_PATH, 100.0, 1920, 1080, 60, True),
        MediaRef("m2", "image", _IMAGE_PATH, None, 800, 600, 0, False),
    ]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 10.0, 0.0, 10.0, origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c2", "m1", 10.0, 10.0, 20.0, 30.0, origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c3", "m1", 20.0, 10.0, 50.0, 60.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=[
        AudioClip("a1", "c1"), AudioClip("a2", "c2"), AudioClip("a3", "c3"),
    ])
    overlay = Track("V2", TRACK_VIDEO, 2, name="Video 2", clips=[
        Clip("o1", "m2", 5.0, 3.0, 0.0, 3.0, z_order=10),
        Clip("o2", "m2", 30.0, 3.0, 0.0, 3.0, z_order=20),
    ])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1", clips=[
        SubtitleClip("s1", 1.0, 3.0, "字幕1"),
        SubtitleClip("s2", 12.0, 3.0, "字幕2"),
    ])
    return Timeline(fps=60, source={"media_id": "m1", "duration_sec": 100.0},
                    media_pool=media, tracks=[video, audio, overlay, subtitle])


# 素材を持たない空の Timeline (別画面へ貼るときの再登録を確かめる)
def _build_empty():
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=[])
    return Timeline(fps=60, media_pool=[], tracks=[video, audio])


# 全トラックの (クリップID → 開始秒) を控える。「動いていないこと」の比較に使う。
def _positions(timeline):
    return {clip.id: clip.timeline_start
            for track in timeline.tracks if not track.is_audio()
            for clip in track.clips}


# A1 の全クリップが V1 の対応クリップと一致していることを確かめる (R18 の不変条件)
def _assert_audio_in_sync(case, timeline):
    audio_track = timeline.base_audio_track()
    for audio in audio_track.clips:
        video = timeline.clip_by_id(audio.link_clip)
        case.assertIsNotNone(video, f"{audio.id} のリンク先が失われている")
        case.assertAlmostEqual(audio.timeline_start(timeline), video.timeline_start, places=6)
        case.assertAlmostEqual(audio.duration(timeline), video.duration, places=6)
    case.assertEqual(len(audio_track.clips), len(timeline.base_video_track().clips))


# 同一トラック内にクリップの重なりが無いことを確かめる (割り込みの不変条件①)
def _assert_no_overlap(case, timeline):
    for track in timeline.tracks:
        if track.is_audio():
            continue
        clips = sorted(track.clips, key=lambda c: c.timeline_start)
        for earlier, later in zip(clips, clips[1:]):
            case.assertLessEqual(
                earlier.timeline_end, later.timeline_start + 1e-6,
                f"{track.id}: {earlier.id} と {later.id} が重なっている")


# 貼り付けを 1 回実行する (履歴には積まない)。戻り値はコマンド。
def _paste(timeline, payload, at_sec, **kwargs):
    kwargs.setdefault("min_clip_sec", _MIN)
    command = commands.PasteClips(payload, at_sec, **kwargs)
    command.apply(timeline)
    timeline.normalize()
    return command


class PayloadTest(unittest.TestCase):
    """T1 / T2: コピーした内容の作られ方"""

    def setUp(self):
        self.timeline = _build()
        clipboard.clear()

    # T1: 単一選択
    def test_single(self):
        payload = clipboard.build_payload(self.timeline, ["c2"])
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["kind"], clipboard.KIND_VIDEO)
        self.assertAlmostEqual(item["offset_sec"], 0.0)
        self.assertAlmostEqual(payload["span_sec"], 10.0)
        self.assertEqual(item["track"]["id"], "V1")
        self.assertTrue(item["track"]["is_base"])
        self.assertEqual(item["media"]["path"], _VIDEO_PATH)

    # T1: 複数選択は相対位置で控える
    def test_multi_keeps_relative_positions(self):
        payload = clipboard.build_payload(self.timeline, ["c2", "s2"])
        offsets = {i["clip"]["id"]: i["offset_sec"] for i in payload["items"]}
        self.assertAlmostEqual(offsets["c2"], 0.0)     # アンカー = 最も早い c2 (10.0)
        self.assertAlmostEqual(offsets["s2"], 2.0)     # 12.0 - 10.0
        # 全体の尺 = 最大終端 (c2 の 20.0) - アンカー (10.0)
        self.assertAlmostEqual(payload["span_sec"], 10.0)

    # T1: 音声クリップ ID を渡すとリンク元の V1 クリップへ読み替わる (§3-6)
    def test_audio_id_maps_to_video(self):
        payload = clipboard.build_payload(self.timeline, ["a2"])
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["clip"]["id"], "c2")

    # T1: リンク音声の属性を控える
    def test_audio_attributes(self):
        self.timeline.clip_by_id("a2").gain_db = -3.0
        self.timeline.clip_by_id("a2").muted = True
        item = clipboard.build_payload(self.timeline, ["c2"])["items"][0]
        self.assertAlmostEqual(item["audio"]["gain_db"], -3.0)
        self.assertTrue(item["audio"]["muted"])

    def test_empty_selection(self):
        self.assertIsNone(clipboard.build_payload(self.timeline, []))
        self.assertIsNone(clipboard.build_payload(self.timeline, ["missing"]))

    # T2: 値で控えるため、コピー後に元クリップを編集・削除しても内容が変わらない
    def test_payload_is_by_value(self):
        self.assertEqual(clipboard.copy_clips(self.timeline, ["c2"]), 1)
        payload = clipboard.payload()
        self.timeline.clip_by_id("c2").timeline_start = 99.0
        self.timeline.track_by_id("V1").remove_clip("c2")
        self.assertAlmostEqual(payload["items"][0]["clip"]["timeline_start"], 10.0)
        self.assertAlmostEqual(payload["items"][0]["clip"]["duration"], 10.0)

    def test_clipboard_state(self):
        self.assertTrue(clipboard.is_empty())
        clipboard.copy_clips(self.timeline, ["c2"])
        self.assertFalse(clipboard.is_empty())
        clipboard.clear()
        self.assertTrue(clipboard.is_empty())


class PasteWithoutOverlapTest(unittest.TestCase):
    """T3 / T4: 干渉しない貼り付け = Timeline は 1 か所も動かない (要望 P4)"""

    def setUp(self):
        self.timeline = _build()

    # T3: 内容がそのまま複製される
    def test_paste_copies_content(self):
        payload = clipboard.build_payload(self.timeline, ["o1"])
        command = _paste(self.timeline, payload, 20.0)
        self.assertEqual(len(command.created_clip_ids), 1)
        clip = self.timeline.clip_by_id(command.created_clip_ids[0])
        self.assertEqual(self.timeline.track_of_clip(clip.id).id, "V2")
        self.assertAlmostEqual(clip.timeline_start, 20.0)
        self.assertAlmostEqual(clip.duration, 3.0)
        self.assertAlmostEqual(clip.source_in, 0.0)
        self.assertAlmostEqual(clip.source_out, 3.0)
        self.assertEqual(clip.z_order, 10)
        self.assertEqual(clip.media_id, "m2")
        self.assertNotEqual(clip.id, "o1")           # ID は採番し直す

    # T4 (最重要): V2 の空き位置へ貼ると、どのトラックのクリップも動かない
    def test_no_clip_moves(self):
        before = _positions(self.timeline)
        payload = clipboard.build_payload(self.timeline, ["o1"])
        command = _paste(self.timeline, payload, 20.0)
        self.assertAlmostEqual(command.shifted_sec, 0.0)
        after = _positions(self.timeline)
        for clip_id, start in before.items():
            self.assertAlmostEqual(after[clip_id], start,
                                   msg=f"{clip_id} が動いている")
        # 貼ったぶんだけクリップが増えただけ
        self.assertEqual(len(after), len(before) + 1)

    # T3: 字幕の内容 (色・フォント) も運ばれる
    def test_paste_subtitle(self):
        self.timeline.clip_by_id("s1").color = "#FF0000"
        self.timeline.clip_by_id("s1").font = "Meiryo"
        payload = clipboard.build_payload(self.timeline, ["s1"])
        command = _paste(self.timeline, payload, 20.0)
        clip = self.timeline.clip_by_id(command.created_clip_ids[0])
        self.assertEqual(self.timeline.track_of_clip(clip.id).id, "S1")
        self.assertEqual(clip.text, "字幕1")
        self.assertEqual(clip.color, "#FF0000")
        self.assertEqual(clip.font, "Meiryo")
        self.assertAlmostEqual(command.shifted_sec, 0.0)


class PasteWithOverlapTest(unittest.TestCase):
    """T5 / T6 / T7: 干渉したときは「必要な分だけ」そのトラックだけずらす"""

    def setUp(self):
        self.timeline = _build()

    # T5 (最重要): ずらし量は最小限で、貼り付け終端と既存の開始がぴったり合う (穴が空かない)
    def test_shift_is_minimal(self):
        payload = clipboard.build_payload(self.timeline, ["o1"])   # 3 秒
        command = _paste(self.timeline, payload, 4.0)              # 区間 [4, 7)
        # o1 は 5.0 開始 → 7.0 まで押し出すので 2.0 だけずらせば足りる
        self.assertAlmostEqual(command.shifted_sec, 2.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("o1").timeline_start, 7.0)
        pasted = self.timeline.clip_by_id(command.created_clip_ids[0])
        self.assertAlmostEqual(pasted.timeline_end, 7.0)           # 穴が無い
        _assert_no_overlap(self, self.timeline)

    # T6: 後続ノードも同量動くので、トラック内の間隔が保たれる
    def test_following_clips_keep_spacing(self):
        gap_before = (self.timeline.clip_by_id("o2").timeline_start
                      - self.timeline.clip_by_id("o1").timeline_end)
        payload = clipboard.build_payload(self.timeline, ["o1"])
        _paste(self.timeline, payload, 4.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("o2").timeline_start, 32.0)
        gap_after = (self.timeline.clip_by_id("o2").timeline_start
                     - self.timeline.clip_by_id("o1").timeline_end)
        self.assertAlmostEqual(gap_after, gap_before)

    # T7 (要望 P4): V2 へ貼っても V1・S1 は 1 つも動かない
    def test_other_tracks_do_not_move(self):
        before = _positions(self.timeline)
        payload = clipboard.build_payload(self.timeline, ["o1"])
        _paste(self.timeline, payload, 4.0)
        for clip_id in ("c1", "c2", "c3", "s1", "s2"):
            self.assertAlmostEqual(_positions(self.timeline)[clip_id], before[clip_id],
                                   msg=f"{clip_id} が動いている")

    # required_shift() 単体: 干渉が無ければ 0
    def test_required_shift_zero_without_overlap(self):
        track = self.timeline.track_by_id("V2")
        self.assertAlmostEqual(
            commands.required_shift(track, 20.0, 23.0, _MIN), 0.0)
        self.assertAlmostEqual(
            commands.required_shift(track, 4.0, 7.0, _MIN), 2.0)


class PasteIntoBaseTrackTest(unittest.TestCase):
    """T8 / T9: V1 へ貼ると字幕・オーバーレイも同量ずれる (要望 P14)"""

    def setUp(self):
        self.timeline = _build()
        self.payload = clipboard.build_payload(self.timeline, ["c1"])   # 10 秒

    # T8 (最重要): 全トラックが同量ずれる
    def test_base_paste_shifts_all_tracks(self):
        command = _paste(self.timeline, self.payload, 5.0)     # 区間 [5, 15)
        self.assertAlmostEqual(command.shifted_sec, 10.0)
        # V1: c1 は 5.0 で分割され、後半が右へ。c2 / c3 も 10 秒ずれる
        self.assertAlmostEqual(self.timeline.clip_by_id("c1").timeline_start, 0.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("c1").duration, 5.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("c2").timeline_start, 20.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("c3").timeline_start, 30.0)
        # S1 / V2 も同じ 10 秒だけ動く (置いていかれない)
        self.assertAlmostEqual(self.timeline.clip_by_id("s2").timeline_start, 22.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("o1").timeline_start, 15.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("o2").timeline_start, 40.0)
        # 挿入点より前のものは動かない
        self.assertAlmostEqual(self.timeline.clip_by_id("s1").timeline_start, 1.0)
        _assert_no_overlap(self, self.timeline)

    # T8: 字幕と映像の相対位置が貼り付け前後で変わらない
    def test_subtitle_keeps_relative_position(self):
        before = (self.timeline.clip_by_id("s2").timeline_start
                  - self.timeline.clip_by_id("c2").timeline_start)
        _paste(self.timeline, self.payload, 5.0)
        after = (self.timeline.clip_by_id("s2").timeline_start
                 - self.timeline.clip_by_id("c2").timeline_start)
        self.assertAlmostEqual(after, before)

    # T9: 跨いだクリップは分割され、リンク音声も 2 本になる
    def test_split_keeps_audio_in_sync(self):
        _paste(self.timeline, self.payload, 5.0)
        base = self.timeline.base_video_track()
        # 元の 3 本 + 分割で 1 本 + 貼り付けで 1 本
        self.assertEqual(len(base.clips), 5)
        _assert_audio_in_sync(self, self.timeline)

    # T9: 分割した後半は素材内の位置も正しくずれる
    def test_split_keeps_source_position(self):
        _paste(self.timeline, self.payload, 5.0)
        tail = next(c for c in self.timeline.base_video_track().clips
                    if abs(c.timeline_start - 15.0) < 1e-6)
        self.assertAlmostEqual(tail.source_in, 5.0)
        self.assertAlmostEqual(tail.source_out, 10.0)

    # T12: 貼り付けた V1 クリップにリンク音声が付き、属性が復元される
    def test_pasted_clip_gets_linked_audio(self):
        self.timeline.clip_by_id("a1").gain_db = -6.0
        self.timeline.clip_by_id("a1").muted = True
        payload = clipboard.build_payload(self.timeline, ["c1"])
        command = _paste(self.timeline, payload, 5.0)
        audio = self.timeline.audio_clip_for(command.created_clip_ids[0])
        self.assertIsNotNone(audio)
        self.assertAlmostEqual(audio.gain_db, -6.0)
        self.assertTrue(audio.muted)

    # T12: 画像 (音声なし) には音声クリップを作らない
    def test_image_gets_no_audio(self):
        payload = clipboard.build_payload(self.timeline, ["o1"])
        command = _paste(self.timeline, payload, 20.0)
        self.assertIsNone(self.timeline.audio_clip_for(command.created_clip_ids[0]))

    # V1 に十分な隙間があれば、V1 へ貼っても誰も動かない
    def test_no_shift_when_base_has_room(self):
        # c3 の後ろ (30 秒以降) は空いている
        before = _positions(self.timeline)
        command = _paste(self.timeline, self.payload, 40.0)
        self.assertAlmostEqual(command.shifted_sec, 0.0)
        for clip_id, start in before.items():
            self.assertAlmostEqual(_positions(self.timeline)[clip_id], start)


class RippleScopeTest(unittest.TestCase):
    """T8b: ripple_scope の切り替え"""

    def setUp(self):
        self.timeline = _build()

    # track: V1 へ貼っても字幕は動かない
    def test_track_scope_leaves_subtitles(self):
        payload = clipboard.build_payload(self.timeline, ["c1"])
        _paste(self.timeline, payload, 5.0, ripple_scope=commands.SCOPE_TRACK)
        self.assertAlmostEqual(self.timeline.clip_by_id("s2").timeline_start, 12.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("o1").timeline_start, 5.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("c2").timeline_start, 20.0)

    # all: V2 へ貼っただけで全トラックが動く
    def test_all_scope_shifts_everything(self):
        payload = clipboard.build_payload(self.timeline, ["o1"])
        command = _paste(self.timeline, payload, 4.0, ripple_scope=commands.SCOPE_ALL)
        self.assertAlmostEqual(command.shifted_sec, 2.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("s2").timeline_start, 14.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("c2").timeline_start, 12.0)

    # base_syncs_all (既定): V2 へ貼るときは他トラックを巻き込まない
    def test_base_syncs_all_leaves_others_for_overlay(self):
        payload = clipboard.build_payload(self.timeline, ["o1"])
        _paste(self.timeline, payload, 4.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("s2").timeline_start, 12.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("c2").timeline_start, 10.0)

    # ロックしたトラックは同期対象でも動かさない (ロックを優先する / §3-5)
    def test_locked_track_is_not_shifted(self):
        self.timeline.track_by_id("S1").locked = True
        payload = clipboard.build_payload(self.timeline, ["c1"])
        _paste(self.timeline, payload, 5.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("s2").timeline_start, 12.0)
        self.assertAlmostEqual(self.timeline.clip_by_id("o1").timeline_start, 15.0)


class InsertEdgeCaseTest(unittest.TestCase):
    """T10 / T11: 跨ぎクリップの端と insert_policy"""

    def setUp(self):
        self.timeline = _build()
        self.payload = clipboard.build_payload(self.timeline, ["c1"])   # 10 秒

    # T10: 頭が最小尺未満 → 丸ごと右へ寄せる (重なりを残さない)
    def test_short_head_moves_whole_clip(self):
        _paste(self.timeline, self.payload, 10.02)
        self.assertEqual(len(self.timeline.base_video_track().clips), 4)  # 分割されない
        self.assertAlmostEqual(self.timeline.clip_by_id("c2").duration, 10.0)
        _assert_no_overlap(self, self.timeline)
        _assert_audio_in_sync(self, self.timeline)

    # T10: 尻が最小尺未満 → その位置で切り詰める (動かさない)
    def test_short_tail_trims_clip(self):
        _paste(self.timeline, self.payload, 19.98)
        c2 = self.timeline.clip_by_id("c2")
        self.assertAlmostEqual(c2.timeline_end, 19.98)
        self.assertAlmostEqual(c2.source_out, c2.source_in + c2.duration)
        _assert_no_overlap(self, self.timeline)

    # T11: shift_whole は跨ぎクリップを分割せず丸ごと動かす
    def test_shift_whole_policy(self):
        _paste(self.timeline, self.payload, 5.0,
               insert_policy=commands.INSERT_SHIFT_WHOLE)
        self.assertEqual(len(self.timeline.base_video_track().clips), 4)
        self.assertAlmostEqual(self.timeline.clip_by_id("c1").timeline_start, 15.0)
        _assert_no_overlap(self, self.timeline)


class MediaResolveTest(unittest.TestCase):
    """T13: 別 Timeline へ貼るときの素材の再登録"""

    def setUp(self):
        self.source = _build()
        self.payload = clipboard.build_payload(self.source, ["c1"])

    def test_media_is_registered(self):
        target = _build_empty()
        command = _paste(target, self.payload, 0.0)
        self.assertEqual(len(command.created_clip_ids), 1)
        self.assertEqual(len(target.media_pool), 1)
        self.assertEqual(target.media_pool[0].path, _VIDEO_PATH)
        # 同じパスなら 2 回目は増えない
        _paste(target, self.payload, 30.0)
        self.assertEqual(len(target.media_pool), 1)

    # T13: 実ファイルが無ければ貼らず、場所も空けない
    def test_missing_media_is_skipped(self):
        payload = clipboard.build_payload(self.source, ["c1"])
        payload["items"][0]["media"]["path"] = os.path.join(
            os.path.dirname(__file__), "存在しない素材.mp4")
        target = _build()
        before = _positions(target)
        command = commands.PasteClips(payload, 5.0, min_clip_sec=_MIN)
        self.assertFalse(command.apply(target))       # 何も起きない
        self.assertEqual(command.skipped, 1)
        self.assertAlmostEqual(command.shifted_sec, 0.0)
        self.assertEqual(_positions(target), before)


class LockedTrackTest(unittest.TestCase):
    """T15: ロックしたトラックへは貼らない"""

    def setUp(self):
        self.timeline = _build()

    def test_locked_target_is_skipped(self):
        self.timeline.track_by_id("V2").locked = True
        before = _positions(self.timeline)
        payload = clipboard.build_payload(self.timeline, ["o1"])
        command = commands.PasteClips(payload, 4.0, min_clip_sec=_MIN)
        self.assertFalse(command.apply(self.timeline))
        self.assertEqual(command.skipped, 1)
        self.assertEqual(_positions(self.timeline), before)


class UndoTest(unittest.TestCase):
    """T14 / T18: 履歴"""

    def setUp(self):
        self.timeline = _build()
        self.stack = commands.CommandStack()

    def test_undo_restores_everything(self):
        before = _positions(self.timeline)
        before_clips = sum(len(t.clips) for t in self.timeline.tracks)
        before_duration = self.timeline.duration_sec()
        payload = clipboard.build_payload(self.timeline, ["c1"])
        self.assertTrue(self.stack.push(
            self.timeline, commands.PasteClips(payload, 5.0, min_clip_sec=_MIN)))
        self.assertGreater(self.timeline.duration_sec(), before_duration)

        self.assertEqual(self.stack.undo(self.timeline), "貼り付け")
        self.assertEqual(_positions(self.timeline), before)
        self.assertEqual(sum(len(t.clips) for t in self.timeline.tracks), before_clips)
        self.assertAlmostEqual(self.timeline.duration_sec(), before_duration)
        _assert_audio_in_sync(self, self.timeline)

    # T18: 1 件も貼れないときは履歴を汚さない (Timeline も変わらない)
    def test_nothing_pasted_is_not_recorded(self):
        self.assertFalse(self.stack.push(
            self.timeline, commands.PasteClips({"items": []}, 5.0, min_clip_sec=_MIN)))
        self.assertFalse(self.stack.can_undo())


class ArchiveIndexTest(unittest.TestCase):
    """T16: アーカイブ用 V1 の archive_clip_index (§3-8)"""

    def setUp(self):
        self.timeline = _build()
        # アーカイブ用の Timeline に見せかけ、V1 を clip1 / clip2 の 2 グループにする
        self.timeline.source["archive"] = {"clips": [{"index": 1}, {"index": 2}]}
        for clip_id, index in (("c1", 1), ("c2", 1), ("c3", 2)):
            self.timeline.clip_by_id(clip_id).origin["archive_clip_index"] = index

    # inherit: 直前のクリップから引き継ぐのでグループ境界が増えない
    def test_inherit_keeps_group_boundaries(self):
        payload = clipboard.build_payload(self.timeline, ["c3"])   # index 2 の素材
        command = _paste(self.timeline, payload, 5.0)              # index 1 の途中へ
        pasted = self.timeline.clip_by_id(command.created_clip_ids[0])
        self.assertEqual(pasted.origin["archive_clip_index"], 1)
        self.assertEqual(self._group_count(), 2)

    # keep: 元の index のままなのでグループが割れる (挙動の明文化)
    def test_keep_splits_groups(self):
        payload = clipboard.build_payload(self.timeline, ["c3"])
        command = _paste(self.timeline, payload, 5.0,
                         archive_index_policy=commands.ARCHIVE_INDEX_KEEP)
        pasted = self.timeline.clip_by_id(command.created_clip_ids[0])
        self.assertEqual(pasted.origin["archive_clip_index"], 2)
        self.assertGreater(self._group_count(), 2)

    # クリップ用 (source.archive を持たない) では何もしない
    def test_clip_timeline_keeps_origin(self):
        del self.timeline.source["archive"]
        payload = clipboard.build_payload(self.timeline, ["c3"])
        command = _paste(self.timeline, payload, 5.0)
        pasted = self.timeline.clip_by_id(command.created_clip_ids[0])
        self.assertEqual(pasted.origin["archive_clip_index"], 2)

    # V1 を index で区切ったときのグループ数 (archive/timeline_builder.split_by_clip 相当)
    def _group_count(self):
        groups, current = 0, object()
        for clip in sorted(self.timeline.base_video_track().clips,
                           key=lambda c: c.timeline_start):
            index = clip.origin.get("archive_clip_index")
            if index != current:
                groups += 1
                current = index
        return groups


class SettingsTest(unittest.TestCase):
    """T17: 設定の既定と検証"""

    def test_paste_defaults(self):
        cfg = builder.timeline_config({})["paste"]
        self.assertEqual(cfg["ripple_scope"], "base_syncs_all")
        self.assertEqual(cfg["insert_policy"], "split")
        self.assertEqual(cfg["archive_index_policy"], "inherit")
        self.assertTrue(cfg["move_playhead_to_end"])
        self.assertTrue(cfg["select_pasted"])

    def test_invalid_values_fall_back(self):
        cfg = builder.timeline_config({"timeline": {"paste": {
            "ripple_scope": "なにか", "insert_policy": 123,
            "archive_index_policy": "",
        }}})["paste"]
        self.assertEqual(cfg["ripple_scope"], "base_syncs_all")
        self.assertEqual(cfg["insert_policy"], "split")
        self.assertEqual(cfg["archive_index_policy"], "inherit")

    def test_valid_values_are_kept(self):
        cfg = builder.timeline_config({"timeline": {"paste": {
            "ripple_scope": "track", "insert_policy": "shift_whole",
            "archive_index_policy": "keep", "select_pasted": False,
        }}})["paste"]
        self.assertEqual(cfg["ripple_scope"], "track")
        self.assertEqual(cfg["insert_policy"], "shift_whole")
        self.assertEqual(cfg["archive_index_policy"], "keep")
        self.assertFalse(cfg["select_pasted"])

    # 貼り付けは 1 種類だけ。Ctrl+Shift+V の項目は存在しない (rev2 R-1)
    def test_shortcuts(self):
        keys = builder.timeline_config({})["shortcuts"]
        self.assertEqual(keys["copy"], "Ctrl+C")
        self.assertEqual(keys["paste"], "Ctrl+V")
        self.assertNotIn("paste_insert", keys)

    # 既定に無い action は捨てられるため、書いても復活しない
    def test_paste_insert_cannot_be_added(self):
        keys = builder.timeline_config(
            {"timeline": {"shortcuts": {"paste_insert": "Ctrl+Shift+V"}}})["shortcuts"]
        self.assertNotIn("paste_insert", keys)


if __name__ == "__main__":
    unittest.main()
