# AI 版と人間の修正版の差分の単体テスト (docs/AiEditor/resolve1.md §14 / §20)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・§14.2 の各分類が検出される
#   ・**VOD 時間へ戻してから**比べる (1 か所のカットで以降が全部「変更された」にならない)
#   ・許容 (match_tolerance_sec) 未満のぶれは差分にしない
#   ・1 回しか現れない種類は学習へ取り込まない (§14.4)
#   ・検証で落とした区間を人間が入れ直した分は取り込まない (§14.4)
#   ・`context` を必ず付ける (§14.3)
#   ・学習が無効・対象外のプロジェクトでは何もしない
import os
import tempfile
import unittest

from src.ai import plan_diff

_SETTINGS = {"ai": {"learning": {"enabled": True, "match_tolerance_sec": 0.3,
                                 "keep_days": 180, "exclude": []}}}


# 素材 m1 は VOD 100 秒から始まる。素材内の秒 + 100 = VOD の秒。
def _project(clips=None, subtitles=None, sounds=None, audio=None, vod_start=100.0):
    return {
        "schema_version": 1,
        "updated_at": "2026-11-20T10:00:00",
        "timeline": {"fps": 60, "duration_sec": 40.0},
        "source": {"input_path": "vod.mp4",
                   "archive": {"vod_path": "vod.mp4",
                               "clips": [{"index": 1, "vod_start": vod_start,
                                          "vod_end": vod_start + 30.0,
                                          "media_id": "m1"}],
                               "media": [{"media_id": "m1", "vod_start": vod_start,
                                          "vod_end": vod_start + 30.0}]}},
        "tracks": [
            {"id": "V1", "kind": "video", "clips": clips if clips is not None else [
                {"id": "c1", "media_id": "m1", "timeline_start": 0.0,
                 "duration": 10.0, "source_in": 0.0, "source_out": 10.0,
                 "transform": {"x": 0.0, "y": 0.0, "scale": 1.0}}]},
            {"id": "A1", "kind": "audio", "clips": audio if audio is not None else [
                {"id": "a1", "link_clip": "c1", "gain_db": 0.0, "muted": False}]},
            {"id": "S1", "kind": "subtitle",
             "clips": subtitles if subtitles is not None else []},
            {"id": "SE1", "kind": "sound", "clips": sounds or []},
        ],
    }


def _clip(clip_id="c1", start=0.0, duration=10.0, source_in=0.0, scale=1.0):
    return {"id": clip_id, "media_id": "m1", "timeline_start": start,
            "duration": duration, "source_in": source_in,
            "source_out": source_in + duration,
            "transform": {"x": 0.0, "y": 0.0, "scale": scale}}


def _subtitle(clip_id="s1", start=1.0, duration=2.0, text="やばい", animation="",
              color="", font_size=None, align="", x=None):
    return {"id": clip_id, "timeline_start": start, "duration": duration,
            "text": text, "role": "streamer", "animation": animation,
            "color": color, "outline_color": "", "align": align,
            "font_size": font_size, "transform": {"x": x, "y": None}}


def _sound(clip_id="se1", start=3.0, duration=1.0, gain=0.0):
    return {"id": clip_id, "media_id": "m2", "timeline_start": start,
            "duration": duration, "source_in": 0.0, "source_out": duration,
            "gain_db": gain, "muted": False}


def _kinds(rows):
    return sorted({row["kind"] for row in rows})


def _diff(before, after, **kwargs):
    # 1 回しか現れない種類も見たいので、既定では落とさない
    kwargs.setdefault("drop_singletons", False)

    return plan_diff.diff(before, after, _SETTINGS, **kwargs)


class SceneDiffTest(unittest.TestCase):

    def test_cut_shorten(self):
        before = _project(clips=[_clip(duration=10.0)])
        after = _project(clips=[_clip(duration=7.0, source_in=3.0)])

        rows = _diff(before, after)

        self.assertEqual([plan_diff.KIND_CUT_SHORTEN], _kinds(rows))
        self.assertEqual(3.0, rows[0]["delta"]["head_sec"])
        self.assertEqual(100.0, rows[0]["ai"]["head"])
        self.assertEqual(103.0, rows[0]["human"]["head"])

    def test_cut_extend(self):
        before = _project(clips=[_clip(duration=7.0, source_in=3.0)])
        after = _project(clips=[_clip(duration=10.0, source_in=0.0)])

        rows = _diff(before, after)

        self.assertEqual([plan_diff.KIND_CUT_EXTEND], _kinds(rows))

    # 許容未満のぶれは差分にしない (操作のぶれ / §14.4)。
    def test_small_delta_is_not_a_diff(self):
        before = _project(clips=[_clip(duration=10.0)])
        after = _project(clips=[_clip(duration=9.9, source_in=0.1)])

        self.assertEqual([], _diff(before, after))

    def test_scene_drop(self):
        before = _project(clips=[_clip("c1"), _clip("c2", start=10.0, source_in=15.0)])
        after = _project(clips=[_clip("c1")])

        rows = _diff(before, after)

        self.assertEqual([plan_diff.KIND_SCENE_DROP], _kinds(rows))
        self.assertEqual(115.0, rows[0]["vod_range"]["start"])

    def test_scene_add(self):
        before = _project(clips=[_clip("c1")])
        after = _project(clips=[_clip("c1"), _clip("c2", start=10.0, source_in=15.0)])

        rows = _diff(before, after)

        self.assertEqual([plan_diff.KIND_SCENE_ADD], _kinds(rows))

    # 並べ替えは VOD 時間で突き合わせる (Timeline 時間では全部変更に見える)。
    def test_reorder(self):
        before = _project(clips=[_clip("c1", start=0.0, source_in=0.0),
                                 _clip("c2", start=10.0, source_in=15.0)])
        after = _project(clips=[_clip("c2", start=0.0, source_in=15.0),
                                _clip("c1", start=10.0, source_in=0.0)])

        rows = _diff(before, after)

        self.assertEqual([plan_diff.KIND_REORDER], _kinds(rows))

    # 1 か所のカットで以降が全部「変更」にならない。
    def test_a_single_cut_does_not_mark_everything(self):
        before = _project(clips=[_clip("c1", start=0.0, source_in=0.0, duration=10.0),
                                 _clip("c2", start=10.0, source_in=15.0,
                                       duration=10.0)])
        # 先頭を 3 秒詰めた結果、2 本目の Timeline 位置は 7 秒へずれる
        after = _project(clips=[_clip("c1", start=0.0, source_in=3.0, duration=7.0),
                                _clip("c2", start=7.0, source_in=15.0,
                                      duration=10.0)])

        rows = _diff(before, after)

        self.assertEqual([plan_diff.KIND_CUT_SHORTEN], _kinds(rows))
        self.assertEqual(1, len(rows))


class SubtitleDiffTest(unittest.TestCase):

    def test_text_change(self):
        before = _project(subtitles=[_subtitle(text="やばい")])
        after = _project(subtitles=[_subtitle(text="やばすぎ")])

        rows = _diff(before, after)

        self.assertEqual([plan_diff.KIND_SUBTITLE_TEXT], _kinds(rows))
        self.assertEqual(1, rows[0]["delta"]["chars"], "やばい(3) → やばすぎ(4)")

    def test_style_change(self):
        before = _project(subtitles=[_subtitle(color="")])
        after = _project(subtitles=[_subtitle(color="#FF0000")])

        self.assertEqual([plan_diff.KIND_SUBTITLE_STYLE], _kinds(_diff(before, after)))

    def test_move_change(self):
        before = _project(subtitles=[_subtitle(font_size=None)])
        after = _project(subtitles=[_subtitle(font_size=80)])

        rows = _diff(before, after)

        self.assertEqual([plan_diff.KIND_SUBTITLE_MOVE], _kinds(rows))
        self.assertEqual(80, rows[0]["delta"]["font_size"])

    def test_animation_add_remove_change(self):
        plain = _project(subtitles=[_subtitle(animation="")])
        shaken = _project(subtitles=[_subtitle(animation="shake")])
        tsukkomi = _project(subtitles=[_subtitle(animation="tsukkomi")])

        self.assertEqual([plan_diff.KIND_ANIMATION_ADD],
                         _kinds(_diff(plain, shaken)))
        self.assertEqual([plan_diff.KIND_ANIMATION_REMOVE],
                         _kinds(_diff(shaken, plain)))
        self.assertEqual([plan_diff.KIND_ANIMATION_CHANGE],
                         _kinds(_diff(shaken, tsukkomi)))

    # 人間が足した強調は拾う (§14.2 の emphasis_add)。
    def test_emphasis_added_by_the_human(self):
        before = _project(subtitles=[])
        after = _project(subtitles=[_subtitle(start=5.0, animation="shock1")])

        self.assertEqual([plan_diff.KIND_EMPHASIS_ADD], _kinds(_diff(before, after)))

    def test_plain_added_subtitle_is_not_emphasis(self):
        before = _project(subtitles=[])
        after = _project(subtitles=[_subtitle(start=5.0)])

        self.assertEqual([], _diff(before, after))


class SoundDiffTest(unittest.TestCase):

    def test_se_add_remove_gain(self):
        none = _project(sounds=[])
        one = _project(sounds=[_sound()])
        loud = _project(sounds=[_sound(gain=-2.0)])

        self.assertEqual([plan_diff.KIND_SE_ADD], _kinds(_diff(none, one)))
        self.assertEqual([plan_diff.KIND_SE_REMOVE], _kinds(_diff(one, none)))
        self.assertEqual([plan_diff.KIND_SE_GAIN], _kinds(_diff(one, loud)))

    def test_audio_gain(self):
        before = _project()
        after = _project(audio=[{"id": "a1", "link_clip": "c1", "gain_db": -6.0,
                                 "muted": False}])

        rows = _diff(before, after)

        self.assertEqual([plan_diff.KIND_AUDIO_GAIN], _kinds(rows))
        self.assertEqual(-6.0, rows[0]["delta"]["gain_db"])

    def test_mute_is_a_diff(self):
        before = _project()
        after = _project(audio=[{"id": "a1", "link_clip": "c1", "gain_db": 0.0,
                                 "muted": True}])

        self.assertEqual([plan_diff.KIND_AUDIO_GAIN], _kinds(_diff(before, after)))


class ContextTest(unittest.TestCase):

    class _Bundle:
        transcript = [{"start": 101.0, "end": 104.0, "text": "そんなわけないでしょ"}]
        comments = [{"offset_sec": 102.0, "user": "u", "text": "ｗｗｗ"}]
        cells = [{"start": 100.0, "end": 110.0, "max_db": -8.1, "mean_db": -20.0}]

    # context を必ず付ける (§14.3)。
    def test_context_has_the_situation(self):
        before = _project(clips=[_clip(duration=10.0)])
        after = _project(clips=[_clip(duration=7.0, source_in=3.0)])
        plan = {"scenes": [{"id": "sc1", "source_start": 100.0, "source_end": 110.0,
                            "role": "hook"}]}

        rows = _diff(before, after, plan=plan, bundle=self._Bundle())
        context = rows[0]["context"]

        self.assertEqual("sc1", rows[0]["scene_id"])
        self.assertEqual("hook", context["role"])
        self.assertIn("そんなわけ", context["transcript"])
        self.assertEqual(-8.1, context["max_db"])
        self.assertTrue(context["comment_rate"] > 0)

    def test_hypothesis_is_empty_until_the_ai_fills_it(self):
        before = _project(clips=[_clip(duration=10.0)])
        after = _project(clips=[_clip(duration=7.0, source_in=3.0)])

        rows = _diff(before, after)

        self.assertIsNone(rows[0]["hypothesis"])
        self.assertEqual("", rows[0]["user_note"])


class ExclusionTest(unittest.TestCase):

    # 1 回しか現れない種類は学習へ取り込まない (§14.4)。
    def test_singletons_are_dropped(self):
        before = _project(clips=[_clip("c1"), _clip("c2", start=10.0, source_in=15.0)],
                          subtitles=[_subtitle(text="やばい")])
        after = _project(clips=[_clip("c1", duration=7.0, source_in=3.0),
                                _clip("c2", start=7.0, source_in=18.0,
                                      duration=7.0)],
                         subtitles=[_subtitle(text="やばすぎ")])

        rows = plan_diff.diff(before, after, _SETTINGS)

        self.assertEqual([plan_diff.KIND_CUT_SHORTEN], _kinds(rows))
        self.assertEqual(2, len(rows), "2 回出た種類だけ残る")

    # 検証で落とした区間を人間が入れ直した分は取り込まない (§14.4)。
    def test_ignored_ranges_are_skipped(self):
        before = _project(clips=[_clip("c1")])
        after = _project(clips=[_clip("c1"), _clip("c2", start=10.0, source_in=15.0)])

        rows = _diff(before, after, ignore_ranges=[(114.0, 126.0)])

        self.assertEqual([], rows)

    def test_excluded_project(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = {"ai": {"learning": {"enabled": True, "exclude": [folder]}}}
            path = os.path.join(folder, "a.timeline.json")

            self.assertTrue(plan_diff.is_excluded(path, settings))
            self.assertFalse(plan_diff.is_excluded(path, _SETTINGS))

    def test_missing_project_is_no_diff(self):
        self.assertEqual([], plan_diff.diff("nope.json", "nope2.json", _SETTINGS))


class RecordTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._dir.name, "store")

    def tearDown(self):
        self._dir.cleanup()

    def _write(self, name, data):
        import json

        path = os.path.join(self._dir.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

        return path

    def test_records_and_saves(self):
        project = self._write("v.timeline.json", _project(
            clips=[_clip("c1", duration=7.0, source_in=3.0),
                   _clip("c2", start=7.0, source_in=18.0, duration=7.0)]))
        self._write("v.timeline.ai-base.json", _project(
            clips=[_clip("c1"), _clip("c2", start=10.0, source_in=15.0)]))

        rows, path = plan_diff.record_for_project(project, _SETTINGS, root=self.root)

        self.assertEqual(2, len(rows))
        self.assertTrue(path.endswith(".json"))
        self.assertTrue(os.path.exists(path))

    def test_without_a_snapshot_nothing_happens(self):
        project = self._write("v.timeline.json", _project())

        self.assertEqual(([], ""), plan_diff.record_for_project(
            project, _SETTINGS, root=self.root))

    def test_disabled_learning_does_nothing(self):
        project = self._write("v.timeline.json", _project())
        self._write("v.timeline.ai-base.json", _project())

        self.assertEqual(([], ""), plan_diff.record_for_project(
            project, {"ai": {"learning": {"enabled": False}}}, root=self.root))


if __name__ == "__main__":
    unittest.main()
