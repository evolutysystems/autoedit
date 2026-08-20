# プロジェクト一覧の走査・概要・リネーム・削除 (src/timeline/project_io.py) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver3/resolve9.md §8.1):
#   ・走査が履歴 + フォルダの和集合を重複なく返し、自動保存を除くこと (§3-8)
#   ・概要が種別・クリップ数・尺を返し、素材が無いとき元動画へ読み替えること (§5.13)
#   ・リネームが本体・自動保存・素材フォルダ・JSON 内のパスを揃えて動かすこと (§5.14)
#   ・不正名・同名衝突で TimelineError を投げること
#   ・削除が本体・自動保存・素材フォルダを消すこと (§5.10)
#   ・壊れた JSON でも read_summary が例外を投げないこと
import json
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.exceptions import TimelineError
from src.timeline import project_io
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
    "timeline": {
        "project_suffix": ".timeline.json",
        "project": {
            "autosave_suffix": ".autosave.json",
            "media_dir_suffix": ".media",
            "archive_suffix": ".archive",
            "recent": [],
            "recent_archive": [],
            "library": {"max_items": 100, "extra_dirs": [], "scan_recursive": False},
        },
    },
}


def _settings(tmp_dir, **project_overrides):
    settings = json.loads(json.dumps(_SETTINGS))
    settings["general"]["output_directory"] = tmp_dir
    settings["timeline"]["project"].update(project_overrides)
    return settings


# 本編素材 1 本ぶんのクリップ用 Timeline を組む
def _clip_timeline(media_path, input_path, media_id="m1"):
    timeline = Timeline(fps=60, width=1920, height=1080)
    track = Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, name="Video 1", is_base=True)
    timeline.tracks = [track]
    timeline.media_pool.append(MediaRef(
        media_id, MEDIA_VIDEO, media_path, duration_sec=60.0,
        width=1920, height=1080, fps=60.0, has_audio=True))
    track.clips.append(Clip("c1", media_id, 0.0, 20.0, source_in=5.0, source_out=25.0,
                            origin={"type": ORIGIN_SILENCE_CUT}))
    timeline.source = {"input_path": input_path, "media_path": media_path,
                       "media_id": media_id, "duration_sec": 20.0}
    timeline.normalize()
    return timeline


# アーカイブ用 Timeline (source.archive を持つ)
def _archive_timeline(media_path, vod_path):
    timeline = Timeline(fps=60, width=1920, height=1080)
    track = Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, name="Video 1", is_base=True)
    timeline.tracks = [track]
    timeline.media_pool.append(MediaRef(
        "m1", MEDIA_VIDEO, media_path, duration_sec=180.0,
        width=1920, height=1080, fps=60.0, has_audio=True))
    track.clips.append(Clip(
        "c1", "m1", 0.0, 30.0, source_in=10.0, source_out=40.0,
        origin={"type": ORIGIN_SILENCE_CUT, "archive_clip_index": 1}))
    timeline.source = {
        "input_path": vod_path, "duration_sec": 30.0,
        "archive": {"vod_path": vod_path, "curve": [],
                    "clips": [{"index": 1, "vod_start": 1200.0, "vod_end": 1380.0,
                               "score": 70.0, "media_id": "m1", "theme": "",
                               "media_role": "normalized"}]},
    }
    timeline.normalize()
    return timeline


def _touch(path, size=16):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"0" * size)
    return path


class ScanProjectsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="library_scan_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_union_of_recent_and_folder_without_duplicates(self):
        inside = _touch(os.path.join(self.tmp, "a.timeline.json"))
        outside_dir = tempfile.mkdtemp(prefix="library_other_")
        self.addCleanup(shutil.rmtree, outside_dir, ignore_errors=True)
        outside = _touch(os.path.join(outside_dir, "b.timeline.json"))
        # 履歴に「フォルダ内の 1 件」と「フォルダ外の 1 件」を積む
        settings = _settings(self.tmp, recent=[inside, outside])

        result = project_io.scan_projects(settings)
        paths = [os.path.normcase(p) for p in result["paths"]]
        self.assertIn(os.path.normcase(inside), paths)
        self.assertIn(os.path.normcase(outside), paths)
        self.assertEqual(len(paths), len(set(paths)))    # 重複なし

    def test_autosave_is_excluded(self):
        _touch(os.path.join(self.tmp, "a.timeline.json"))
        _touch(os.path.join(self.tmp, "a.timeline.autosave.json"))
        result = project_io.scan_projects(_settings(self.tmp))
        names = [os.path.basename(p) for p in result["paths"]]
        self.assertEqual(names, ["a.timeline.json"])

    def test_kind_filter_uses_file_name(self):
        _touch(os.path.join(self.tmp, "clip.timeline.json"))
        _touch(os.path.join(self.tmp, "vod.archive.timeline.json"))
        settings = _settings(self.tmp)
        clips = project_io.scan_projects(settings, kind=project_io.KIND_CLIP)
        archives = project_io.scan_projects(settings, kind=project_io.KIND_ARCHIVE)
        self.assertEqual([os.path.basename(p) for p in clips["paths"]],
                         ["clip.timeline.json"])
        self.assertEqual([os.path.basename(p) for p in archives["paths"]],
                         ["vod.archive.timeline.json"])

    def test_max_items_reports_truncated_count(self):
        for index in range(5):
            _touch(os.path.join(self.tmp, f"p{index}.timeline.json"))
        settings = _settings(self.tmp)
        settings["timeline"]["project"]["library"]["max_items"] = 2
        result = project_io.scan_projects(settings)
        self.assertEqual(len(result["paths"]), 2)
        self.assertEqual(result["truncated"], 3)


class ReadSummaryTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="library_summary_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.settings = _settings(self.tmp)

    def test_clip_project_summary(self):
        media = _touch(os.path.join(self.tmp, "normalized.mp4"))
        source = _touch(os.path.join(self.tmp, "input.mp4"))
        path = os.path.join(self.tmp, "sample.timeline.json")
        project_io.save(_clip_timeline(media, source), path, project_path=path)

        summary = project_io.read_summary(path, self.settings)
        self.assertFalse(summary["broken"])
        self.assertEqual(summary["kind"], project_io.KIND_CLIP)
        self.assertEqual(summary["clip_count"], 1)
        self.assertAlmostEqual(summary["duration_sec"], 20.0, places=3)
        self.assertEqual(len(summary["frames"]), 3)
        # 素材が在るのでそちらを見る
        self.assertEqual(os.path.normcase(summary["frames"][0]["path"]),
                         os.path.normcase(media))

    def test_falls_back_to_source_video_when_media_is_gone(self):
        media = _touch(os.path.join(self.tmp, "normalized.mp4"))
        source = _touch(os.path.join(self.tmp, "input.mp4"))
        path = os.path.join(self.tmp, "sample.timeline.json")
        project_io.save(_clip_timeline(media, source), path, project_path=path)
        os.remove(media)      # 中間ファイルは実行の終わりに消える

        summary = project_io.read_summary(path, self.settings)
        self.assertTrue(summary["frames"])
        for frame in summary["frames"]:
            self.assertEqual(os.path.normcase(frame["path"]),
                             os.path.normcase(source))
            # 正規化は映像を -c:v copy で通すため素材内の時刻をそのまま使える
            self.assertGreaterEqual(frame["sec"], 5.0)

    def test_archive_project_frames_offset_by_vod_start(self):
        media = _touch(os.path.join(self.tmp, "clip1.mp4"))
        vod = _touch(os.path.join(self.tmp, "vod.mp4"))
        path = os.path.join(self.tmp, "vod.archive.timeline.json")
        project_io.save(_archive_timeline(media, vod), path, project_path=path)
        os.remove(media)

        summary = project_io.read_summary(path, self.settings)
        self.assertEqual(summary["kind"], project_io.KIND_ARCHIVE)
        self.assertTrue(summary["frames"])
        for frame in summary["frames"]:
            self.assertEqual(os.path.normcase(frame["path"]),
                             os.path.normcase(vod))
            # VOD 時刻 = クリップ内時刻 + vod_start
            self.assertGreaterEqual(frame["sec"], 1200.0 + 10.0)

    def test_broken_project_does_not_raise(self):
        path = os.path.join(self.tmp, "broken.timeline.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        summary = project_io.read_summary(path, self.settings)
        self.assertTrue(summary["broken"])
        self.assertEqual(summary["frames"], [])


class RenameProjectTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="library_rename_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.settings = _settings(self.tmp)
        self.cfg = self.settings["timeline"]
        self.source = _touch(os.path.join(self.tmp, "input.mp4"))
        self.path = os.path.join(self.tmp, "old.timeline.json")
        # keep_media で複製された素材を模す (<プロジェクト名>.media/ の中)
        self.media_dir = os.path.join(self.tmp, "old.media")
        self.media = _touch(os.path.join(self.media_dir, "m1_normalized.mp4"))
        project_io.save(_clip_timeline(self.media, self.source), self.path,
                        project_path=self.path)
        self.autosave = _touch(os.path.join(self.tmp, "old.timeline.autosave.json"))

    def test_renames_project_autosave_and_media_dir(self):
        new_path = project_io.rename_project(self.path, "new", self.settings,
                                             timeline_cfg=self.cfg)
        self.assertTrue(os.path.exists(new_path))
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "new.media")))
        self.assertFalse(os.path.isdir(self.media_dir))
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "new.timeline.autosave.json")))
        self.assertFalse(os.path.exists(self.autosave))

    def test_media_paths_are_rewritten_so_project_still_opens(self):
        new_path = project_io.rename_project(self.path, "new", self.settings,
                                             timeline_cfg=self.cfg)
        timeline = project_io.load(new_path, validate_timeline=False)
        media = timeline.media_pool[0]
        self.assertTrue(os.path.exists(media.path),
                        f"素材のリンクが切れています: {media.path}")
        self.assertIn("new.media", media.path)
        # 検証を通しても無効化されない = 開ける状態
        project_io.validate(timeline)
        self.assertTrue(timeline.base_clips()[0].enabled)

    def test_invalid_name_raises(self):
        for name in ("", "  ", "a/b", "a:b", "a*b"):
            with self.assertRaises(TimelineError):
                project_io.rename_project(self.path, name, self.settings,
                                          timeline_cfg=self.cfg)
        self.assertTrue(os.path.exists(self.path))      # 何も動いていない

    def test_existing_name_raises_and_keeps_everything(self):
        _touch(os.path.join(self.tmp, "taken.timeline.json"))
        with self.assertRaises(TimelineError):
            project_io.rename_project(self.path, "taken", self.settings,
                                      timeline_cfg=self.cfg)
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(os.path.isdir(self.media_dir))

    def test_rolls_back_when_media_dir_cannot_move(self):
        # 移動先の素材フォルダが既にある = 途中で失敗する条件
        os.makedirs(os.path.join(self.tmp, "new.media"), exist_ok=True)
        with self.assertRaises(TimelineError):
            project_io.rename_project(self.path, "new", self.settings,
                                      timeline_cfg=self.cfg)
        # 本体と自動保存が元の名前へ戻っている
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(os.path.exists(self.autosave))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "new.timeline.json")))


class DeleteProjectTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="library_delete_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.settings = _settings(self.tmp)
        self.cfg = self.settings["timeline"]
        self.path = _touch(os.path.join(self.tmp, "p.timeline.json"))
        self.autosave = _touch(os.path.join(self.tmp, "p.timeline.autosave.json"))
        self.media_dir = os.path.join(self.tmp, "p.media")
        _touch(os.path.join(self.media_dir, "m1_audio.m4a"))

    def test_deletes_project_autosave_and_media_dir(self):
        result = project_io.delete_project(self.path, timeline_cfg=self.cfg,
                                           use_trash=False)
        self.assertEqual(result["failed"], [])
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.autosave))
        self.assertFalse(os.path.isdir(self.media_dir))

    def test_keeps_media_dir_when_disabled(self):
        project_io.delete_project(self.path, timeline_cfg=self.cfg,
                                  delete_media_dir=False, use_trash=False)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(os.path.isdir(self.media_dir))

    def test_related_paths_lists_only_existing(self):
        os.remove(self.autosave)
        related = project_io.related_paths(self.path, self.cfg)
        self.assertTrue(related["project"])
        self.assertEqual(related["autosave"], "")
        self.assertTrue(related["media_dir"])


class ForgetRecentTest(unittest.TestCase):

    def test_removes_and_replaces_entries(self):
        settings = {"timeline": {"project": {
            "recent": [r"D:\a.timeline.json", r"D:\b.timeline.json"],
            "recent_archive": [r"D:\c.archive.timeline.json"]}}}
        self.assertTrue(project_io.forget_recent(settings, r"D:\a.timeline.json"))
        self.assertEqual(settings["timeline"]["project"]["recent"],
                         [r"D:\b.timeline.json"])
        # 改名は差し替えになる
        self.assertTrue(project_io.forget_recent(
            settings, r"D:\c.archive.timeline.json",
            new_path=r"D:\d.archive.timeline.json"))
        self.assertEqual(
            [os.path.basename(p)
             for p in settings["timeline"]["project"]["recent_archive"]],
            ["d.archive.timeline.json"])


class ProjectKindTest(unittest.TestCase):

    def test_kind_from_timeline_and_dict(self):
        clip = _clip_timeline("m.mp4", "in.mp4")
        archive = _archive_timeline("c.mp4", "vod.mp4")
        self.assertEqual(project_io.project_kind(clip), project_io.KIND_CLIP)
        self.assertEqual(project_io.project_kind(archive), project_io.KIND_ARCHIVE)
        data = json.loads(project_io.to_json(archive))
        self.assertEqual(project_io.project_kind(data), project_io.KIND_ARCHIVE)

    def test_default_project_path_with_name_suffix(self):
        settings = {"general": {"output_directory": ""},
                    "timeline": {"project_suffix": ".timeline.json"}}
        path = project_io.default_project_path(
            settings, os.path.join(os.sep, "tmp", "vod.mp4"), name_suffix=".archive")
        self.assertEqual(os.path.basename(path), "vod.archive.timeline.json")


if __name__ == "__main__":
    unittest.main()
