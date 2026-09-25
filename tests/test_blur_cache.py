# 追従結果キャッシュ (src/blur/store.py) と付随ファイルの単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (ver5 resolve4 §2.8 / §5.12 + ver5 resolve8 §5.6 / §8.1 E):
#   ・**素材の一時パスが変わっても由来キーが変わらないこと** (回帰テスト。
#     これが崩れると、開き直すたびに追従をやり直すことになる)
#   ・元動画・切り出し範囲・解像度・尺が変われば由来キーが変わること
#   ・**区切りの指紋で「変わった所だけ」追い直すこと** (ver5 resolve8 §3.5)
#   ・素材ごとに妥当性を判定し、変わった素材の追従結果だけ捨てること
#   ・追従結果がプロジェクトの改名・削除に付いて回ること
import json
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.blur import store
from src.blur.config import config
from src.timeline import project_io
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


# 中間ファイル (毎回作り直される) を 1 つ作る
def _make_media_file(folder, name="normalized.mp4", body=b"x" * 128):
    path = os.path.join(folder, name)
    with open(path, "wb") as handle:
        handle.write(body)
    return path


def _build_timeline(media_path, source):
    media = [MediaRef("m1", "video", media_path, 100.0, 1920, 1080, 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 10.0, 0.0, 10.0),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
                  clips=[AudioClip("a1", "c1")])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1")
    return Timeline(fps=60, width=1920, height=1080, source=source,
                    media_pool=media, tracks=[video, audio, subtitle])


class MediaKeyStabilityTest(unittest.TestCase):
    """ver5 resolve4 §2.8 (a): 一時パスで由来キーが変わってはいけない"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="blur_cache_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = config({"blur": {"enabled": True}})
        # 元動画 (利用者のファイル。消えない)
        self.input_path = _make_media_file(self.tmp, "input.mp4", b"vod" * 100)

    # 実行のたびに別の一時フォルダへ作り直される中間ファイルで Timeline を作る
    def _clip_timeline(self, run):
        folder = os.path.join(self.tmp, run)
        os.makedirs(folder, exist_ok=True)
        path = _make_media_file(folder)
        return _build_timeline(path, {
            "media_id": "m1", "input_path": self.input_path,
            "media_path": path, "media_role": "normalized"})

    def _archive_timeline(self, run, vod_start=10.0, vod_end=20.0):
        folder = os.path.join(self.tmp, run)
        os.makedirs(folder, exist_ok=True)
        path = _make_media_file(folder, "clip1.mp4")
        return _build_timeline(path, {
            "media_id": "m1", "input_path": self.input_path,
            "archive": {"vod_path": self.input_path, "clips": [
                {"media_id": "m1", "vod_start": vod_start, "vod_end": vod_end,
                 "media_role": "normalized"}]}})

    def _key(self, timeline):
        return store.media_key(timeline, timeline.media_by_id("m1"))

    # クリップ用: 一時パスが変わっても由来キーは同じ
    def test_clip_key_survives_a_new_temp_path(self):
        self.assertEqual(self._key(self._clip_timeline("run1")),
                         self._key(self._clip_timeline("run2")))

    # アーカイブ用: 同上
    def test_archive_key_survives_a_new_temp_path(self):
        self.assertEqual(self._key(self._archive_timeline("run1")),
                         self._key(self._archive_timeline("run2")))

    # 素材のファイルが既に消えていても落ちないこと
    def test_key_works_when_the_media_file_is_already_gone(self):
        timeline = self._clip_timeline("run1")
        os.remove(timeline.media_by_id("m1").path)
        self.assertTrue(self._key(timeline))

    # 由来キーに一時パスが混ざっていないこと
    def test_media_key_does_not_contain_the_temp_path(self):
        timeline = self._clip_timeline("run1")
        self.assertNotIn(timeline.media_by_id("m1").path, self._key(timeline))

    # 元動画が変わったら由来キーも変わること
    def test_changing_the_source_video_changes_the_key(self):
        before = self._key(self._clip_timeline("run1"))
        with open(self.input_path, "wb") as handle:
            handle.write(b"different" * 80)
        self.assertNotEqual(before, self._key(self._clip_timeline("run2")))

    # 切り出し範囲が変わったら由来キーも変わること
    def test_changing_the_clip_range_changes_the_key(self):
        before = self._key(self._archive_timeline("run1", 10.0, 20.0))
        self.assertNotEqual(before, self._key(self._archive_timeline("run2", 12.0, 20.0)))

    # 解像度・尺が変わったら由来キーも変わること
    def test_changing_the_resolution_or_duration_changes_the_key(self):
        timeline = self._clip_timeline("run1")
        before = self._key(timeline)
        timeline.media_by_id("m1").width = 1280
        self.assertNotEqual(before, self._key(timeline))
        timeline.media_by_id("m1").width = 1920
        timeline.media_by_id("m1").duration_sec = 42.0
        self.assertNotEqual(before, self._key(timeline))

    # E2: 追従の設定・キャンバスの寸法が変われば settings_key も変わること
    def test_settings_key_follows_the_track_settings(self):
        timeline = self._clip_timeline("run1")
        before = store.settings_key(self.cfg, timeline)
        other = config({"blur": {"track": {"sample_fps": 2.0}}})
        self.assertNotEqual(before, store.settings_key(other, timeline))
        # 素材を変えても settings_key は変わらない (素材は media[].key で個別に見る)
        self.assertEqual(before, store.settings_key(config({"blur": {"enabled": True}}),
                                                    timeline))
        # 正規化座標はキャンバスの寸法に依るため、寸法が変われば作り直す
        timeline.width, timeline.height = 1080, 1920
        self.assertNotEqual(before, store.settings_key(self.cfg, timeline))


class MediaKeyTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="blur_key_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.input_path = _make_media_file(self.tmp, "input.mp4", b"vod" * 100)

    def test_key_leaves_out_the_temp_path(self):
        folder = os.path.join(self.tmp, "run1")
        os.makedirs(folder, exist_ok=True)
        path = _make_media_file(folder)
        timeline = _build_timeline(path, {
            "media_id": "m1", "input_path": self.input_path,
            "media_path": path, "media_role": "normalized"})
        key = store.media_key(timeline, timeline.media_by_id("m1"))
        self.assertNotIn("run1", key)
        self.assertIn("input=", key)

    def test_user_owned_media_still_uses_its_own_file(self):
        # OP/ED・画像など、元動画から作られたものでない素材は従来どおり
        other = _make_media_file(self.tmp, "opening.mp4", b"op" * 40)
        timeline = _build_timeline(other, {"input_path": self.input_path})
        key = store.media_key(timeline, timeline.media_by_id("m1"))
        self.assertIn("file=", key)


class SegmentCacheTest(unittest.TestCase):
    """区切りの指紋で「変わった所だけ」追い直すこと (ver5 resolve8 §3.5 / E1〜E6)"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="blur_reuse_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.input_path = _make_media_file(self.tmp, "input.mp4", b"vod" * 100)
        self.settings = {"blur": {"enabled": True}}
        self.cfg = config(self.settings)
        self.timeline = self._timeline("run1")

    def _timeline(self, run):
        folder = os.path.join(self.tmp, run)
        os.makedirs(folder, exist_ok=True)
        path = _make_media_file(folder)
        return _build_timeline(path, {
            "media_id": "m1", "input_path": self.input_path,
            "media_path": path, "media_role": "normalized"})

    def _segment(self, spec_id="b1", hash_="h1", start=0.0, end=2.0):
        return {"spec_id": spec_id, "media_id": "m1", "dir": "fwd",
                "start": start, "end": end, "hash": hash_,
                "start_rect": [0.3, 0.3, 0.1, 0.2], "end_rect": None}

    def _result(self, segment, samples=None):
        return {"hash": segment["hash"], "dir": segment["dir"], "start": segment["start"],
                "end": segment["end"], "status": "ok", "lost_sec": None,
                "samples": samples if samples is not None else
                [{"t": segment["start"], "cx": 0.35, "cy": 0.4, "score": 1.0}]}

    # E1: 書式版が違うキャッシュは読まないこと
    def test_schema_bump_invalidates_old_caches(self):
        cache = os.path.join(self.tmp, "sample.blur.json")
        stale = store.new_tracks(self.timeline, self.cfg, ["m1"])
        stale["schema"] = store.SCHEMA_VERSION - 1
        store.save(cache, stale)
        self.assertIsNone(store.load(cache, stale["settings_key"]))

    # E2: 設定が変われば古いキャッシュは読まれないこと
    def test_settings_change_invalidates_the_cache(self):
        cache = os.path.join(self.tmp, "sample.blur.json")
        store.save(cache, store.new_tracks(self.timeline, self.cfg, ["m1"]))
        other = config({"blur": {"track": {"sample_fps": 2.0}}})
        self.assertIsNone(store.load(cache, store.settings_key(other, self.timeline)))
        self.assertIsNotNone(store.load(cache, store.settings_key(self.cfg, self.timeline)))

    # E3: 指紋の無い区切りだけが「追い直す対象」になること
    def test_missing_segments(self):
        tracks = store.new_tracks(self.timeline, self.cfg, ["m1"])
        first = self._segment(hash_="h1")
        second = self._segment(hash_="h2", start=2.0, end=4.0)
        self.assertEqual(len(store.missing_segments(tracks, [first, second])), 2)
        store.put_segment(tracks, first, self._result(first))
        self.assertEqual([s["hash"] for s in store.missing_segments(tracks, [first, second])],
                         ["h2"])

    # E4: 入れた区切りを指紋で引けること (同じ指紋なら置き換わる)
    def test_put_and_get_segment(self):
        tracks = store.new_tracks(self.timeline, self.cfg, ["m1"])
        segment = self._segment()
        store.put_segment(tracks, segment, self._result(segment))
        store.put_segment(tracks, segment, self._result(segment, samples=[]))
        found = store.segment_of(tracks, "b1", "h1")
        self.assertIsNotNone(found)
        self.assertEqual(found["samples"], [])
        self.assertEqual(len(tracks["tracks"]["b1"]["segments"]), 1)
        self.assertIsNone(store.segment_of(tracks, "b1", "other"))

    # E5: 使わなくなった追従結果を捨てること (指定を消した / 指紋が変わった)
    def test_drop_unused(self):
        tracks = store.new_tracks(self.timeline, self.cfg, ["m1"])
        old = self._segment(hash_="h1")
        store.put_segment(tracks, old, self._result(old))
        decisions = {"specs": [{"id": "b1"}]}
        # 指紋が変わった = 古い区切りは捨てる
        store.drop_unused(tracks, decisions, [self._segment(hash_="h2")])
        self.assertEqual(tracks["tracks"]["b1"]["segments"], [])
        # 指定そのものが無くなった = 項目ごと捨てる
        store.put_segment(tracks, old, self._result(old))
        store.drop_unused(tracks, {"specs": []}, [])
        self.assertEqual(tracks["tracks"], {})

    # E6: 由来の変わった素材の追従結果だけ捨てること (他の素材は残す)
    def test_stale_media_is_dropped_alone(self):
        tracks = store.new_tracks(self.timeline, self.cfg, ["m1"])
        first = self._segment(spec_id="b1")
        store.put_segment(tracks, first, self._result(first))
        other = dict(self._segment(spec_id="b2"), media_id="m9")
        store.put_segment(tracks, other, self._result(other))
        tracks["media"]["m9"] = {"key": "other", "width": 1, "height": 1, "duration": 1.0}

        dropped = store.drop_stale_media(tracks, self.timeline)
        self.assertEqual(dropped, ["m9"])
        self.assertEqual(list(tracks["tracks"].keys()), ["b1"])
        self.assertIn("m1", tracks["media"])
        self.assertNotIn("m9", tracks["media"])

    # 素材の控えを後から足せること (指定が増えたとき)
    def test_merge_media(self):
        tracks = store.new_tracks(self.timeline, self.cfg, [])
        self.assertEqual(tracks["media"], {})
        store.merge_media(tracks, self.timeline, ["m1"])
        self.assertIn("m1", tracks["media"])


class MismatchReasonTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="blur_reason_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = config({"blur": {"enabled": True}})
        self.input_path = _make_media_file(self.tmp, "input.mp4", b"vod" * 100)
        self.timeline = _build_timeline(
            _make_media_file(self.tmp, "normalized.mp4"),
            {"media_id": "m1", "input_path": self.input_path,
             "media_path": "x", "media_role": "normalized"})
        self.tracks = store.new_tracks(self.timeline, self.cfg, ["m1"])

    def test_no_change_points_at_the_settings(self):
        reason = store.describe_mismatch(self.tracks, self.timeline, ["m1"])
        self.assertIn("設定", reason)

    def test_duration_change_is_named(self):
        self.timeline.media_by_id("m1").duration_sec = 42.0
        reason = store.describe_mismatch(self.tracks, self.timeline, ["m1"])
        self.assertIn("尺が変わりました", reason)
        self.assertIn("m1", reason)

    def test_resolution_change_is_named(self):
        self.timeline.media_by_id("m1").width = 1280
        reason = store.describe_mismatch(self.tracks, self.timeline, ["m1"])
        self.assertIn("解像度が変わりました", reason)

    def test_source_video_change_is_named(self):
        with open(self.input_path, "wb") as handle:
            handle.write(b"different" * 80)
        reason = store.describe_mismatch(self.tracks, self.timeline, ["m1"])
        self.assertIn("元動画か切り出し範囲が変わりました", reason)

    def test_old_cache_without_media_snapshot(self):
        reason = store.describe_mismatch({"schema": 2}, self.timeline, ["m1"])
        self.assertIn("素材の控え", reason)


class RelatedFilesTest(unittest.TestCase):
    """追従結果がプロジェクトに付いて回ること (§5.12.3 / E8)"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="blur_related_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = {"project_suffix": ".timeline.json", "project": {}}
        self.project = os.path.join(self.tmp, "sample.timeline.json")
        with open(self.project, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 1, "timeline": {}, "tracks": [],
                       "media_pool": []}, handle)
        self.cache = project_io.blur_cache_path(self.project)
        with open(self.cache, "w", encoding="utf-8") as handle:
            handle.write("{}")

    def test_cache_path_matches_the_store_convention(self):
        self.assertEqual(os.path.basename(self.cache), "sample.timeline.blur.json")
        self.assertEqual(project_io.blur_cache_path(self.project),
                         store.cache_path_for(self.project, self.tmp))

    def test_related_paths_reports_the_cache(self):
        related = project_io.related_paths(self.project, self.cfg)
        self.assertEqual(os.path.normcase(related["blur_cache"]),
                         os.path.normcase(self.cache))

    def test_related_paths_is_empty_when_there_is_no_cache(self):
        os.remove(self.cache)
        self.assertEqual(project_io.related_paths(self.project, self.cfg)["blur_cache"], "")

    def test_rename_moves_the_cache(self):
        new_path = project_io.rename_project(self.project, "renamed", timeline_cfg=self.cfg)
        self.assertTrue(os.path.isfile(new_path))
        self.assertFalse(os.path.exists(self.cache),
                         "改名で追従結果が取り残されています")
        self.assertTrue(os.path.isfile(project_io.blur_cache_path(new_path)))

    def test_delete_removes_the_cache(self):
        result = project_io.delete_project(self.project, timeline_cfg=self.cfg,
                                           use_trash=False)
        self.assertEqual(result["failed"], [])
        self.assertFalse(os.path.exists(self.cache),
                         "削除で追従結果がゴミとして残っています")


if __name__ == "__main__":
    unittest.main()
