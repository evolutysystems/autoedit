# アーカイブ用プロジェクトの保存と再編集 (src/archive/project_resume.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver3/resolve9.md §8.1):
#   ・保存 → 素材削除 → 復旧 → 検証で V1 が全本生存すること (§5.5)
#   ・復旧が音声サイドカー経路を通り、ラウドネス正規化を 1 度も呼ばないこと (§3-1 案D)
#   ・サイドカーを消すと再正規化経路へ落ち、それでも V1 が生存すること
#   ・rebuild_prepared が index/start/end/score/normalized_path を揃えること
#   ・アーカイブ用でないプロジェクトを渡すと InputError
import os
import shutil
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import project_resume
from src.exceptions import InputError
from src.timeline import media_sidecar, project_io
from src.timeline.model import (
    BASE_VIDEO_TRACK_ID,
    MEDIA_VIDEO,
    ORIGIN_SILENCE_CUT,
    TRACK_VIDEO,
    Clip,
    MediaRef,
    Timeline,
    Track,
)

_SETTINGS = {
    "general": {"output_directory": ""},
    "ffmpeg": {"audio_codec": "aac", "audio_sample_rate": 48000},
    "archive": {"clip_pipeline": {}},
    "timeline": {
        "project_suffix": ".timeline.json",
        "project": {"media_dir_suffix": ".media", "autosave_suffix": ".autosave.json",
                    "sidecar_tolerance_sec": 0.5, "missing_media_policy": "renormalize"},
    },
}

_CLIPS = [
    {"index": 1, "vod_start": 100.0, "vod_end": 280.0, "score": 72.5},
    {"index": 2, "vod_start": 900.0, "vod_end": 1080.0, "score": 61.0},
]


def _touch(path, size=16):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"0" * size)
    return path


# build_archive_timeline と同じ形のアーカイブ用 Timeline を組む
def _archive_timeline(media_paths, vod_path):
    timeline = Timeline(fps=60, width=1920, height=1080)
    track = Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, name="Video 1", is_base=True)
    timeline.tracks = [track]
    cursor = 0.0
    clip_meta = []
    for entry, path in zip(_CLIPS, media_paths):
        media_id = f"m{entry['index']}"
        timeline.media_pool.append(MediaRef(
            media_id, MEDIA_VIDEO, path, duration_sec=180.0,
            width=1920, height=1080, fps=60.0, has_audio=True))
        track.clips.append(Clip(
            f"c{entry['index']}", media_id, cursor, 180.0,
            source_in=0.0, source_out=180.0,
            origin={"type": ORIGIN_SILENCE_CUT,
                    "archive_clip_index": entry["index"], "segment_index": 0}))
        clip_meta.append({
            "index": entry["index"], "vod_start": entry["vod_start"],
            "vod_end": entry["vod_end"], "score": entry["score"],
            "timeline_start": cursor, "timeline_end": cursor + 180.0,
            "media_id": media_id, "theme": "", "media_role": "normalized",
        })
        cursor += 180.0
    timeline.source = {
        "input_path": vod_path, "duration_sec": cursor,
        "archive": {"vod_path": vod_path, "clips": clip_meta, "curve": []},
    }
    timeline.normalize()
    return timeline


class ArchiveRecoveryTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="archive_resume_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.settings = dict(_SETTINGS)
        self.vod = _touch(os.path.join(self.tmp, "vod.mp4"))
        # 実行中の中間ファイル (一時領域。実行の終わりに消える)
        self.work = os.path.join(self.tmp, "archive_clips_run1")
        self.media_paths = [
            _touch(os.path.join(self.work, f"clip{c['index']}", "normalized.mp4"))
            for c in _CLIPS
        ]
        self.project = os.path.join(self.tmp, "vod.archive.timeline.json")
        project_io.save(_archive_timeline(self.media_paths, self.vod), self.project,
                        project_path=self.project)
        # 保存時に音声サイドカーが作られた状態を模す
        self.media_dir = os.path.join(self.tmp, "vod.archive.media")
        self.sidecars = [
            _touch(os.path.join(self.media_dir, f"m{c['index']}_audio.m4a"))
            for c in _CLIPS
        ]
        # 実行が終わり中間ファイルが消える
        shutil.rmtree(self.work, ignore_errors=True)

    # cut_region / mux / normalize_file をダミーファイル生成へ差し替える
    def _patched(self, sidecar_ok=True):
        def fake_cut(input_path, start, end, dest, ffmpeg_cfg):
            return _touch(dest)

        def fake_mux(video_path, audio_path, dest_path, ffmpeg_cfg):
            if not sidecar_ok:
                return None
            return _touch(dest_path)

        def fake_normalize(src, dest, settings, on_progress=None):
            return _touch(dest)

        return (
            mock.patch("src.archive.clip_writer.cut_region", side_effect=fake_cut),
            mock.patch.object(media_sidecar, "mux", side_effect=fake_mux),
            mock.patch.object(media_sidecar, "matches_duration", return_value=True),
            mock.patch("src.modules.loudness_normalizer.normalize_file",
                       side_effect=fake_normalize),
        )

    def _recover(self, media_dir=None, sidecar_ok=True):
        timeline, _meta = project_io.load_project(self.project, validate_timeline=False)
        workdir = tempfile.mkdtemp(prefix="archive_resume_work_", dir=self.tmp)
        stats = {}
        patches = self._patched(sidecar_ok=sidecar_ok)
        started = [p.start() for p in patches]
        try:
            recover = project_resume.make_media_recover(
                timeline, self.settings, workdir,
                media_dir=self.media_dir if media_dir is None else media_dir,
                stats=stats)
            from src.timeline import media_recovery
            media_recovery.recover(timeline, self.project, self.settings,
                                   source_recover=recover)
            project_io.validate(timeline)
            normalize_mock = started[3]
        finally:
            for patch in patches:
                patch.stop()
        return timeline, stats, normalize_mock

    def test_all_clips_survive_via_sidecar(self):
        timeline, stats, normalize_mock = self._recover()
        clips = timeline.base_clips()
        self.assertEqual(len(clips), 2)
        self.assertTrue(all(c.enabled for c in clips), "V1 クリップが無効化されました")
        self.assertEqual(stats["from_sidecar"], 2)
        self.assertEqual(stats["renormalized"], 0)
        # 案D の要: 正規化は 1 度も走らない
        normalize_mock.assert_not_called()

    def test_falls_back_to_renormalize_without_sidecar(self):
        for path in self.sidecars:
            os.remove(path)
        timeline, stats, normalize_mock = self._recover()
        clips = timeline.base_clips()
        self.assertEqual(len(clips), 2)
        self.assertTrue(all(c.enabled for c in clips))
        self.assertEqual(stats["from_sidecar"], 0)
        self.assertEqual(stats["renormalized"], 2)
        self.assertEqual(normalize_mock.call_count, 2)

    def test_falls_back_when_mux_fails(self):
        timeline, stats, normalize_mock = self._recover(sidecar_ok=False)
        self.assertTrue(all(c.enabled for c in timeline.base_clips()))
        self.assertEqual(stats["from_sidecar"], 0)
        self.assertEqual(stats["renormalized"], 2)
        self.assertEqual(normalize_mock.call_count, 2)

    def test_use_source_policy_skips_normalize(self):
        for path in self.sidecars:
            os.remove(path)
        settings = dict(self.settings)
        settings["timeline"] = {
            "project_suffix": ".timeline.json",
            "project": {"media_dir_suffix": ".media",
                        "autosave_suffix": ".autosave.json",
                        "sidecar_tolerance_sec": 0.5,
                        "missing_media_policy": "use_source"},
        }
        self.settings = settings
        timeline, stats, normalize_mock = self._recover()
        self.assertTrue(all(c.enabled for c in timeline.base_clips()))
        self.assertEqual(stats["raw"], 2)
        normalize_mock.assert_not_called()

    def test_rebuild_prepared(self):
        timeline, _stats, _mock = self._recover()
        prepared = project_resume.rebuild_prepared(timeline)
        self.assertEqual([p["index"] for p in prepared], [1, 2])
        self.assertAlmostEqual(prepared[0]["start"], 100.0)
        self.assertAlmostEqual(prepared[0]["end"], 280.0)
        self.assertAlmostEqual(prepared[0]["score"], 72.5)
        for entry in prepared:
            self.assertTrue(os.path.exists(entry["normalized_path"]))
            self.assertAlmostEqual(entry["normalized_duration"], 180.0)

    def test_clip_project_is_rejected(self):
        timeline = Timeline(fps=60, width=1920, height=1080)
        timeline.tracks = [Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1,
                                 name="Video 1", is_base=True)]
        timeline.source = {"input_path": self.vod}
        path = os.path.join(self.tmp, "clip.timeline.json")
        project_io.save(timeline, path, project_path=path)
        with self.assertRaises(InputError):
            project_resume.run_from_archive_project(path, self.settings)

    def test_missing_project_raises(self):
        with self.assertRaises(InputError):
            project_resume.run_from_archive_project(
                os.path.join(self.tmp, "no_such.timeline.json"), self.settings)


class SidecarSuffixTest(unittest.TestCase):

    def test_suffixes_follow_audio_codec(self):
        self.assertEqual(media_sidecar.suffixes_for({"audio_codec": "aac"}),
                         (".m4a", ".mp4"))
        # MP4 に入らないコーデックは Matroska へ倒す
        self.assertEqual(media_sidecar.suffixes_for({"audio_codec": "libopus"}),
                         (".mka", ".mkv"))
        self.assertEqual(media_sidecar.suffixes_for({}), (".m4a", ".mp4"))

    def test_sidecar_path_uses_media_id(self):
        path = media_sidecar.sidecar_path(os.path.join("D:", "p.media"), "m3",
                                          {"audio_codec": "aac"})
        self.assertEqual(os.path.basename(path), "m3_audio.m4a")

    def test_duration_mismatch_is_rejected(self):
        with mock.patch("src.modules.ffmpeg_runner.probe_duration", return_value=100.0):
            self.assertTrue(media_sidecar.matches_duration("a.m4a", 100.2, {}, 0.5))
            self.assertFalse(media_sidecar.matches_duration("a.m4a", 103.0, {}, 0.5))
            # 期待値が無い (旧データ) 場合は通す
            self.assertTrue(media_sidecar.matches_duration("a.m4a", 0.0, {}, 0.5))


if __name__ == "__main__":
    unittest.main()
