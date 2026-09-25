# ぼかしの指定 (src/blur/decisions.py) と設定 / コマンドの単体テスト
# 実行: python -m unittest discover -s tests
# 重点 (docs/request/ver5/resolve8.md §8.1 A / F / §8.2):
#   ・書式 v5 (全面ぼかし + 囲み + キーフレーム) が往復すること
#   ・キーフレームの置き換え・削除の決めごと (同じコマは 1 つ / 最後の 1 件は消さない)
#   ・指定はクリップの素材区間 (span) に閉じること
#   ・旧キーだけの setting.json でも読めること (§7 の読み替え)
#   ・v4 以前の指定を読んでも落ちず、移せるものだけ残ること (§3.9)
#   ・すべての操作が Undo できること (R15)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.blur import decisions as blur_decisions
from src.blur.config import config
from src.timeline import commands
from src.timeline.model import (
    ORIGIN_SILENCE_CUT,
    TRACK_AUDIO,
    TRACK_SUBTITLE,
    TRACK_VIDEO,
    AudioClip,
    Clip,
    MediaRef,
    Timeline,
    Track,
)


# ベースクリップ 2 本 (0〜5 秒 / 5〜10 秒) の Timeline。素材の時刻もそのまま 0〜5 / 5〜10。
def _build_timeline(source=None):
    media = [MediaRef("m1", "video", __file__, 100.0, 1920, 1080, 60, True)]
    video = Track("V1", TRACK_VIDEO, 1, name="Video 1", is_base=True, clips=[
        Clip("c1", "m1", 0.0, 5.0, 0.0, 5.0, origin={"type": ORIGIN_SILENCE_CUT}),
        Clip("c2", "m1", 5.0, 5.0, 5.0, 10.0, origin={"type": ORIGIN_SILENCE_CUT}),
    ])
    audio = Track("A1", TRACK_AUDIO, 1, name="Audio 1", link_track="V1",
                  clips=[AudioClip("a1", "c1"), AudioClip("a2", "c2")])
    subtitle = Track("S1", TRACK_SUBTITLE, 1, name="Subtitle 1")
    base = {"media_id": "m1", "input_path": __file__}
    base.update(source or {})
    return Timeline(fps=60, width=1920, height=1080, source=base,
                    media_pool=media, tracks=[video, audio, subtitle])


class SpecFormatTest(unittest.TestCase):

    def setUp(self):
        self.timeline = _build_timeline()
        self.clip = self.timeline.base_clips()[0]
        self.span = blur_decisions.span_for(self.timeline, self.clip)

    def _stored(self, decisions):
        blur_decisions.store(self.timeline, decisions)
        return blur_decisions.load(self.timeline)

    # A1: 保存 → 読み戻しで並び順とキーフレームまで一致すること
    def test_specs_round_trip(self):
        decisions = blur_decisions._empty()      # noqa: SLF001 (テストのため内部を使う)
        decisions = blur_decisions.with_spec(
            decisions, blur_decisions.make_frame_spec("b1", "m1", self.span), to_bottom=True)
        decisions = blur_decisions.with_spec(decisions, blur_decisions.make_area_spec(
            "b2", blur_decisions.KEEP, "m1", self.span, 2.0, (0.3, 0.2, 0.1, 0.4),
            label="店員"))
        loaded = self._stored(decisions)
        self.assertEqual([s["id"] for s in loaded["specs"]], ["b1", "b2"])
        self.assertEqual(loaded["specs"][0]["kind"], blur_decisions.KIND_FRAME)
        self.assertEqual(loaded["specs"][1]["label"], "店員")
        self.assertEqual(blur_decisions.keys_of(loaded["specs"][1])[0]["rect"],
                         [0.3, 0.2, 0.1, 0.4])

    # A2: 一度使った番号は消しても戻らないこと
    def test_next_spec_id_never_reuses(self):
        decisions = blur_decisions.with_spec(
            blur_decisions._empty(),             # noqa: SLF001
            blur_decisions.make_frame_spec("b1", "m1", self.span))
        decisions = blur_decisions.without_spec(decisions, "b1")
        self.assertEqual(blur_decisions.next_spec_id(decisions), "b2")

    # A3: 区間はクリップの素材区間を写し取ること
    def test_span_for_clip(self):
        self.assertEqual(self.span["start"], 0.0)
        self.assertEqual(self.span["end"], 5.0)
        self.assertEqual(self.span["clip_id"], "c1")
        self.assertEqual(self.span["label"], "クリップ 1")

    # A4: 区間の境界は含むこと
    def test_span_contains_boundaries(self):
        spec = blur_decisions.make_frame_spec("b1", "m1", self.span)
        self.assertTrue(blur_decisions.span_contains(spec, "m1", 0.0))
        self.assertTrue(blur_decisions.span_contains(spec, "m1", 5.0))
        self.assertFalse(blur_decisions.span_contains(spec, "m1", 5.01))
        self.assertFalse(blur_decisions.span_contains(spec, "m2", 1.0))

    # A5 / A6: 同じコマへ 2 回打つと置き換わり、時刻の昇順に並ぶこと
    def test_with_key_replaces_and_sorts(self):
        decisions = blur_decisions.with_spec(
            blur_decisions._empty(),             # noqa: SLF001
            blur_decisions.make_area_spec("b1", blur_decisions.BLUR, "m1", self.span,
                                          1.0, (0.1, 0.1, 0.2, 0.2)))
        decisions = blur_decisions.with_key(decisions, "b1", 3.0, (0.5, 0.1, 0.2, 0.2))
        decisions = blur_decisions.with_key(decisions, "b1", 2.0, (0.3, 0.1, 0.2, 0.2))
        decisions = blur_decisions.with_key(decisions, "b1", 2.0, (0.4, 0.1, 0.2, 0.2))
        keys = blur_decisions.keys_of(decisions["specs"][0])
        self.assertEqual([k["t"] for k in keys], [1.0, 2.0, 3.0])
        self.assertEqual(keys[1]["rect"][0], 0.4)

    # A7: キーフレームが 1 件しか無ければ消さないこと
    def test_last_key_is_kept(self):
        decisions = blur_decisions.with_spec(
            blur_decisions._empty(),             # noqa: SLF001
            blur_decisions.make_area_spec("b1", blur_decisions.BLUR, "m1", self.span,
                                          1.0, (0.1, 0.1, 0.2, 0.2)))
        decisions = blur_decisions.without_key(decisions, "b1", 1.0)
        self.assertEqual(len(blur_decisions.keys_of(decisions["specs"][0])), 1)

    # A8: 重ね順を動かせること。端では動かないこと
    def test_move_spec(self):
        decisions = blur_decisions._empty()      # noqa: SLF001
        for index in range(1, 4):
            decisions = blur_decisions.with_spec(
                decisions, blur_decisions.make_frame_spec(f"b{index}", "m1", self.span))
        moved = blur_decisions.with_spec_moved(decisions, "b1", +1)
        self.assertEqual([s["id"] for s in moved["specs"]], ["b2", "b1", "b3"])
        edge = blur_decisions.with_spec_moved(decisions, "b1", -1)
        self.assertEqual([s["id"] for s in edge["specs"]], ["b1", "b2", "b3"])

    # A9: 全面ぼかしだけなら追従は要らないこと
    def test_needs_mask_and_tracking(self):
        frame_only = blur_decisions.with_spec(
            blur_decisions._empty(),             # noqa: SLF001
            blur_decisions.make_frame_spec("b1", "m1", self.span))
        self.assertTrue(blur_decisions.needs_mask(frame_only))
        self.assertFalse(blur_decisions.needs_tracking(frame_only))

        with_area = blur_decisions.with_spec(frame_only, blur_decisions.make_area_spec(
            "b2", blur_decisions.KEEP, "m1", self.span, 1.0, (0.1, 0.1, 0.2, 0.2)))
        self.assertTrue(blur_decisions.needs_tracking(with_area))
        fixed = blur_decisions.with_spec_follow(with_area, "b2", blur_decisions.FOLLOW_FIXED)
        self.assertFalse(blur_decisions.needs_tracking(fixed))

    # 指定は自分のクリップにだけ出ること (R11)
    def test_specs_in_clip(self):
        first, second = self.timeline.base_clips()
        decisions = blur_decisions.with_spec(
            blur_decisions._empty(),             # noqa: SLF001
            blur_decisions.make_frame_spec("b1", "m1", self.span))
        self.assertEqual(len(blur_decisions.specs_in_clip(decisions, first)), 1)
        self.assertEqual(blur_decisions.specs_in_clip(decisions, second), [])

    # 潰れた矩形は指定を作らないこと
    def test_degenerate_rect_is_rejected(self):
        self.assertIsNone(blur_decisions.make_area_spec(
            "b1", blur_decisions.BLUR, "m1", self.span, 1.0, (0.5, 0.5, 0.0, 0.2)))

    # 壊れた項目は落ちずに捨てられること (M7)
    def test_broken_entries_are_dropped(self):
        timeline = _build_timeline({"blur": {"version": 5, "specs": [
            {"id": "b1", "kind": "area", "mode": "blur", "media_id": "m1",
             "span": {"start": 0.0, "end": 5.0}, "keys": []},              # キーが空
            {"id": "b2", "kind": "frame", "mode": "blur", "media_id": "",
             "span": {"start": 0.0, "end": 5.0}},                          # 素材なし
            {"id": "b3", "kind": "frame", "mode": "blur", "media_id": "m1",
             "span": {"start": 5.0, "end": 5.0}},                          # 区間が 0
            {"id": "b4", "kind": "frame", "mode": "keep", "media_id": "m1",
             "span": {"start": 0.0, "end": 5.0}},                          # 全面は blur へ直す
        ]}})
        loaded = blur_decisions.load(timeline)
        self.assertEqual([s["id"] for s in loaded["specs"]], ["b4"])
        self.assertEqual(loaded["specs"][0]["mode"], blur_decisions.BLUR)


class ConfigTest(unittest.TestCase):
    """§7 の読み替えと丸め (A10〜A12)"""

    # A10: 旧キーだけの設定でも読めること
    def test_legacy_keys_are_read(self):
        cfg = config({"blur": {
            "analysis": {"sample_fps": 3.0, "auto_start": False},
            "region": {"hold_sec": 2.5},
            "manual": {"search_ratio": 2.0, "min_iou": 0.4, "match_psr": 12.0,
                       "detector_score": 0.35},
            "spec": {"preview_mode": "keep", "show_overlays": False, "name_max_len": 8},
        }})
        self.assertEqual(cfg["track"]["sample_fps"], 3.0)
        self.assertFalse(cfg["track"]["auto_start"])
        self.assertEqual(cfg["track"]["hold_sec"], 2.5)
        self.assertEqual(cfg["track"]["search_ratio"], 2.0)
        self.assertEqual(cfg["track"]["min_iou"], 0.4)
        self.assertEqual(cfg["track"]["match_psr"], 12.0)
        self.assertEqual(cfg["model"]["detector_score"], 0.35)
        self.assertEqual(cfg["editor"]["preview_mode"], "keep")
        self.assertFalse(cfg["editor"]["show_overlays"])
        self.assertEqual(cfg["editor"]["name_max_len"], 8)

    # 新しいキーがあれば旧キーより優先すること
    def test_new_keys_win(self):
        cfg = config({"blur": {"analysis": {"sample_fps": 3.0},
                               "track": {"sample_fps": 7.0}}})
        self.assertEqual(cfg["track"]["sample_fps"], 7.0)

    # A11: 廃止した塗り方・表示モードは近いものへ落ちること
    def test_removed_choices_fall_back(self):
        self.assertEqual(config({"blur": {"render": {"shape": "silhouette"}}})["render"]["shape"],
                         "rounded")
        self.assertEqual(config({"blur": {"editor": {"preview_mode": "mask"}}})
                         ["editor"]["preview_mode"], "keep")

    # A12: 壊れた値はすべて既定へ落ちること
    def test_broken_values_fall_back(self):
        cfg = config({"blur": {
            "render": {"strength": "abc", "mask_scale": 99.0, "feather_ratio": -1.0},
            "track": {"sample_fps": float("nan"), "min_iou": 5.0},
            "editor": {"step_frames": 0, "handle_px": 999, "min_size_ratio": "x"},
        }})
        self.assertEqual(cfg["render"]["strength"], 50)
        self.assertEqual(cfg["render"]["mask_scale"], 1.0)
        self.assertEqual(cfg["render"]["feather_ratio"], 0.0)
        self.assertEqual(cfg["track"]["sample_fps"], 10.0)
        self.assertEqual(cfg["track"]["min_iou"], 0.99)
        self.assertEqual(cfg["editor"]["step_frames"], 1)
        self.assertEqual(cfg["editor"]["handle_px"], 40)
        self.assertEqual(cfg["editor"]["min_size_ratio"], 0.01)


class MigrationTest(unittest.TestCase):
    """v4 以前の指定の読み替え (§3.9 / M1〜M8)"""

    # M1 / M4 / M5: 全面ぼかしは引き継ぎ、人物の枠への指定は捨てること
    def test_v4_marks_are_migrated(self):
        timeline = _build_timeline({"blur": {
            "version": 4,
            "marks": [
                {"id": "m1", "mode": "blur", "target": {"kind": "frame"},
                 "scope": {"media_id": "m1", "start": 0.0, "end": 5.0, "width": "clip",
                           "clip_id": "c1", "label": "クリップ 1"}},
                {"id": "m2", "mode": "keep",
                 "target": {"kind": "track",
                            "anchor": {"media_id": "m1", "t": 1.0, "rect": [1, 2, 3, 4]}},
                 "scope": {"media_id": "m1", "start": 0.0, "end": 5.0, "width": "clip"}},
            ],
            "excluded": [{"media_id": "m1", "t": 2.0, "rect": [1, 2, 3, 4]}],
            "names": [{"anchor": {"media_id": "m1", "t": 1.0, "rect": [1, 2, 3, 4]},
                       "name": "だれか"}],
        }})
        loaded = blur_decisions.load(timeline)
        self.assertTrue(loaded["migrated"])
        self.assertEqual(len(loaded["specs"]), 1)
        self.assertEqual(loaded["specs"][0]["kind"], blur_decisions.KIND_FRAME)
        self.assertEqual(loaded["specs"][0]["span"]["end"], 5.0)
        self.assertEqual(loaded["migrated_dropped"], 1)
        self.assertEqual(loaded["migrated_kept"], 1)

    # M2: 「この素材全体」の指定は区間をそのまま引き継ぐこと
    def test_media_scope_keeps_its_span(self):
        timeline = _build_timeline({"blur": {
            "version": 4,
            "marks": [{"id": "m1", "mode": "blur", "target": {"kind": "frame"},
                       "scope": {"media_id": "m1", "start": 0.0, "end": 100.0,
                                 "width": "media"}}],
        }})
        loaded = blur_decisions.load(timeline)
        self.assertEqual(loaded["specs"][0]["span"]["end"], 100.0)

    # M3: 手で囲んだ枠は囲みの指定へ移り、形と名前を引き継ぐこと
    def test_v4_region_becomes_an_area(self):
        timeline = _build_timeline({"blur": {
            "version": 4,
            "marks": [{"id": "m1", "mode": "keep",
                       "target": {"kind": "region", "id": "r1"},
                       "scope": {"media_id": "m1", "start": 0.0, "end": 5.0,
                                 "width": "clip"}}],
            "regions": [{"id": "r1", "kind": "object", "label": "看板", "media_id": "m1",
                         "anchor_sec": 3.0, "follow": "fixed",
                         "path": [[0.2, 0.3], [0.5, 0.3], [0.5, 0.7], [0.2, 0.7]]}],
        }})
        loaded = blur_decisions.load(timeline)
        spec = loaded["specs"][0]
        self.assertEqual(spec["kind"], blur_decisions.KIND_AREA)
        self.assertEqual(spec["mode"], blur_decisions.KEEP)
        self.assertEqual(spec["label"], "看板")
        self.assertEqual(spec["follow"], blur_decisions.FOLLOW_FIXED)
        self.assertTrue(spec["outline"], "自由な囲みの形を引き継いでいません")
        key = blur_decisions.keys_of(spec)[0]
        self.assertEqual(key["t"], 3.0)
        self.assertEqual(key["rect"], [0.2, 0.3, 0.3, 0.4])

    # M6: v3 以前 (人物への指定) でも落ちずに読めること
    def test_v3_section_does_not_crash(self):
        timeline = _build_timeline({"blur": {
            "version": 3,
            "default_policy": "blur_others",
            "identities": {"p1": "keep", "p2": "blur"},
            "merges": [["p1", "p2"]],
            "splits": [{"media_id": "m1", "t": 1.0, "rect": [0, 0, 10, 10], "as": "s1"}],
        }})
        loaded = blur_decisions.load(timeline)
        self.assertEqual(loaded["specs"], [])
        self.assertTrue(loaded["migrated"])
        self.assertEqual(loaded["migrated_dropped"], 4)

    # M8: v5 は読み替えが走らないこと
    def test_v5_is_not_migrated(self):
        timeline = _build_timeline()
        blur_decisions.store(timeline, blur_decisions.with_spec(
            blur_decisions._empty(),             # noqa: SLF001
            blur_decisions.make_frame_spec("b1", "m1", {"start": 0.0, "end": 5.0})))
        self.assertFalse(blur_decisions.load(timeline)["migrated"])


class BlurCommandUndoTest(unittest.TestCase):
    """すべての操作が Undo できること (§8.1 F / R15)"""

    def setUp(self):
        self.timeline = _build_timeline()
        self.stack = commands.CommandStack()
        self.clip = self.timeline.base_clips()[0]
        self.span = blur_decisions.span_for(self.timeline, self.clip)

    def _push(self, command):
        return self.stack.push(self.timeline, command)

    def _specs(self):
        return blur_decisions.load(self.timeline)["specs"]

    def _add_keep_pair(self):
        return self._push(commands.AddBlurSpecs([
            blur_decisions.make_frame_spec("", "m1", self.span),
            blur_decisions.make_area_spec("", blur_decisions.KEEP, "m1", self.span,
                                          1.0, (0.3, 0.2, 0.1, 0.4)),
        ]))

    # F1: 「全面 + 囲み」は 1 回の Undo で両方消えること
    def test_add_pair_is_one_undo(self):
        self.assertTrue(self._add_keep_pair())
        self.assertEqual(len(self._specs()), 2)
        self.stack.undo(self.timeline)
        self.assertEqual(self._specs(), [])

    # 全面ぼかしは一番下、囲みはその上へ積まれること (R6)
    def test_frame_goes_to_the_bottom(self):
        self._add_keep_pair()
        specs = self._specs()
        self.assertEqual(specs[0]["kind"], blur_decisions.KIND_FRAME)
        self.assertEqual(specs[1]["kind"], blur_decisions.KIND_AREA)

    # F2 / F3: キーフレームの置き方と Undo
    def test_set_key_undo(self):
        self._add_keep_pair()
        area_id = self._specs()[1]["id"]
        self.assertTrue(self._push(commands.SetBlurKey(area_id, 3.0, (0.5, 0.2, 0.1, 0.4))))
        self.assertEqual(len(blur_decisions.keys_of(self._specs()[1])), 2)
        self.assertTrue(self._push(commands.SetBlurKey(area_id, 3.0, (0.6, 0.2, 0.1, 0.4))))
        self.assertEqual(len(blur_decisions.keys_of(self._specs()[1])), 2)
        self.stack.undo(self.timeline)
        self.assertEqual(blur_decisions.keys_of(self._specs()[1])[1]["rect"][0], 0.5)
        self.stack.undo(self.timeline)
        self.assertEqual(len(blur_decisions.keys_of(self._specs()[1])), 1)

    # 同じ位置へ置き直しても履歴を汚さないこと
    def test_same_key_does_not_push(self):
        self._add_keep_pair()
        area_id = self._specs()[1]["id"]
        self.assertTrue(self._push(commands.SetBlurKey(area_id, 3.0, (0.5, 0.2, 0.1, 0.4))))
        self.assertFalse(self._push(commands.SetBlurKey(area_id, 3.0, (0.5, 0.2, 0.1, 0.4))))

    # キーフレームの削除 (最後の 1 件は消さない)
    def test_remove_key(self):
        self._add_keep_pair()
        area_id = self._specs()[1]["id"]
        self._push(commands.SetBlurKey(area_id, 3.0, (0.5, 0.2, 0.1, 0.4)))
        self.assertTrue(self._push(commands.RemoveBlurKey(area_id, 3.0)))
        self.assertFalse(self._push(commands.RemoveBlurKey(area_id, 1.0)))

    # F4: 削除と Undo で並び順まで戻ること
    def test_remove_specs_undo(self):
        self._add_keep_pair()
        ids = [s["id"] for s in self._specs()]
        self.assertTrue(self._push(commands.RemoveBlurSpecs(ids)))
        self.assertEqual(self._specs(), [])
        self.stack.undo(self.timeline)
        self.assertEqual([s["id"] for s in self._specs()], ids)

    # F5: 重ね順の入れ替えと Undo
    def test_move_spec_undo(self):
        self._add_keep_pair()
        ids = [s["id"] for s in self._specs()]
        self.assertTrue(self._push(commands.MoveBlurSpec(ids[0], +1)))
        self.assertEqual([s["id"] for s in self._specs()], [ids[1], ids[0]])
        self.stack.undo(self.timeline)
        self.assertEqual([s["id"] for s in self._specs()], ids)

    # ボカす / ボカさない の入れ替え・名前・追従方法
    def test_mode_label_follow(self):
        self._add_keep_pair()
        area_id = self._specs()[1]["id"]
        self.assertTrue(self._push(commands.SetBlurSpecMode(area_id, blur_decisions.BLUR)))
        self.assertEqual(self._specs()[1]["mode"], blur_decisions.BLUR)
        self.assertTrue(self._push(commands.RenameBlurSpec(area_id, "書類")))
        self.assertEqual(self._specs()[1]["label"], "書類")
        self.assertTrue(self._push(commands.RenameBlurSpec(area_id, "")))
        self.assertNotIn("label", self._specs()[1])
        self.assertTrue(self._push(
            commands.SetBlurSpecFollow(area_id, blur_decisions.FOLLOW_FIXED)))
        self.assertFalse(self._push(
            commands.SetBlurSpecFollow(area_id, blur_decisions.FOLLOW_FIXED)))

    # F6: 追従結果の置き場は相対と絶対の両方を持つこと
    def test_set_cache(self):
        self.assertTrue(self._push(commands.SetBlurCache(
            r"D:\works\sample.timeline.blur.json",
            project_path=r"D:\works\sample.timeline.json")))
        decisions = blur_decisions.load(self.timeline)
        self.assertEqual(decisions["cache"], "sample.timeline.blur.json")
        self.assertTrue(decisions["cache_abs"].endswith("sample.timeline.blur.json"))
        self.assertFalse(self._push(commands.SetBlurCache(
            r"D:\works\sample.timeline.blur.json",
            project_path=r"D:\works\sample.timeline.json")))

    # F7: 何も無い状態で消しても履歴を汚さないこと
    def test_remove_nothing(self):
        self.assertFalse(self._push(commands.RemoveBlurSpecs("b9")))


if __name__ == "__main__":
    unittest.main()
