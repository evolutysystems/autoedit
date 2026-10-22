# 元 VOD を手放したアーカイブプロジェクトを開けることの単体テスト
# (docs/error/fromUser/resolve.md §9-6)
# 実行: python -m unittest discover -s tests
#
# 背景 (2026-10-22 の実機確認):
#   §6-6 で archive_keep_media を既定にし、<プロジェクト>.media/ へ映像素材を
#   複製して「自己完結したら元 VOD をゴミ箱へ送る」運用にした。ところが
#   project_resume は素材を見る前に元 VOD を必須にしていたため、素材が全件
#   揃っているのに「素材が見つかりません」(= VOD の再リンク画面) で止まっていた。
#
# 重点:
#   ・.media/ に素材が揃っていれば、元 VOD 無しで開ける (再リンクを 1 度も尋ねない)
#   ・素材が無いプロジェクトは従来どおり VOD を尋ね、答えが無ければ InputError
#   ・尋ねた答えは source.input_path と archive.vod_path の両方へ書き戻る (回答 Q7)
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.archive import clip_writer, project_resume
from src.exceptions import InputError
from src.timeline import media_recovery, project_io
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
    {"index": 1, "vod_start": 100.0, "vod_end": 280.0},
    {"index": 2, "vod_start": 900.0, "vod_end": 1080.0},
]


def _touch(path, size=16):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"0" * size)
    return path


# build_archive_timeline と同じ形のアーカイブ用 Timeline を組む
# (tests/test_archive_project_resume.py と同じ組み立て。discover が tests を
#  パッケージとして読まないため、共有せずここに持つ)
def _archive_timeline(media_paths, vod_path):
    timeline = Timeline(fps=60, width=1920, height=1080)
    track = Track(BASE_VIDEO_TRACK_ID, TRACK_VIDEO, 1, name="Video 1", is_base=True)
    timeline.tracks = [track]
    cursor = 0.0
    clip_meta = []
    media_meta = []
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
            "vod_end": entry["vod_end"], "score": 0.0,
            "timeline_start": cursor, "timeline_end": cursor + 180.0,
            "media_id": media_id, "theme": "", "media_role": "normalized",
        })
        media_meta.append({
            "media_id": media_id, "vod_start": entry["vod_start"],
            "vod_end": entry["vod_end"], "media_role": "normalized",
        })
        cursor += 180.0
    timeline.source = {
        "input_path": vod_path, "duration_sec": cursor,
        "archive": {"vod_path": vod_path, "clips": clip_meta,
                    "media": media_meta, "curve": []},
    }
    timeline.normalize()
    return timeline


class ResumeWithoutVodTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="archive_no_vod_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.settings = dict(_SETTINGS)
        # 元 VOD は取得した時点では在る (保存の時点ではまだ消えていない)
        self.vod = _touch(os.path.join(self.tmp, "vod_1.mp4"))
        self.project = os.path.join(self.tmp, "vod_1.archive.timeline.json")
        self.media_dir = os.path.join(self.tmp, "vod_1.archive.media")
        # 再リンク画面が何回呼ばれたか (0 であることを確かめる)
        self.relink_calls = []

    def _relink(self, info):
        self.relink_calls.append(dict(info or {}))
        return None             # 「この素材を使わない」= 利用者は差し替えない

    # archive_keep_media で保存した状態を作る (.media/ に映像素材が在る)
    def _save_self_contained(self):
        media_paths = [
            _touch(os.path.join(self.media_dir, f"m{c['index']}_normalized.mp4"))
            for c in _CLIPS
        ]
        project_io.save(_archive_timeline(media_paths, self.vod), self.project,
                        project_path=self.project)
        return media_paths

    # keep_audio だけで保存した状態を作る (素材は実行の終わりに消える)
    def _save_without_media(self):
        work = os.path.join(self.tmp, "archive_clips_run1")
        media_paths = [
            _touch(os.path.join(work, f"clip{c['index']}", "normalized.mp4"))
            for c in _CLIPS
        ]
        project_io.save(_archive_timeline(media_paths, self.vod), self.project,
                        project_path=self.project)
        shutil.rmtree(work, ignore_errors=True)
        return media_paths

    # 復旧だけを通す (書き出しと編集画面は通らない)
    def _recover(self, timeline):
        workdir = tempfile.mkdtemp(prefix="archive_no_vod_work_", dir=self.tmp)
        resolver = project_resume.make_vod_resolver(timeline, self._relink)
        recover = project_resume.make_media_recover(
            timeline, self.settings, workdir, media_dir=self.media_dir,
            vod_resolver=resolver)
        return media_recovery.recover(
            timeline, self.project, self.settings,
            relink_callback=self._relink, source_recover=recover)

    # 本題: 素材が揃っていれば元 VOD が無くても開ける
    def test_opens_when_media_dir_has_every_source(self):
        media_paths = self._save_self_contained()
        os.remove(self.vod)            # vod_retention がゴミ箱へ送った状態

        timeline, _meta = project_io.load_project(
            self.project, validate_timeline=False)
        result = self._recover(timeline)
        project_io.validate(timeline)

        self.assertEqual(result["missing"], [])
        self.assertEqual(self.relink_calls, [],
                         "元 VOD も素材も尋ねてはいけません")
        clips = timeline.base_clips()
        self.assertEqual(len(clips), len(_CLIPS))
        self.assertTrue(all(c.enabled for c in clips), "V1 クリップが無効化されました")
        self.assertEqual([m.path for m in timeline.media_pool], media_paths)

    # 編集画面へ渡す prepared も素材を指したまま揃う
    def test_rebuild_prepared_without_vod(self):
        self._save_self_contained()
        os.remove(self.vod)

        timeline, _meta = project_io.load_project(
            self.project, validate_timeline=False)
        self._recover(timeline)

        prepared = project_resume.rebuild_prepared(timeline)
        self.assertEqual([p["index"] for p in prepared], [c["index"] for c in _CLIPS])
        for entry in prepared:
            self.assertTrue(os.path.exists(entry["normalized_path"]))

    # イントロカードの素の控えがあれば prepared へ載る (resolve.md §9-7)
    def test_prepared_carries_the_saved_intro_source(self):
        self._save_self_contained()
        os.remove(self.vod)
        intro = _touch(clip_writer.intro_source_path(self.media_dir, "m1"))

        timeline, _meta = project_io.load_project(
            self.project, validate_timeline=False)
        self._recover(timeline)
        prepared = project_resume.rebuild_prepared(timeline,
                                                   media_dir=self.media_dir)

        by_index = {p["index"]: p for p in prepared}
        self.assertEqual(by_index[1]["intro_source"], intro)
        # 控えが無い素材は空文字 (元 VOD から作る / 作れなければ省く)
        self.assertEqual(by_index[2]["intro_source"], "")

    # media_dir を渡さない呼び出し (従来の CLI・テスト経路) でも壊れない
    def test_prepared_without_media_dir(self):
        self._save_self_contained()
        os.remove(self.vod)

        timeline, _meta = project_io.load_project(
            self.project, validate_timeline=False)
        self._recover(timeline)

        prepared = project_resume.rebuild_prepared(timeline)
        self.assertTrue(all(p["intro_source"] == "" for p in prepared))

    # 素材が無いプロジェクトは従来どおり元 VOD を尋ねる
    def test_still_asks_for_vod_when_media_must_be_recut(self):
        self._save_without_media()
        os.remove(self.vod)

        timeline, _meta = project_io.load_project(
            self.project, validate_timeline=False)
        with self.assertRaises(InputError):
            self._recover(timeline)

        self.assertEqual([info["media_id"] for info in self.relink_calls], ["vod"])

    # 尋ねた答えは input_path と archive.vod_path の両方へ書き戻る (回答 Q7)
    def test_relinked_vod_is_written_back(self):
        self._save_without_media()
        os.remove(self.vod)
        replaced = _touch(os.path.join(self.tmp, "moved", "vod_1.mp4"))

        timeline, _meta = project_io.load_project(
            self.project, validate_timeline=False)
        resolver = project_resume.make_vod_resolver(
            timeline, lambda _info: replaced)

        self.assertEqual(resolver(), replaced)
        self.assertEqual(timeline.source["input_path"], replaced)
        self.assertEqual(timeline.source["archive"]["vod_path"], replaced)

    # 2 本目以降は尋ね直さない (記憶する)
    def test_asks_only_once(self):
        self._save_without_media()
        os.remove(self.vod)
        replaced = _touch(os.path.join(self.tmp, "moved", "vod_1.mp4"))
        asked = []

        def relink(info):
            asked.append(dict(info or {}))
            return replaced

        timeline, _meta = project_io.load_project(
            self.project, validate_timeline=False)
        resolver = project_resume.make_vod_resolver(timeline, relink)
        for _ in range(3):
            self.assertEqual(resolver(), replaced)

        self.assertEqual(len(asked), 1)


if __name__ == "__main__":
    unittest.main()
