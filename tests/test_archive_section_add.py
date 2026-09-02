# アーカイブ Timeline へのセクション追加 (ver3 resolve13 §10-2) の単体テスト
# 実行: python -m unittest discover -s tests
# 重点:
#   ・元動画の時系列に合う位置へ挿入されること (先頭 / 末尾 / 間)
#   ・既存セクションと重なる場合に 1 つへ統合されること
#   ・統合しても既存セクションの編集が保たれること (差分だけ用意する方式)
#   ・セクション番号を振り直さないこと
#   ・Undo で完全に元へ戻ること
# ffmpeg・音声認識は呼ばない。media_probe.probe だけ差し替えて素材を模す。
import unittest

from src.archive import config as archive_config
from src.archive import timeline_builder as archive_timeline
from src.timeline import commands, media_probe
from src.timeline.model import MediaRef

# 素材の尺 (パス → 秒)。差し替えた probe が参照する。
_DURATIONS = {}


# 実ファイル無しで MediaRef を返す probe の代用
def _fake_probe(path, media_id, settings=None, cfg=None):
    return MediaRef(media_id, "video", path, _DURATIONS.get(path, 60.0),
                    1920, 1080, 60.0, True)


# prepare_one_clip の戻り値を模した 1 件を作る
def _entry(index, start, end, items=None):
    path = f"section{index}.mp4"
    _DURATIONS[path] = end - start
    return {
        "index": index, "start": float(start), "end": float(end), "score": 10.0,
        "prepared_path": "", "profile": None, "eff_cfg": {},
        "items": items or [], "keep_segments": None,
        "normalized_path": path, "normalized_duration": float(end - start),
        "media_role": "normalized",
    }


class SectionAddTestBase(unittest.TestCase):

    def setUp(self):
        self._original_probe = media_probe.probe
        media_probe.probe = _fake_probe
        self.settings = {"ffmpeg": {}}
        self.stack = commands.CommandStack()

    def tearDown(self):
        media_probe.probe = self._original_probe

    # 元動画の区間一覧から初期 Timeline を組む
    def _timeline(self, spans, items=None):
        self.prepared = [_entry(i, s, e, items) for i, (s, e) in enumerate(spans, 1)]
        return archive_timeline.build_archive_timeline(
            self.prepared, self.settings, source_path="vod.mp4")

    # Timeline 上の (開始位置, セクション番号) を並び順で返す
    def _layout(self, timeline):
        base = timeline.base_video_track()
        return [(round(c.timeline_start, 3), c.origin.get(archive_timeline.ORIGIN_ARCHIVE_INDEX))
                for c in sorted(base.clips, key=lambda c: c.timeline_start)]

    # 元動画の区間を Timeline 位置順で返す
    def _spans(self, timeline):
        return [(r["index"], r["vod_start"], r["vod_end"])
                for r in archive_timeline.sections_by_vod(timeline)]

    # 区間 [start, end] を追加する (計画 → 素材の用意 → コマンド)
    def _add(self, timeline, start, end, merge=True):
        plan = archive_timeline.plan_section_add(
            timeline, start, end, merge_on_overlap=merge)
        if plan is None:
            return None, False
        base = archive_timeline.next_section_index(timeline)
        entries = [_entry(base + i, s, e) for i, (s, e) in enumerate(plan["ranges"])]
        self.added = entries
        changed = self.stack.push(
            timeline, archive_timeline.AddArchiveSection(
                entries, self.settings, plan))
        return plan, changed


class InsertPositionTest(SectionAddTestBase):
    """resolve13 §3-2: 元動画の時系列として正しい位置へ挿入する (要望 G3)"""

    # どのセクションよりも前 → Timeline の先頭 (G3-a)
    def test_insert_before_all(self):
        timeline = self._timeline([(100, 200), (400, 500)])
        _plan, changed = self._add(timeline, 0, 50)
        self.assertTrue(changed)
        layout = self._layout(timeline)
        self.assertEqual(layout[0], (0.0, 3))          # 追加分が先頭
        self.assertEqual([i for _, i in layout], [3, 1, 2])

    # どのセクションよりも後ろ → Timeline の最後 (G3-b)
    def test_insert_after_all(self):
        timeline = self._timeline([(100, 200), (400, 500)])
        self._add(timeline, 900, 950)
        self.assertEqual([i for _, i in self._layout(timeline)], [1, 2, 3])

    # セクションとセクションの間 → その間へ入り、以降が右へずれる (G3-c)
    def test_insert_between(self):
        timeline = self._timeline([(100, 200), (400, 500)])
        self._add(timeline, 250, 300)
        layout = self._layout(timeline)
        self.assertEqual([i for _, i in layout], [1, 3, 2])
        # clip1 は動かず、追加分 (50s) のぶん clip2 が後ろへずれる
        self.assertEqual(layout[0][0], 0.0)
        self.assertEqual(layout[1][0], 100.0)
        self.assertEqual(layout[2][0], 150.0)

    # 挿入後も「Timeline の並び = 元動画の時系列」が保たれる (§2.2 の不変条件)
    def test_insert_keeps_vod_order(self):
        timeline = self._timeline([(100, 200), (400, 500), (700, 800)])
        self._add(timeline, 250, 300)
        self._add(timeline, 0, 50)
        self._add(timeline, 900, 950)
        starts = [start for _index, start, _end in self._spans(timeline)]
        self.assertEqual(starts, sorted(starts))

    # 追加分の字幕も Timeline へ載る (要望 G4)
    def test_subtitles_are_added(self):
        timeline = self._timeline([(100, 200)])
        plan = archive_timeline.plan_section_add(timeline, 300, 400)
        entry = _entry(archive_timeline.next_section_index(timeline), 300, 400,
                       items=[{"start": 1.0, "end": 3.0, "text": "追加テロップ",
                               "use": True, "role": "streamer"}])
        self.stack.push(timeline, archive_timeline.AddArchiveSection(
            [entry], self.settings, plan))
        texts = [c.text for track in timeline.subtitle_tracks() for c in track.clips]
        self.assertIn("追加テロップ", texts)


class SectionMergeTest(SectionAddTestBase):
    """resolve13 §3-4: 既存セクションと重なる場合は 1 つへ統合する (要望 G5)"""

    # 重なり → 代表番号へ統合され、元動画の区間が和集合になる
    def test_overlap_is_merged(self):
        timeline = self._timeline([(100, 200), (400, 500), (700, 800)])
        plan, changed = self._add(timeline, 450, 600)
        self.assertTrue(changed)
        self.assertEqual(plan["target_index"], 2)
        self.assertEqual(plan["ranges"], [(500.0, 600.0)])   # 差分だけ用意する
        self.assertEqual(self._spans(timeline),
                         [(1, 100.0, 200.0), (2, 400.0, 600.0), (3, 700.0, 800.0)])

    # 接触 (終端と開始が一致) も統合する (scoring._merge_time_sections と同規約)
    def test_touching_range_is_merged(self):
        timeline = self._timeline([(100, 200)])
        plan, _changed = self._add(timeline, 200, 300)
        self.assertEqual(plan["target_index"], 1)
        self.assertEqual(self._spans(timeline), [(1, 100.0, 300.0)])

    # 統合しても既存セクションのクリップは置き換わらない (要望 G10 / §3-4 案B)
    def test_merge_preserves_existing_clips(self):
        timeline = self._timeline([(100, 200), (400, 500)])
        base = timeline.base_video_track()
        before = {c.id: (c.media_id, c.source_in, c.source_out)
                  for c in base.clips
                  if c.origin.get(archive_timeline.ORIGIN_ARCHIVE_INDEX) == 2}
        self.assertTrue(before)
        self._add(timeline, 450, 600)
        after = {c.id: (c.media_id, c.source_in, c.source_out) for c in base.clips}
        for clip_id, value in before.items():
            self.assertIn(clip_id, after, "既存クリップが作り直されている")
            self.assertEqual(after[clip_id], value)

    # 統合後は 1 つのセクションとして書き出される (§3-4 ④の担保)
    def test_split_by_clip_groups_merged_section(self):
        timeline = self._timeline([(100, 200), (400, 500), (700, 800)])
        self._add(timeline, 150, 750)          # 3 セクションすべてを跨ぐ
        groups = [index for index, _sub in archive_timeline.split_by_clip(timeline)]
        self.assertEqual(groups, [1])
        self.assertEqual(self._spans(timeline), [(1, 100.0, 800.0)])

    # 完全に含まれる区間は追加するものが無い (履歴も汚さない)
    def test_contained_range_is_noop(self):
        timeline = self._timeline([(100, 200), (400, 500)])
        self.assertIsNone(archive_timeline.plan_section_add(timeline, 420, 480))

    # merge_on_overlap=False なら統合せず別セクションとして並べる (切り戻し)
    def test_merge_can_be_disabled(self):
        timeline = self._timeline([(400, 500)])
        plan, changed = self._add(timeline, 450, 600, merge=False)
        self.assertTrue(changed)
        self.assertIsNone(plan["target_index"])
        self.assertEqual(len(self._spans(timeline)), 2)

    # 統合時は代表セクションの prepared を和集合へ広げる (§3-4 ⑥)
    # イントロカードの切り出し位置と個別出力のファイル名がこれを使う。
    def test_merge_updates_prepared_span(self):
        timeline = self._timeline([(100, 200), (400, 500)])
        plan, _changed = self._add(timeline, 450, 600)
        rows = archive_timeline.apply_prepared_after_add(
            self.prepared, plan, self.added)
        merged = next(r for r in rows if r["index"] == 2)
        self.assertEqual((merged["start"], merged["end"]), (400.0, 600.0))
        # 差分は代表へ畳むため独立した行は増えない
        self.assertEqual(len(rows), 2)


class SectionIndexTest(SectionAddTestBase):
    """resolve13 §3-3: セクション番号は振り直さない (要望 G7)"""

    # 既存セクションの番号は追加で変わらない (origin の参照が壊れないこと)
    def test_index_is_not_renumbered(self):
        timeline = self._timeline([(100, 200), (400, 500), (700, 800)])
        self._add(timeline, 250, 300)          # clip1 と clip2 の間へ挿入
        indexes = sorted(i for i, _s, _e in self._spans(timeline))
        self.assertEqual(indexes, [1, 2, 3, 4])
        # 番号 1..3 の元動画区間が変わっていないこと
        spans = {i: (s, e) for i, s, e in self._spans(timeline)}
        self.assertEqual(spans[1], (100.0, 200.0))
        self.assertEqual(spans[2], (400.0, 500.0))
        self.assertEqual(spans[3], (700.0, 800.0))

    # 追加番号は既存の最大 + 1
    def test_next_index_is_max_plus_one(self):
        timeline = self._timeline([(100, 200), (400, 500)])
        self.assertEqual(archive_timeline.next_section_index(timeline), 3)
        self._add(timeline, 900, 950)
        self.assertEqual(archive_timeline.next_section_index(timeline), 4)


class SectionUndoTest(SectionAddTestBase):
    """resolve13 §5.5: 追加は 1 コマンドで、Undo で完全に戻る (要望 G9)"""

    # Timeline の状態を比較用に写し取る
    def _snapshot(self, timeline):
        return (
            self._layout(timeline),
            self._spans(timeline),
            len(timeline.media_pool),
            sorted(c.text for track in timeline.subtitle_tracks() for c in track.clips),
        )

    def test_undo_restores_timeline(self):
        timeline = self._timeline([(100, 200), (400, 500), (700, 800)])
        before = self._snapshot(timeline)
        self._add(timeline, 250, 300)
        self.assertNotEqual(self._snapshot(timeline), before)
        self.stack.undo(timeline)
        self.assertEqual(self._snapshot(timeline), before)

    def test_redo_reapplies(self):
        timeline = self._timeline([(100, 200), (400, 500)])
        self._add(timeline, 250, 300)
        added = self._snapshot(timeline)
        self.stack.undo(timeline)
        self.stack.redo(timeline)
        self.assertEqual(self._snapshot(timeline), added)

    # 統合も 1 コマンド。番号の付け替えごと戻る。
    def test_undo_restores_merged_indexes(self):
        timeline = self._timeline([(100, 200), (400, 500)])
        before = self._snapshot(timeline)
        self._add(timeline, 450, 600)
        self.stack.undo(timeline)
        self.assertEqual(self._snapshot(timeline), before)


class SectionScoreTest(unittest.TestCase):
    """resolve13 §5.4: 追加区間のスコアは窓スコア列から推定する (採点はやり直さない)"""

    def test_estimated_score_from_curve(self):
        curve = [{"start": 0, "end": 100, "total": 12.0},
                 {"start": 100, "end": 200, "total": 44.5},
                 {"start": 200, "end": 300, "total": 8.0}]
        self.assertEqual(
            archive_timeline.estimate_section_score(curve, 120, 180), 44.5)
        # 複数の窓に重なるときは最大値
        self.assertEqual(
            archive_timeline.estimate_section_score(curve, 50, 250), 44.5)

    # 重なる窓が無ければ 0.0 (採点していない区間を偽らない)
    def test_score_without_curve(self):
        self.assertEqual(archive_timeline.estimate_section_score([], 0, 10), 0.0)
        self.assertEqual(
            archive_timeline.estimate_section_score(
                [{"start": 0, "end": 10, "total": 50.0}], 100, 200), 0.0)


class SectionAddConfigTest(unittest.TestCase):
    """resolve13 §7: 設定は既定で有効・1 つで機能ごと止められる"""

    def test_defaults(self):
        cfg = archive_config.section_add_config({})
        self.assertTrue(cfg["enabled"])
        self.assertTrue(cfg["merge_on_overlap"])
        self.assertEqual(cfg["merge_mode"], "delta")

    # 既定尺は採点の窓幅へ揃える (値を二重に持たない)
    def test_default_length_follows_window(self):
        cfg = archive_config.section_add_config(
            {"archive": {"scoring": {"window_sec": 300}}})
        self.assertEqual(cfg["default_length_sec"], 300.0)

    def test_can_be_disabled(self):
        cfg = archive_config.section_add_config(
            {"archive": {"section_add": {"enabled": False}}})
        self.assertFalse(cfg["enabled"])


if __name__ == "__main__":
    unittest.main()
