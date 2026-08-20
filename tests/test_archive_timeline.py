# アーカイブ用 Timeline の構築・分割・逆変換 (src/archive/timeline_builder.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver3/resolve5.md §10.1):
#   ・選ばれたクリップが TOP 順に隙間なく 1 本へ並ぶこと (§3-1)
#   ・字幕がクリップの開始位置ぶんだけずれて載ること
#   ・書き出し時にクリップ単位へ切り直せること (§3-8)。0 起点へ戻ること
#   ・使用しないクリップ (enabled=False) が書き出しから外れること
#   ・Resolve 出力の entry へ戻せること (§5.7)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import timeline_builder
from src.timeline.model import (
    MEDIA_VIDEO,
    ORIGIN_SILENCE_CUT,
    ORIGIN_USER_MEDIA,
    TRACK_VIDEO,
    Clip,
    MediaRef,
    Track,
)

# OP/ED を無効化した設定コピー相当 (build_clip_settings の戻り値と同じ形)
_CLIP_SETTINGS = {
    "general": {"opening_enabled": False, "ending_enabled": False,
                "opening_video": "op.mp4", "ending_video": "ed.mp4"},
    "ffmpeg": {"output_width": 1920, "output_height": 1080, "output_fps": 60},
}


# ffprobe を起こさずに素材情報を返す (テストは構築ロジックだけを見る)
def _fake_probe(path, media_id, settings=None, cfg=None):
    return MediaRef(media_id, MEDIA_VIDEO, path, duration_sec=60.0,
                    width=1920, height=1080, fps=60, has_audio=True)


# クリップ 3 本ぶんの prepared を作る。
# 各クリップは素材内 [0,5) と [10,15) を残す = 尺 10 秒。
def _prepared(count=3, keep=None, items=True):
    entries = []
    for i in range(1, count + 1):
        entries.append({
            "index": i,
            "start": 100.0 * i, "end": 100.0 * i + 30.0, "score": 90.0 - i,
            "normalized_path": f"clip{i}.mp4",
            "normalized_duration": 30.0,
            "keep_segments": [(0.0, 5.0), (10.0, 15.0)] if keep is None else keep,
            "items": ([{"start": 1.0, "end": 3.0, "text": f"clip{i}-a", "use": True,
                        "role": "streamer"},
                       {"start": 6.0, "end": 8.0, "text": f"clip{i}-b", "use": True,
                        "role": "sub"}] if items else []),
            "profile": {"width": 1920, "height": 1080, "orientation": "landscape"},
            "eff_cfg": {"enabled": True},
        })
    return entries


class BuildTest(unittest.TestCase):

    def setUp(self):
        self._real_probe = timeline_builder.media_probe.probe
        timeline_builder.media_probe.probe = _fake_probe

    def tearDown(self):
        timeline_builder.media_probe.probe = self._real_probe

    def _build(self, prepared=None):
        return timeline_builder.build_archive_timeline(
            prepared if prepared is not None else _prepared(), _CLIP_SETTINGS,
            source_path="vod.mp4")

    # 残す区間がそのまま V1 クリップになる
    def test_segments_become_clips(self):
        timeline = self._build()
        clips = timeline.base_video_track().clips
        self.assertEqual(len(clips), 6)          # 3 クリップ × 2 区間
        for clip in clips:
            self.assertAlmostEqual(clip.duration, 5.0, places=6)
        self.assertAlmostEqual(clips[0].source_in, 0.0, places=6)
        self.assertAlmostEqual(clips[0].source_out, 5.0, places=6)
        self.assertAlmostEqual(clips[1].source_in, 10.0, places=6)
        self.assertAlmostEqual(clips[1].source_out, 15.0, places=6)

    # TOP 順に隙間なく並ぶ
    def test_clips_are_lined_up_without_gaps(self):
        timeline = self._build()
        cursor = 0.0
        for clip in timeline.base_video_track().clips:
            self.assertAlmostEqual(clip.timeline_start, cursor, places=6)
            cursor = clip.timeline_end
        self.assertAlmostEqual(timeline.duration_sec(), 30.0, places=6)   # 3 × 10 秒

    # どのアーカイブクリップ由来かが記録される
    def test_origin_keeps_clip_index(self):
        timeline = self._build()
        indexes = [c.origin.get(timeline_builder.ORIGIN_ARCHIVE_INDEX)
                   for c in timeline.base_video_track().clips]
        self.assertEqual(indexes, [1, 1, 2, 2, 3, 3])

    # 音声は V1 へリンクする (時刻は持たない / R18)
    def test_audio_links_to_video(self):
        timeline = self._build()
        video = timeline.base_video_track()
        audio = timeline.base_audio_track()
        self.assertEqual(len(audio.clips), len(video.clips))
        for audio_clip in audio.clips:
            self.assertIsNotNone(timeline.clip_by_id(audio_clip.link_clip))

    # 字幕はクリップの開始位置ぶんだけ後ろへずれる
    def test_subtitles_are_offset_per_clip(self):
        timeline = self._build()
        subtitles = timeline.base_subtitle_track().clips
        self.assertEqual(len(subtitles), 6)
        starts = [round(s.timeline_start, 3) for s in subtitles]
        # clip1 は +0 / clip2 は +10 / clip3 は +20
        self.assertEqual(starts, [1.0, 6.0, 11.0, 16.0, 21.0, 26.0])
        self.assertEqual(subtitles[2].text, "clip2-a")

    # 無音カット無効 (keep_segments 無し) はクリップ全長 1 本になる
    def test_without_keep_segments(self):
        timeline = self._build(_prepared(count=1, keep=[]))
        clips = timeline.base_video_track().clips
        self.assertEqual(len(clips), 1)
        self.assertAlmostEqual(clips[0].duration, 30.0, places=6)

    # OP/ED は入らない (clip_settings で無効化されているため / §3-3)
    def test_no_opening_ending(self):
        timeline = self._build()
        origins = {c.origin_type() for c in timeline.base_video_track().clips}
        self.assertEqual(origins, {ORIGIN_SILENCE_CUT})

    # 字幕がクリップの Timeline 範囲からはみ出さない (docs/error/20260811/resolve.md §9-3)
    # items と V1 が同一の軸 (残す区間を詰めた後) であることの検証。
    # 実カット後ファイルへ認識していた頃は後方の字幕が範囲を超えてズレていた。
    def test_subtitles_stay_within_their_clip(self):
        timeline = self._build()
        for index in (1, 2, 3):
            span = timeline_builder.clip_range(timeline, index)
            subtitles = [s for s in timeline.base_subtitle_track().clips
                         if s.origin.get(timeline_builder.ORIGIN_ARCHIVE_INDEX) == index]
            self.assertTrue(subtitles)
            for subtitle in subtitles:
                self.assertGreaterEqual(subtitle.timeline_start, span[0] - 1e-6)
                self.assertLessEqual(subtitle.timeline_end, span[1] + 1e-6)

    # 字幕の由来時刻が残す区間の内側へ逆算できる (§9-3)
    # 軸がずれていると詰めた後の時刻が区間長を超え、to_source が None になり得る。
    def test_subtitle_origin_maps_back_into_segments(self):
        timeline = self._build()
        subtitles = timeline.base_subtitle_track().clips
        for subtitle in subtitles:
            self.assertIn("source_start", subtitle.origin)
            self.assertIn("source_end", subtitle.origin)
        # clip1: 詰めた後 1.0-3.0 → 素材 1.0-3.0 / 6.0-8.0 → 素材 11.0-13.0
        self.assertAlmostEqual(subtitles[0].origin["source_start"], 1.0, places=3)
        self.assertAlmostEqual(subtitles[1].origin["source_start"], 11.0, places=3)
        self.assertAlmostEqual(subtitles[1].origin["source_end"], 13.0, places=3)

    # 採点グラフとの対応付けが残る
    def test_archive_meta(self):
        timeline = self._build()
        meta = timeline.source["archive"]["clips"]
        self.assertEqual([m["index"] for m in meta], [1, 2, 3])
        self.assertAlmostEqual(meta[1]["timeline_start"], 10.0, places=6)
        self.assertAlmostEqual(meta[1]["timeline_end"], 20.0, places=6)
        self.assertAlmostEqual(meta[1]["vod_start"], 200.0, places=6)


class ClipBoundaryTest(unittest.TestCase):
    # クリップ終端をまたぐ字幕の扱い (docs/error/20260812/resolve2.md §5-1)
    # 整形 (adjust_display_timing) は各クリップ最後の字幕を「発話末 +2 秒」まで伸ばすため、
    # 打ち切らないと次クリップの領域へ重なって表示・焼き込みされる。

    def setUp(self):
        self._real_probe = timeline_builder.media_probe.probe
        timeline_builder.media_probe.probe = _fake_probe

    def tearDown(self):
        timeline_builder.media_probe.probe = self._real_probe

    # clip1 の items だけ差し替えた Timeline を組む (クリップ尺は 10 秒)
    def _build(self, items):
        prepared = _prepared(count=2)
        prepared[0]["items"] = items
        return timeline_builder.build_archive_timeline(
            prepared, _CLIP_SETTINGS, source_path="vod.mp4")

    def _subtitles_of(self, timeline, index):
        return [s for s in timeline.base_subtitle_track().clips
                if s.origin.get(timeline_builder.ORIGIN_ARCHIVE_INDEX) == index]

    # クリップ終端を越える表示は終端で打ち切る (Q1: 短縮)
    def test_subtitle_is_trimmed_at_clip_end(self):
        timeline = self._build([{"start": 9.5, "end": 12.0, "text": "はみ出し",
                                 "use": True, "role": "streamer"}])
        subtitles = self._subtitles_of(timeline, 1)
        self.assertEqual(len(subtitles), 1)
        self.assertAlmostEqual(subtitles[0].timeline_start, 9.5, places=6)
        self.assertAlmostEqual(subtitles[0].timeline_end, 10.0, places=6)

    # 完全にクリップの外へ出る字幕は載せない
    def test_subtitle_outside_clip_is_dropped(self):
        timeline = self._build([{"start": 10.5, "end": 12.0, "text": "外",
                                 "use": True, "role": "streamer"}])
        self.assertEqual(self._subtitles_of(timeline, 1), [])
        # clip2 の字幕は影響を受けない
        self.assertEqual(len(self._subtitles_of(timeline, 2)), 2)

    # 全クリップの末尾がはみ出す条件でも字幕どうしが重ならない (Q2 / I3)
    def test_no_overlapping_subtitles(self):
        prepared = _prepared(count=3)
        for entry in prepared:
            entry["items"] = [{"start": 8.0, "end": 12.0, "text": "末尾",
                               "use": True, "role": "streamer"}]
        timeline = timeline_builder.build_archive_timeline(
            prepared, _CLIP_SETTINGS, source_path="vod.mp4")
        subtitles = sorted(timeline.base_subtitle_track().clips,
                           key=lambda s: s.timeline_start)
        self.assertEqual(len(subtitles), 3)
        for previous, current in zip(subtitles, subtitles[1:]):
            self.assertLessEqual(previous.timeline_end, current.timeline_start + 1e-6)

    # 字幕が Timeline の全長を伸ばさない (映像の終端を超えない / I2)
    def test_subtitles_do_not_extend_timeline(self):
        prepared = _prepared(count=2)
        for entry in prepared:
            entry["items"] = [{"start": 9.0, "end": 15.0, "text": "末尾",
                               "use": True, "role": "streamer"}]
        timeline = timeline_builder.build_archive_timeline(
            prepared, _CLIP_SETTINGS, source_path="vod.mp4")
        self.assertAlmostEqual(timeline.duration_sec(), 20.0, places=6)


class SplitTest(unittest.TestCase):

    def setUp(self):
        self._real_probe = timeline_builder.media_probe.probe
        timeline_builder.media_probe.probe = _fake_probe
        self.timeline = timeline_builder.build_archive_timeline(
            _prepared(), _CLIP_SETTINGS, source_path="vod.mp4")

    def tearDown(self):
        timeline_builder.media_probe.probe = self._real_probe

    # クリップ単位へ切り直せる
    def test_splits_into_clips(self):
        groups = timeline_builder.split_by_clip(self.timeline)
        self.assertEqual([index for index, _tl in groups], [1, 2, 3])
        for _index, sub in groups:
            self.assertEqual(len(sub.base_video_track().clips), 2)
            self.assertEqual(len(sub.base_subtitle_track().clips), 2)

    # 各グループが 0 起点へ戻る
    def test_groups_are_rebased(self):
        groups = timeline_builder.split_by_clip(self.timeline)
        for _index, sub in groups:
            clips = sub.base_video_track().clips
            self.assertAlmostEqual(clips[0].timeline_start, 0.0, places=6)
            self.assertAlmostEqual(sub.duration_sec(), 10.0, places=6)
            self.assertAlmostEqual(
                sub.base_subtitle_track().clips[0].timeline_start, 1.0, places=6)

    # 使用しないクリップ (enabled=False) は書き出しから外れる
    def test_disabled_clip_is_excluded(self):
        for clip in self.timeline.base_video_track().clips:
            if clip.origin.get(timeline_builder.ORIGIN_ARCHIVE_INDEX) == 2:
                clip.enabled = False
        groups = timeline_builder.split_by_clip(self.timeline)
        self.assertEqual([index for index, _tl in groups], [1, 3])

    # クリップを跨いで移動した区間は独立したグループになる (resolve5 §9.1 の割り切り)
    def test_moved_clip_becomes_own_group(self):
        clips = sorted(self.timeline.base_video_track().clips,
                       key=lambda c: c.timeline_start)
        moved = clips[-1]                     # clip3 の後半を先頭より前へ動かす
        moved.timeline_start = -5.0
        groups = timeline_builder.split_by_clip(self.timeline)
        self.assertEqual([index for index, _tl in groups], [3, 1, 2, 3])

    # 境界を跨ぐ字幕は両グループへ、それぞれ見えている分だけ入る
    # (開始時刻だけで振り分けると丸ごと隣のクリップへ移ってしまう / resolve2.md §5-2)
    def test_subtitle_crossing_boundary_is_trimmed_per_group(self):
        subtitle = self.timeline.base_subtitle_track().clips[0]   # clip1-a
        subtitle.timeline_start = 9.0                             # 9.0-11.0 = 境界 10.0 を跨ぐ
        subtitle.duration = 2.0
        self.timeline.normalize()
        groups = dict(timeline_builder.split_by_clip(self.timeline))
        first = [s for s in groups[1].base_subtitle_track().clips if s.text == "clip1-a"]
        second = [s for s in groups[2].base_subtitle_track().clips if s.text == "clip1-a"]
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertAlmostEqual(first[0].timeline_start, 9.0, places=6)
        self.assertAlmostEqual(first[0].timeline_end, 10.0, places=6)
        self.assertAlmostEqual(second[0].timeline_start, 0.0, places=6)
        self.assertAlmostEqual(second[0].timeline_end, 1.0, places=6)

    # 各グループの字幕がグループの範囲に収まる (I4)
    def test_group_subtitles_stay_in_range(self):
        for _index, sub in timeline_builder.split_by_clip(self.timeline):
            for subtitle in sub.base_subtitle_track().clips:
                self.assertGreaterEqual(subtitle.timeline_start, -1e-6)
                self.assertLessEqual(subtitle.timeline_end, sub.duration_sec() + 1e-6)

    # オーバーレイ (D&D で足した素材) も範囲ごとに振り分けられる
    def test_overlay_goes_to_its_group(self):
        overlay = Track("V2", TRACK_VIDEO, 2, name="Video 2", clips=[
            Clip("ov1", "m2", 12.0, 3.0, 0.0, 3.0,
                 origin={"type": ORIGIN_USER_MEDIA}),
        ])
        self.timeline.tracks.append(overlay)
        groups = dict(timeline_builder.split_by_clip(self.timeline))
        overlays = [t for t in groups[2].video_tracks() if t.id == "V2"]
        self.assertEqual(len(overlays), 1)
        self.assertAlmostEqual(overlays[0].clips[0].timeline_start, 2.0, places=6)
        self.assertFalse([t for t in groups[1].video_tracks() if t.id == "V2"])


class ExportEntryTest(unittest.TestCase):

    def setUp(self):
        self._real_probe = timeline_builder.media_probe.probe
        timeline_builder.media_probe.probe = _fake_probe
        self.prepared = _prepared()
        self.timeline = timeline_builder.build_archive_timeline(
            self.prepared, _CLIP_SETTINGS, source_path="vod.mp4")

    def tearDown(self):
        timeline_builder.media_probe.probe = self._real_probe

    # 編集後の Timeline から Resolve 出力用の entry へ戻せる
    def test_entry_from_timeline(self):
        groups = dict(timeline_builder.split_by_clip(self.timeline))
        entry = timeline_builder.to_export_entry(
            2, groups[2], self.prepared[1], theme="テーマ")
        self.assertEqual(entry["index"], 2)
        self.assertAlmostEqual(entry["start"], 200.0, places=6)
        self.assertEqual(entry["keep_segments"], [(0.0, 5.0), (10.0, 15.0)])
        self.assertEqual([i["text"] for i in entry["items"]], ["clip2-a", "clip2-b"])
        self.assertEqual(entry["theme"], "テーマ")

    # トリムした結果が keep_segments へ反映される
    def test_entry_reflects_trim(self):
        for clip in self.timeline.base_video_track().clips:
            if clip.origin.get(timeline_builder.ORIGIN_ARCHIVE_INDEX) == 1:
                clip.source_out = clip.source_in + 2.0
                clip.duration = 2.0
        self.timeline.normalize()
        groups = dict(timeline_builder.split_by_clip(self.timeline))
        entry = timeline_builder.to_export_entry(1, groups[1], self.prepared[0])
        self.assertEqual(entry["keep_segments"], [(0.0, 2.0), (10.0, 12.0)])

    # 字幕の個別指定 (色) が Resolve 出力用の entry まで運ばれる (ver3 resolve6 §3-5)
    def test_entry_keeps_subtitle_colors(self):
        for track in self.timeline.subtitle_tracks():
            for subtitle in track.clips:
                subtitle.color = "#FFE24B"
                subtitle.outline_color = "&H00202020"
        groups = dict(timeline_builder.split_by_clip(self.timeline))
        entry = timeline_builder.to_export_entry(1, groups[1], self.prepared[0])
        self.assertTrue(entry["items"])
        for item in entry["items"]:
            self.assertEqual(item["color"], "#FFE24B")
            self.assertEqual(item["outline_color"], "&H00202020")

    # 色を指定していない字幕は従来どおりキーを持たない (出力は現行と同一)
    def test_entry_without_colors(self):
        groups = dict(timeline_builder.split_by_clip(self.timeline))
        entry = timeline_builder.to_export_entry(1, groups[1], self.prepared[0])
        for item in entry["items"]:
            self.assertNotIn("color", item)
            self.assertNotIn("outline_color", item)
            # 既存の鍵はそのまま揃っている
            for key in ("start", "end", "text", "use", "role", "font", "font_size"):
                self.assertIn(key, item)

    # D&D で足した素材は元 VOD の区間ではないため除外する
    def test_user_media_is_excluded(self):
        groups = dict(timeline_builder.split_by_clip(self.timeline))
        sub = groups[1]
        sub.base_video_track().clips.append(
            Clip("ov1", "m9", 10.0, 3.0, 0.0, 3.0, origin={"type": ORIGIN_USER_MEDIA}))
        entry = timeline_builder.to_export_entry(1, sub, self.prepared[0])
        self.assertEqual(entry["keep_segments"], [(0.0, 5.0), (10.0, 15.0)])


# テーマの編集コマンド (ver3 resolve9 §3-3)
# テーマは source.archive.clips[].theme にあり、コマンド経由で書き換えることで
# Undo と「未保存の印」に載る。
class ArchiveThemeCommandTest(unittest.TestCase):

    def setUp(self):
        prepared = [
            {"index": 1, "start": 100.0, "end": 130.0, "score": 70.0,
             "normalized_path": "clip1.mp4", "normalized_duration": 30.0,
             "keep_segments": [(0.0, 30.0)], "items": [],
             "profile": {"width": 1920, "height": 1080, "orientation": "landscape"}},
        ]
        self._real_probe = timeline_builder.media_probe.probe
        timeline_builder.media_probe.probe = _fake_probe
        try:
            self.timeline = timeline_builder.build_archive_timeline(
                prepared, _CLIP_SETTINGS, source_path="vod.mp4",
                curve=[{"start": 0.0, "end": 180.0, "emotion": 40.0,
                        "comment": 10.0, "total": 26.5, "extra": "捨てられる"}])
        finally:
            timeline_builder.media_probe.probe = self._real_probe

    def test_theme_is_stored_in_source(self):
        self.assertEqual(timeline_builder.clip_theme(self.timeline, 1), "")
        command = timeline_builder.SetArchiveClipTheme(1, " 神回 ")
        self.assertTrue(command.apply(self.timeline))
        self.assertEqual(timeline_builder.clip_theme(self.timeline, 1), "神回")

    def test_same_theme_reports_no_change(self):
        timeline_builder.SetArchiveClipTheme(1, "A").apply(self.timeline)
        # 変化なし → 履歴を汚さない
        self.assertFalse(timeline_builder.SetArchiveClipTheme(1, "A").apply(self.timeline))

    def test_unknown_clip_reports_no_change(self):
        self.assertFalse(
            timeline_builder.SetArchiveClipTheme(99, "X").apply(self.timeline))

    def test_curve_is_trimmed_and_saved(self):
        curve = timeline_builder.archive_section(self.timeline)["curve"]
        self.assertEqual(len(curve), 1)
        self.assertEqual(sorted(curve[0].keys()),
                         ["comment", "emotion", "end", "start", "total"])

    def test_media_role_defaults_to_normalized(self):
        entry = timeline_builder.clip_entry(self.timeline, 1)
        self.assertEqual(entry["media_role"], "normalized")


if __name__ == "__main__":
    unittest.main()
