# プロジェクト JSON 入出力 (src/timeline/project_io.py) の単体テスト
# 実行: python -m unittest discover -s tests
# resolve.md §6.2: スキーマ・検証・マイグレーション。
import json
import os
import shutil
import tempfile
import unittest

from src.exceptions import TimelineError
from src.timeline import project_io
from src.timeline.model import (
    AudioClip,
    Clip,
    MediaRef,
    SubtitleClip,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    Timeline,
    Track,
)


# 実在するダミーメディアを持つ Timeline を組む (メディア欠落の WARNING を避ける)
def _build_timeline(media_path):
    media = [MediaRef("m1", "video", media_path, 100.0, 1920, 1080, 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 12.48, 0.0, 12.48, origin={"type": "silence_cut"}),
        Clip("c2", "m1", 12.48, 33.68, 15.22, 48.9, origin={"type": "silence_cut"}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1", clips=[
        AudioClip("a1", "c1"), AudioClip("a2", "c2", gain_db=-3.0, muted=True),
    ])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1", clips=[
        SubtitleClip("s1", 0.35, 2.1, "テスト字幕",
                     origin={"type": "asr", "source_start": 0.35, "source_end": 2.45}),
    ])
    return Timeline(
        fps=60, width=1920, height=1080,
        source={"input_path": media_path, "media_path": media_path,
                "media_id": "m1", "duration_sec": 100.0},
        edit_points={
            "detector": "silencedetect", "threshold_db": -23, "min_silence_sec": 0.6,
            "source_duration_sec": 100.0,
            "keep_segments": [(0.0, 12.48), (15.22, 48.9)],
        },
        media_pool=media, tracks=[video, audio, subtitle],
    )


class _TempMediaCase(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="timeline_io_test_")
        self.media_path = os.path.join(self._dir, "sample.mp4")
        with open(self.media_path, "wb") as f:
            f.write(b"")
        self.timeline = _build_timeline(self.media_path)

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)


class RoundTripTest(_TempMediaCase):

    # 書き出して読み戻すと構造が保たれる
    def test_round_trip_preserves_structure(self):
        restored = project_io.from_json(project_io.to_json(self.timeline))
        self.assertEqual(restored.fps, 60)
        self.assertEqual([t.id for t in restored.tracks], ["V1", "A1", "S1"])
        self.assertEqual([c.id for c in restored.base_clips()], ["c1", "c2"])
        self.assertAlmostEqual(restored.duration_sec(), 46.16, places=3)

    # 音声クリップのゲイン・ミュートは往復する (時刻は持たない)
    def test_audio_settings_round_trip(self):
        restored = project_io.from_json(project_io.to_json(self.timeline))
        audio = restored.audio_clip_for("c2")
        self.assertAlmostEqual(audio.gain_db, -3.0)
        self.assertTrue(audio.muted)
        # 導出値は映像クリップと一致する
        self.assertAlmostEqual(audio.timeline_start(restored), 12.48, places=3)

    # 字幕の位置未指定 (None) が保たれる
    def test_subtitle_position_none_round_trips(self):
        restored = project_io.from_json(project_io.to_json(self.timeline))
        self.assertFalse(restored.subtitle_clips()[0].transform.is_positioned())

    # ファイルへ保存して読み込める
    def test_save_and_load(self):
        path = os.path.join(self._dir, "sample.timeline.json")
        project_io.save(self.timeline, path, generator="test")
        self.assertTrue(os.path.exists(path))
        restored = project_io.load(path)
        self.assertEqual(len(restored.base_clips()), 2)

    # 一時ファイルを残さない
    def test_save_leaves_no_temp_file(self):
        path = os.path.join(self._dir, "sample.timeline.json")
        project_io.save(self.timeline, path)
        self.assertFalse(os.path.exists(path + ".tmp"))


class SchemaTest(_TempMediaCase):

    def _dump(self):
        return json.loads(project_io.to_json(self.timeline))

    # 必須セクションが揃っている (§6.2.2)
    def test_top_level_sections(self):
        data = self._dump()
        for key in ("schema_version", "timeline", "source", "edit_points",
                    "media_pool", "tracks"):
            self.assertIn(key, data)
        self.assertEqual(data["schema_version"], project_io.SCHEMA_VERSION)

    # cut_segments は keep_segments から導出される (§6.2.3)
    def test_cut_segments_are_derived(self):
        cuts = self._dump()["edit_points"]["cut_segments"]
        self.assertEqual(cuts[0], [12.48, 15.22])
        # 末尾 (48.9〜100.0) も取り除く区間として現れる
        self.assertEqual(cuts[-1], [48.9, 100.0])

    # 音声クリップに時刻フィールドが無い (§6.2.3)
    def test_audio_clip_has_no_time_fields(self):
        tracks = {t["id"]: t for t in self._dump()["tracks"]}
        for clip in tracks["A1"]["clips"]:
            self.assertEqual(set(clip), {"id", "link_clip", "gain_db", "muted"})

    # 時刻はミリ秒精度へ丸められる
    def test_times_are_rounded_to_milliseconds(self):
        self.timeline.clip_by_id("c1").timeline_start = 1.23456789
        clip = self._dump()["tracks"][0]["clips"][0]
        self.assertEqual(clip["timeline_start"], 1.235)


class MigrationTest(unittest.TestCase):

    # schema_version が無いファイルは開けない
    def test_missing_version_is_rejected(self):
        with self.assertRaises(TimelineError):
            project_io.from_json(json.dumps({"tracks": []}))

    # より新しい版は理由を明示して開かない (§6.2.4)
    def test_future_version_is_rejected_with_reason(self):
        payload = json.dumps({"schema_version": project_io.SCHEMA_VERSION + 1})
        with self.assertRaises(TimelineError) as ctx:
            project_io.from_json(payload)
        self.assertIn("開けない", str(ctx.exception))

    # 壊れた JSON は TimelineError になる
    def test_broken_json_is_rejected(self):
        with self.assertRaises(TimelineError):
            project_io.from_json("{ not json")


class ValidationTest(_TempMediaCase):

    # 短すぎるクリップは除外される
    def test_too_short_clip_is_dropped(self):
        data = json.loads(project_io.to_json(self.timeline))
        data["tracks"][0]["clips"][1]["duration"] = 0.001
        restored = project_io.from_dict(data, min_clip_sec=0.05)
        self.assertEqual([c.id for c in restored.base_clips()], ["c1"])

    # リンク先が無い音声クリップは捨てられる (映像は無音として残る)
    def test_orphan_audio_clip_is_dropped(self):
        data = json.loads(project_io.to_json(self.timeline))
        data["tracks"][1]["clips"][0]["link_clip"] = "no_such_clip"
        restored = project_io.from_dict(data)
        self.assertEqual([c.id for c in restored.base_audio_track().clips], ["a2"])
        # 映像側は残る
        self.assertEqual(len(restored.base_clips()), 2)

    # メディアが存在しない場合は該当クリップを無効化して継続する
    def test_missing_media_disables_clips(self):
        data = json.loads(project_io.to_json(self.timeline))
        data["media_pool"][0]["path"] = os.path.join(self._dir, "no_such_file.mp4")
        restored = project_io.from_dict(data)
        self.assertEqual(restored.base_clips(), [])
        self.assertEqual(len(restored.base_clips(include_disabled=True)), 2)

    # 端数のある尺を積み上げた Timeline を往復しても重複と誤判定されない
    # (時刻をミリ秒精度で丸めるため、開始と直前終端が最大 1ms ずれる)
    def test_rounding_does_not_trigger_overlap_fix(self):
        from src.timeline.model import Clip, TRACK_VIDEO, Timeline, Track
        clips = []
        cursor = 0.0
        for index, duration in enumerate([2.0, 3.0083, 2.9871, 1.9954, 1.5162]):
            clips.append(Clip(f"c{index}", "m1", cursor, duration,
                              cursor, cursor + duration))
            cursor += duration
        timeline = Timeline(
            fps=30, media_pool=self.timeline.media_pool,
            source={"media_id": "m1"},
            tracks=[Track("V1", TRACK_VIDEO, 1, is_base=True, clips=clips)])
        with self.assertNoLogs("src.timeline.project_io", level="WARNING"):
            restored = project_io.from_json(project_io.to_json(timeline))
        self.assertEqual(len(restored.base_clips()), 5)

    # 実際に重複しているクリップは後勝ちで詰められる
    def test_overlapping_clips_are_pushed(self):
        data = json.loads(project_io.to_json(self.timeline))
        data["tracks"][0]["clips"][1]["timeline_start"] = 5.0
        restored = project_io.from_dict(data)
        clips = restored.base_clips()
        self.assertAlmostEqual(clips[1].timeline_start, clips[0].timeline_end, places=3)

    # 編集点の逆転区間は無視される
    def test_invalid_keep_segment_is_ignored(self):
        data = json.loads(project_io.to_json(self.timeline))
        data["edit_points"]["keep_segments"] = [[0.0, 10.0], [20.0, 15.0]]
        restored = project_io.from_dict(data)
        self.assertEqual(restored.edit_points["keep_segments"], [[0.0, 10.0]])


class OutputPathTest(unittest.TestCase):

    # 出力先は timeline.project_dir → general.output_directory → 入力と同じ場所 の順
    def test_falls_back_to_input_directory(self):
        settings = {"timeline": {}, "general": {"output_directory": ""}}
        path = project_io.default_project_path(settings, "D:/videos/sample.mp4")
        self.assertEqual(os.path.basename(path), "sample.timeline.json")
        self.assertEqual(os.path.dirname(path), os.path.dirname(os.path.abspath("D:/videos/sample.mp4")))

    def test_uses_configured_directory(self):
        out_dir = tempfile.mkdtemp(prefix="timeline_out_")
        try:
            settings = {"timeline": {"project_dir": out_dir}, "general": {}}
            path = project_io.default_project_path(settings, "D:/videos/sample.mp4")
            self.assertEqual(os.path.dirname(path), out_dir)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
