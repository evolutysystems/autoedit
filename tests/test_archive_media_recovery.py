# セクション追加した素材の復元 (src/archive/timeline_builder.py の archive.media) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve6.md §3.1 / §3.2 / §5.2〜§5.5 / §8.1):
#   ・**統合しても、畳まれた側の素材の復元情報が残ること** (B1・B2 の回帰テスト)
#   ・素材単位の控えが無い旧プロジェクトでも、セクションの控えから引けること
#   ・既に壊れているプロジェクトを、穴と尺の突き合わせで自動修復できること
#   ・どのクリップからも使われていない素材は、復旧を尋ねず・保存もしないこと
# ffmpeg・音声認識は呼ばない。media_probe.probe だけ差し替えて素材を模す。
import json
import os
import shutil
import tempfile
import unittest

from src.archive import timeline_builder as archive_timeline
from src.timeline import commands, media_probe, media_recovery, project_io
from src.timeline.model import (
    BASE_Z_ORDER,
    ORIGIN_SILENCE_CUT,
    Clip,
    MediaRef,
)

_DURATIONS = {}


def _fake_probe(path, media_id, settings=None, cfg=None):
    return MediaRef(media_id, "video", path, _DURATIONS.get(path, 60.0),
                    1920, 1080, 60.0, True)


def _entry(index, start, end):
    path = f"section{index}.mp4"
    _DURATIONS[path] = end - start
    return {
        "index": index, "start": float(start), "end": float(end), "score": 10.0,
        "prepared_path": "", "profile": None, "eff_cfg": {},
        "items": [], "keep_segments": None,
        "normalized_path": path, "normalized_duration": float(end - start),
        "media_role": "normalized",
    }


class ArchiveMediaTestBase(unittest.TestCase):

    def setUp(self):
        self._original_probe = media_probe.probe
        media_probe.probe = _fake_probe
        self.settings = {"ffmpeg": {}}
        self.stack = commands.CommandStack()

    def tearDown(self):
        media_probe.probe = self._original_probe

    def _timeline(self, spans):
        prepared = [_entry(i, s, e) for i, (s, e) in enumerate(spans, 1)]
        return archive_timeline.build_archive_timeline(
            prepared, self.settings, source_path="vod.mp4")

    def _add(self, timeline, start, end):
        plan = archive_timeline.plan_section_add(timeline, start, end)
        if plan is None:
            return None
        base = archive_timeline.next_section_index(timeline)
        entries = [_entry(base + i, s, e) for i, (s, e) in enumerate(plan["ranges"])]
        self.stack.push(timeline, archive_timeline.AddArchiveSection(
            entries, self.settings, plan))
        return plan


class MediaRecordTest(ArchiveMediaTestBase):
    """§5.2: 素材 1 本ごとの復元情報を持つ"""

    def test_build_records_every_media(self):
        timeline = self._timeline([(100, 200), (400, 500)])
        rows = archive_timeline.archive_media(timeline)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["vod_start"] for r in rows}, {100.0, 400.0})
        self.assertTrue(all(r["media_role"] == "normalized" for r in rows))

    def test_entry_can_be_looked_up_by_media_id(self):
        timeline = self._timeline([(100, 200)])
        media_id = archive_timeline.archive_section(timeline)["clips"][0]["media_id"]
        entry = archive_timeline.archive_media_entry(timeline, media_id)
        self.assertEqual((entry["vod_start"], entry["vod_end"]), (100.0, 200.0))
        self.assertIsNone(archive_timeline.archive_media_entry(timeline, "m404"))

    # 素材単位の控えが無い旧プロジェクトは、セクションの控えから引く
    def test_falls_back_to_the_section_record(self):
        timeline = self._timeline([(100, 200)])
        media_id = archive_timeline.archive_section(timeline)["clips"][0]["media_id"]
        del timeline.source["archive"]["media"]
        entry = archive_timeline.archive_media_entry(timeline, media_id)
        self.assertEqual((entry["vod_start"], entry["vod_end"]), (100.0, 200.0))


class MergeKeepsMediaRecordTest(ArchiveMediaTestBase):
    """§2.2 の回帰テスト: 統合しても畳まれた側の素材が復元できること"""

    def test_merge_keeps_both_media_records(self):
        timeline = self._timeline([(100, 200)])
        # 150-300 を足すと clip1 と統合され、差分 200-300 の素材が増える
        plan = self._add(timeline, 150, 300)
        self.assertEqual(plan["merge_indexes"], [1])

        # セクションの控えは 1 件へ畳まれる (今までどおり)
        clips = archive_timeline.archive_section(timeline)["clips"]
        self.assertEqual(len(clips), 1)
        self.assertEqual((clips[0]["vod_start"], clips[0]["vod_end"]), (100.0, 300.0))

        # ★ 素材の控えは 2 件とも残る (ここが壊れていた)
        rows = archive_timeline.archive_media(timeline)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r["vod_start"] for r in rows), [100.0, 200.0])

    # 統合後、Timeline が使うすべての素材が復元情報を持っていること
    def test_every_used_media_is_recoverable_after_merge(self):
        timeline = self._timeline([(100, 200)])
        self._add(timeline, 150, 300)
        for media_id in timeline.used_media_ids():
            self.assertIsNotNone(
                archive_timeline.archive_media_entry(timeline, media_id),
                f"素材 {media_id} を復元できません (開き直すと差し替えを聞かれる)")

    # Undo すると控えも元へ戻る
    def test_undo_restores_the_records(self):
        timeline = self._timeline([(100, 200)])
        self._add(timeline, 150, 300)
        self.assertEqual(len(archive_timeline.archive_media(timeline)), 2)
        self.stack.undo(timeline)
        self.assertEqual(len(archive_timeline.archive_media(timeline)), 1)


class RepairTest(ArchiveMediaTestBase):
    """§3.2 / §5.4: 既に壊れているプロジェクトの自動修復"""

    # 統合済みのプロジェクトから素材の控えを消し、壊れた状態を作る
    def _broken(self):
        timeline = self._timeline([(100, 200)])
        self._add(timeline, 150, 300)
        rows = archive_timeline.archive_media(timeline)
        target = next(r for r in rows if r["vod_start"] == 200.0)
        lost = target["media_id"]
        timeline.source["archive"]["media"] = [r for r in rows if r is not target]
        return timeline, lost

    def test_repairs_a_single_hole(self):
        timeline, lost = self._broken()
        self.assertIsNone(archive_timeline.archive_media_entry(timeline, lost))

        repaired = archive_timeline.repair_archive_media(timeline)
        self.assertEqual(repaired, [lost])
        entry = archive_timeline.archive_media_entry(timeline, lost)
        self.assertAlmostEqual(entry["vod_start"], 200.0, places=1)
        self.assertAlmostEqual(entry["vod_end"], 300.0, places=1)

    # キーフレーム吸着で素材が少し長くなっていても当てられる
    def test_tolerates_a_slightly_longer_media(self):
        timeline, lost = self._broken()
        timeline.media_by_id(lost).duration_sec = 103.0      # 穴 100s に対して 3s 長い
        self.assertEqual(archive_timeline.repair_archive_media(timeline), [lost])

    # 尺が明らかに違えば当てない (黙って違う区間を切り出さない)
    def test_does_not_guess_when_the_length_is_far_off(self):
        timeline, lost = self._broken()
        timeline.media_by_id(lost).duration_sec = 12.0
        self.assertEqual(archive_timeline.repair_archive_media(timeline), [])
        self.assertIsNone(archive_timeline.archive_media_entry(timeline, lost))

    # 既に控えがあるプロジェクトには何もしない
    def test_healthy_project_is_untouched(self):
        timeline = self._timeline([(100, 200)])
        self._add(timeline, 150, 300)
        before = json.dumps(archive_timeline.archive_media(timeline), sort_keys=True)
        self.assertEqual(archive_timeline.repair_archive_media(timeline), [])
        self.assertEqual(
            json.dumps(archive_timeline.archive_media(timeline), sort_keys=True), before)

    # 修復すると「使用中」の判定も正しくなる (B3 と地続き)
    def test_repair_restores_the_used_ranges(self):
        timeline, _lost = self._broken()
        # 控えが無いあいだは、その素材の区間を「使用中」と数えられない
        self.assertEqual(archive_timeline.used_vod_ranges(timeline, 1), [(100.0, 200.0)])
        archive_timeline.repair_archive_media(timeline)
        self.assertEqual(archive_timeline.used_vod_ranges(timeline, 1, 1.0),
                         [(100.0, 300.0)])


class UnusedMediaTest(ArchiveMediaTestBase):
    """§3.3 / §5.5: 使われていない素材は尋ねず・保存もしない"""

    def setUp(self):
        super().setUp()
        self._dir = tempfile.mkdtemp(prefix="archive_media_test_")
        self.addCleanup(shutil.rmtree, self._dir, ignore_errors=True)

    # どのクリップからも使われていない素材を 1 件足す。
    # 使われている素材の実体は作っておく (そちらが復旧対象にならないように)。
    def _with_orphan(self):
        timeline = self._timeline([(100, 200)])
        present = os.path.join(self._dir, "used.mp4")
        with open(present, "wb") as handle:
            handle.write(b"")
        for media in timeline.media_pool:
            media.path = present
        timeline.media_pool.append(
            MediaRef("orphan", "video", os.path.join(self._dir, "gone.mp4"),
                     60.0, 1920, 1080, 60.0, True))
        return timeline

    def test_used_media_ids_ignores_orphans(self):
        timeline = self._with_orphan()
        self.assertNotIn("orphan", timeline.used_media_ids())

    # B1 (b) の回帰テスト: 残骸について差し替えを尋ねない
    def test_orphan_is_not_asked_about(self):
        timeline = self._with_orphan()
        asked = []
        result = media_recovery.recover(
            timeline, os.path.join(self._dir, "p.timeline.json"), {"timeline": {}},
            relink_callback=lambda info: asked.append(info["media_id"]) or None)
        self.assertEqual(asked, [], "使われていない素材の差し替えを尋ねています")
        self.assertNotIn("orphan", result["missing"])

    # 使われている素材は今までどおり尋ねる
    def test_used_media_is_still_asked_about(self):
        timeline = self._with_orphan()
        used_id = timeline.base_video_track().clips[0].media_id
        timeline.media_by_id(used_id).path = os.path.join(self._dir, "lost.mp4")
        asked = []
        media_recovery.recover(
            timeline, os.path.join(self._dir, "p.timeline.json"), {"timeline": {}},
            relink_callback=lambda info: asked.append(info["media_id"]) or None)
        self.assertEqual(asked, [used_id])

    # 「使わない」にしただけのクリップが指す素材は残す
    def test_disabled_clip_keeps_its_media(self):
        timeline = self._timeline([(100, 200)])
        timeline.base_video_track().clips[0].enabled = False
        used = timeline.used_media_ids()
        self.assertEqual(len(used), 1)

    # 保存する JSON から残骸を落とす。Timeline そのものは変えない
    def test_orphan_is_not_saved(self):
        timeline = self._with_orphan()
        data = json.loads(project_io.to_json(timeline))
        self.assertNotIn("orphan", [m["id"] for m in data["media_pool"]])
        self.assertIsNotNone(timeline.media_by_id("orphan"),
                             "メディアプールそのものを変えてはいけない (Undo が壊れる)")

    def test_can_be_turned_off(self):
        timeline = self._with_orphan()
        data = json.loads(project_io.to_json(timeline, drop_unused_media=False))
        self.assertIn("orphan", [m["id"] for m in data["media_pool"]])


class InsertPositionInsideSectionTest(ArchiveMediaTestBase):
    """§3.5 / §5.7: セクションの内側へ足した区間が元動画の時系列どおりに入る"""

    def test_inside_hole_goes_before_the_later_part(self):
        timeline = self._timeline([(100, 200), (400, 500)])
        # clip1 の前半 (100-150) を削り、後半 (150-200) だけ残す
        clip = timeline.base_video_track().clips[0]
        clip.source_in = 50.0
        clip.duration = 50.0

        # 削った 100-150 を足し直す
        plan = self._add(timeline, 100, 150)
        self.assertIsNotNone(plan)
        base = timeline.base_video_track()
        rows = sorted(
            ((c.timeline_start,
              archive_timeline.archive_media_entry(timeline, c.media_id)["vod_start"]
              + c.source_in)
             for c in base.clips), key=lambda r: r[0])
        # Timeline 順に並べたとき、VOD の時刻も昇順になっていること
        vods = [round(v, 1) for _t, v in rows]
        self.assertEqual(vods, sorted(vods), f"元動画と逆順に並んでいます: {vods}")
        self.assertEqual(vods[0], 100.0)


if __name__ == "__main__":
    unittest.main()
