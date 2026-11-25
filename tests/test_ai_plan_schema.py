# EditPlan / SceneEval / VideoPlan の形の単体テスト (docs/AiEditor/resolve1.md §9 / §20)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・必須キーの欠落と未知の plan_version は AiPlanError (致命)
#   ・列挙値 (§9.4) は既定へ丸めて落とさない
#   ・列挙値が既存の実装 (timeline.model / modules.subtitle_animation) と一致する
#   ・時刻は数値だけを受ける ("12:34" のような表記を作らない / §5.4)
#   ・コメント役割のテロップに動きが付かない (§9.4)
#   ・SE はプリセット名だけを受け、パスを受けない (§9.4 末尾)
# ネットワークもプロバイダーも使わない。
import json
import os
import tempfile
import unittest

from src.ai import plan_schema as ps
from src.ai import profile as ai_profile
from src.exceptions import AiPlanError
from src.modules import subtitle_animation
from src.settings.settings_window import DEFAULT_SETTINGS
from src.timeline import model


def _plan(**overrides):
    plan = ps.new_plan("youtube_shorts", {"kind": "vod", "video_id": "1"},
                       duration_sec=3600.0)
    plan["scenes"] = [{"id": "sc1", "source_start": 10.0, "source_end": 25.0,
                       "role": "hook", "order": 1}]
    plan.update(overrides)

    return plan


class PlanRequiredKeysTest(unittest.TestCase):

    def test_new_plan_normalizes_as_is(self):
        normalized, notes = ps.normalize(_plan())

        self.assertEqual("youtube_shorts", normalized["project"]["target"])
        self.assertEqual("portrait", normalized["project"]["canvas"]["orientation"])
        self.assertEqual([], notes, "既定のままの plan は何も丸めない")

    def test_missing_project_is_fatal(self):
        plan = _plan()
        del plan["project"]

        with self.assertRaises(AiPlanError):
            ps.normalize(plan)

    def test_missing_scenes_is_fatal(self):
        plan = _plan()
        del plan["scenes"]

        with self.assertRaises(AiPlanError):
            ps.normalize(plan)

    def test_all_scenes_unusable_is_fatal(self):
        with self.assertRaises(AiPlanError):
            ps.normalize(_plan(scenes=[{"role": "hook"}]))

    def test_unknown_version_is_fatal(self):
        with self.assertRaises(AiPlanError):
            ps.normalize(_plan(plan_version=2))

        with self.assertRaises(AiPlanError):
            ps.normalize(_plan(plan_version="1"))

    # 呼び出し側が頼んだ target が正。モデルが別の出力先を書いても採用しない。
    def test_requested_target_wins(self):
        plan = _plan()
        plan["project"]["target"] = "youtube_long"

        normalized, notes = ps.normalize(plan, target="youtube_shorts")

        self.assertEqual("youtube_shorts", normalized["project"]["target"])
        self.assertEqual(["project.target"], [n["where"] for n in notes])

    def test_unknown_target_without_a_request_is_fatal(self):
        plan = _plan()
        plan["project"]["target"] = "instagram"

        with self.assertRaises(AiPlanError):
            ps.normalize(plan)


class PlanEnumTest(unittest.TestCase):

    # 列挙値は既存の実装と同じでなければ適用時に落ちる (ai/ は timeline を import しない)。
    def test_animations_match_the_existing_implementation(self):
        self.assertEqual(model.SUBTITLE_ANIMATIONS, ps.ANIMATIONS)
        self.assertEqual(subtitle_animation.ANIMATIONS, ps.ANIMATIONS)

    def test_aligns_match_the_existing_implementation(self):
        self.assertEqual(model.SUBTITLE_ALIGNS, ps.ALIGNS)

    def test_track_roles_map_to_the_model_roles(self):
        self.assertEqual(model.DEFAULT_ROLE, ps.model_track_role("own"))
        self.assertEqual(model.ROLE_COMMENT, ps.model_track_role("comment"))
        self.assertEqual("sub", ps.model_track_role("sub"))
        self.assertEqual(model.DEFAULT_ROLE, ps.model_track_role("bogus"))

    def test_gain_limits_match_the_settings(self):
        sound = DEFAULT_SETTINGS["timeline"]["sound_tracks"]

        self.assertEqual(sound["gain_min_db"], ps.GAIN_MIN_DB)
        self.assertEqual(sound["gain_max_db"], ps.GAIN_MAX_DB)

    def test_unknown_role_falls_back_to_body(self):
        plan = _plan(scenes=[{"id": "sc1", "source_start": 1.0, "source_end": 2.0,
                              "role": "finale"}])

        normalized, notes = ps.normalize(plan)

        self.assertEqual("body", normalized["scenes"][0]["role"])
        self.assertEqual("default", notes[0]["action"])

    def test_retired_animation_becomes_none(self):
        plan = _plan(subtitles=[{"scene_id": "sc1", "source_start": 11.0,
                                 "source_end": 12.0, "text": "a",
                                 "track_role": "own", "animation": "word_pop"}])

        normalized, notes = ps.normalize(plan)

        self.assertEqual("", normalized["subtitles"][0]["animation"])
        self.assertTrue(any(n["where"].endswith("animation") for n in notes))

    # コメント役割はフェードしか効かない (背景の箱とアイコンが追従できない)。
    def test_comment_role_gets_no_animation(self):
        plan = _plan(subtitles=[{"scene_id": "sc1", "source_start": 11.0,
                                 "source_end": 12.0, "text": "a",
                                 "track_role": "comment", "animation": "shake",
                                 "fade_in": True}])

        normalized, _ = ps.normalize(plan)

        self.assertEqual("", normalized["subtitles"][0]["animation"])
        self.assertTrue(normalized["subtitles"][0]["fade_in"], "フェードは残る")

    def test_values_out_of_range_are_clamped(self):
        plan = _plan(scenes=[{"id": "sc1", "source_start": 1.0, "source_end": 2.0,
                              "gain_db": 99.0, "crop": {"mode": "face", "scale": 9.0},
                              "zoom": {"scale": 99.0, "x": -5.0, "y": 0.5}}])

        normalized, _ = ps.normalize(plan)
        scene = normalized["scenes"][0]

        self.assertEqual(ps.GAIN_MAX_DB, scene["gain_db"])
        self.assertEqual(ps.CROP_SCALE_MAX, scene["crop"]["scale"])
        self.assertEqual(ps.ZOOM_MAX, scene["zoom"]["scale"])
        self.assertEqual(-1.0, scene["zoom"]["x"])

    def test_bad_color_is_dropped(self):
        plan = _plan(subtitles=[{"scene_id": "sc1", "source_start": 11.0,
                                 "source_end": 12.0, "text": "a",
                                 "track_role": "own", "color": "red"}])

        normalized, _ = ps.normalize(plan)

        self.assertEqual("", normalized["subtitles"][0]["color"])

    def test_gain_limits_can_be_given(self):
        plan = _plan(scenes=[{"id": "sc1", "source_start": 1.0, "source_end": 2.0,
                              "gain_db": 9.0}])

        normalized, _ = ps.normalize(plan, gain_limits=(-6.0, 6.0))

        self.assertEqual(6.0, normalized["scenes"][0]["gain_db"])


class PlanTimeTest(unittest.TestCase):

    # 時刻は秒 (数値) だけ。"12:34" のような表記はパースの揺れを作るため受けない。
    def test_text_time_is_dropped(self):
        plan = _plan(scenes=[{"id": "sc1", "source_start": "12:34",
                              "source_end": 800.0},
                             {"id": "sc2", "source_start": 10.0, "source_end": 20.0}])

        normalized, notes = ps.normalize(plan)

        self.assertEqual(["sc2"], [s["id"] for s in normalized["scenes"]])
        self.assertEqual("drop", notes[0]["action"])

    def test_times_keep_millisecond_precision(self):
        plan = _plan(scenes=[{"id": "sc1", "source_start": 10.00049,
                              "source_end": 25.6789}])

        normalized, _ = ps.normalize(plan)

        self.assertEqual(10.0, normalized["scenes"][0]["source_start"])
        self.assertEqual(25.679, normalized["scenes"][0]["source_end"])

    def test_scenes_are_sorted_by_order(self):
        plan = _plan(scenes=[
            {"id": "a", "source_start": 1.0, "source_end": 2.0, "order": 3},
            {"id": "b", "source_start": 3.0, "source_end": 4.0, "order": 1},
            {"id": "c", "source_start": 5.0, "source_end": 6.0, "order": 1},
        ])

        normalized, _ = ps.normalize(plan)

        self.assertEqual(["b", "c", "a"], [s["id"] for s in normalized["scenes"]])
        self.assertEqual([1, 2, 3], [s["order"] for s in normalized["scenes"]])

    # 同じ区間を 2 回使う構成 (オープニング → クライマックス) は許す (§8.1)。
    def test_the_same_range_may_be_used_twice(self):
        plan = _plan(scenes=[
            {"id": "op", "source_start": 1262.0, "source_end": 1272.0,
             "role": "hook", "order": 1},
            {"id": "cl", "source_start": 1240.0, "source_end": 1290.0,
             "role": "punchline", "order": 2},
        ])

        normalized, _ = ps.normalize(plan)

        self.assertEqual(2, len(normalized["scenes"]))

    def test_duplicate_scene_ids_are_renamed(self):
        plan = _plan(scenes=[
            {"id": "sc1", "source_start": 1.0, "source_end": 2.0},
            {"id": "sc1", "source_start": 3.0, "source_end": 4.0},
        ])

        normalized, _ = ps.normalize(plan)
        ids = [s["id"] for s in normalized["scenes"]]

        self.assertEqual(len(set(ids)), len(ids))


class PlanReferenceTest(unittest.TestCase):

    def test_subtitle_without_a_scene_is_dropped(self):
        plan = _plan(subtitles=[{"scene_id": "nope", "source_start": 11.0,
                                 "source_end": 12.0, "text": "a"}])

        normalized, notes = ps.normalize(plan)

        self.assertEqual([], normalized["subtitles"])
        self.assertEqual("V11", notes[0]["check"])

    def test_empty_subtitle_text_is_dropped(self):
        plan = _plan(subtitles=[{"scene_id": "sc1", "source_start": 11.0,
                                 "source_end": 12.0, "text": "   "}])

        normalized, _ = ps.normalize(plan)

        self.assertEqual([], normalized["subtitles"])

    # パスを AI に書かせない。プリセット名だけを受ける。
    def test_sound_path_is_rejected(self):
        plan = _plan(sounds=[
            {"scene_id": "sc1", "offset_sec": 0.1, "preset": "C:/windows/clip.wav"},
            {"scene_id": "sc1", "offset_sec": 0.2, "preset": "sounds/a.wav"},
            {"scene_id": "sc1", "offset_sec": 0.3, "preset": "surprise"},
        ])

        normalized, _ = ps.normalize(plan)

        self.assertEqual(["surprise"], [s["preset"] for s in normalized["sounds"]])


class PlanIoTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._dir.cleanup()

    def test_path_is_next_to_the_project(self):
        path = ps.plan_path_for(os.path.join("out", "video.autoedit.json"))

        self.assertTrue(path.endswith(ps.PLAN_SUFFIX))
        self.assertEqual("", ps.plan_path_for(""))

    def test_round_trip(self):
        path = os.path.join(self._dir.name, "video.plan.json")
        normalized, _ = ps.normalize(_plan())

        ps.save(normalized, path)

        self.assertEqual(normalized, ps.load(path))

    def test_future_version_is_not_loaded(self):
        path = os.path.join(self._dir.name, "future.plan.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"plan_version": 99}, handle)

        with self.assertRaises(AiPlanError):
            ps.load(path)

    def test_broken_file_is_not_loaded(self):
        path = os.path.join(self._dir.name, "broken.plan.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{")

        with self.assertRaises(AiPlanError):
            ps.load(path)


class SceneEvalTest(unittest.TestCase):

    def _eval(self, **overrides):
        scene = {"candidate_index": 0, "start": 10.0, "end": 25.0,
                 "scores": {key: 50 for key in ps.SCORE_KEYS},
                 "summary": "s", "reason": "r"}
        scene.update(overrides)

        return {"scenes": [scene]}

    def test_missing_scenes_is_fatal(self):
        with self.assertRaises(AiPlanError):
            ps.normalize_scene_eval({"result": []})

    def test_scores_are_clamped(self):
        payload = self._eval(scores={"funny": 500, "surprise": -20})

        scenes, notes = ps.normalize_scene_eval(payload)
        scores = scenes[0]["scores"]

        self.assertEqual(100.0, scores["funny"])
        self.assertEqual(0.0, scores["surprise"])
        # 欠けた軸は中立。0 にすると 1 軸の欠落で面白い場面が沈む。
        self.assertEqual(float(ps.DEFAULT_SCORE), scores["standalone"])
        self.assertTrue(any(n["action"] == "default" for n in notes))

    def test_out_of_range_candidate_is_dropped(self):
        scenes, notes = ps.normalize_scene_eval(
            self._eval(candidate_index=9), candidate_count=5)

        self.assertEqual([], scenes)
        self.assertEqual("drop", notes[0]["action"])

    def test_duplicate_candidate_is_dropped(self):
        payload = self._eval()
        payload["scenes"].append(dict(payload["scenes"][0]))

        scenes, _ = ps.normalize_scene_eval(payload)

        self.assertEqual(1, len(scenes))

    def test_reason_is_shortened(self):
        scenes, notes = ps.normalize_scene_eval(
            self._eval(reason="あ" * 300), max_reason_chars=120)

        self.assertEqual(120, len(scenes[0]["reason"]))
        self.assertTrue(any(n["check"] == "V4" for n in notes))

    def test_unknown_recommended_use_is_dropped(self):
        scenes, _ = ps.normalize_scene_eval(
            self._eval(recommended_use=["shorts", "reels", "shorts"]))

        self.assertEqual(["shorts"], scenes[0]["recommended_use"])

    def test_needed_context_needs_a_range(self):
        scenes, _ = ps.normalize_scene_eval(
            self._eval(needed_context={"start": 20.0, "end": 10.0, "why": "x"}))

        self.assertIsNone(scenes[0]["needed_context"])

    # 順位は設定の重みで決める (AI に順位そのものを作らせない / §7.3)。
    def test_rank_score_uses_the_weights(self):
        scores = {key: 0.0 for key in ps.SCORE_KEYS}
        scores["funny"] = 80.0
        scores["context_dependency"] = 50.0
        weights = {"funny": 0.3, "context_dependency": -0.2}

        value = ps.rank_score(scores, weights, base_score=10.0, base_weight=0.2)

        self.assertAlmostEqual(80 * 0.3 - 50 * 0.2 + 10 * 0.2, value, places=4)


class VideoPlanTest(unittest.TestCase):

    def _payload(self, **overrides):
        payload = {"target": "youtube_long", "title": "t", "concept": "c",
                   "duration_sec": 480.0,
                   "sections": [{"role": "opening", "scene_ref": 7, "start": 1.0,
                                 "end": 11.0, "reason": "r"}],
                   "direction": {"subtitle": "full", "emphasis_scenes": [7],
                                 "sound_effects": "light", "bgm": "none",
                                 "vertical_crop": False}}
        payload.update(overrides)

        return payload

    def test_normalizes(self):
        plan, notes = ps.normalize_video_plan(self._payload(), "youtube_long")

        self.assertEqual("youtube_long", plan["target"])
        self.assertEqual(1, len(plan["sections"]))
        self.assertEqual([], notes)

    def test_sections_are_required(self):
        with self.assertRaises(AiPlanError):
            ps.normalize_video_plan(self._payload(sections=[]), "youtube_long")

    def test_unknown_direction_values_fall_back(self):
        plan, _ = ps.normalize_video_plan(
            self._payload(direction={"subtitle": "all", "sound_effects": "loud",
                                     "bgm": "jazz"}), "youtube_shorts")

        self.assertEqual("full", plan["direction"]["subtitle"])
        self.assertEqual("light", plan["direction"]["sound_effects"])
        self.assertEqual("none", plan["direction"]["bgm"])
        self.assertTrue(plan["direction"]["vertical_crop"], "縦の出力は既定で縦")


class StructuredOutputSchemaTest(unittest.TestCase):

    # スキーマは定数から組み立てる (§9.4 の列挙値と食い違わない)。
    def test_scene_eval_schema_covers_every_axis(self):
        schema = ps.scene_eval_schema(candidate_count=8)
        item = schema["properties"]["scenes"]["items"]["properties"]

        self.assertEqual(set(ps.SCORE_KEYS), set(item["scores"]["properties"]))
        self.assertEqual("integer", item["candidate_index"]["type"])
        self.assertFalse(schema["additionalProperties"])

    def test_edit_plan_schema_uses_the_enums(self):
        schema = ps.edit_plan_schema("youtube_shorts")
        scene = schema["properties"]["scenes"]["items"]["properties"]
        subtitle = schema["properties"]["subtitles"]["items"]["properties"]

        self.assertEqual(list(ps.SCENE_ROLES), scene["role"]["enum"])
        # target は出力先ごとに絞らない (スキーマもキャッシュの前方一致に入るため)。
        # 呼び出し側の指定が正で、normalize_edit_plan がそれを採用する。
        self.assertEqual(list(ps.TARGETS),
                         schema["properties"]["project"]["properties"]["target"]["enum"])
        self.assertIn("", subtitle["animation"]["enum"])
        self.assertEqual(list(ps.TRACK_ROLES), subtitle["track_role"]["enum"])

    # スキーマは出力先ごとに変えない。スキーマもキャッシュの前方一致に含まれる
    # ため、出力先ごとに列挙を絞ると 3 出力先の呼び出しが別々の entry になり
    # プロンプトキャッシュへ乗らない (2026-11-24 実測)。
    # 役割の並びは planner が payload の section_roles と前置きで伝える
    # (SECTION_ROLE_ORDER の註どおり「検証では強制しない」)。
    def test_plan_schemas_do_not_vary_by_target(self):
        for build in (ps.video_plan_schema, ps.edit_plan_schema):
            rendered = {json.dumps(build(target), sort_keys=True)
                        for target in ps.TARGETS}

            self.assertEqual(1, len(rendered), build.__name__)

    def test_video_plan_schema_allows_every_role(self):
        schema = ps.video_plan_schema("youtube_shorts")
        roles = schema["properties"]["sections"]["items"]["properties"]["role"]["enum"]

        self.assertEqual(list(ps.SCENE_ROLES), roles)

    def test_schemas_are_json(self):
        for schema in (ps.scene_eval_schema(), ps.video_plan_schema("tiktok"),
                       ps.edit_plan_schema("tiktok")):
            json.dumps(schema)

    # 構造化出力が受け付けないキーワードを送ると Anthropic が 400 を返し
    # ("For 'integer' type, properties maximum, minimum are not supported")、
    # AI の呼び出しが丸ごと落ちる。送信直前の sanitize で全部落ちることを見る。
    def test_sanitize_drops_every_unsupported_keyword(self):
        for name, schema in _every_schema():
            cleaned = ps.sanitize_for_api(schema)
            for path, node in _walk(cleaned):
                for key in ps._UNSUPPORTED_SCHEMA_KEYS:
                    self.assertNotIn(key, node, name + " " + path)

    # integer へ minimum / maximum を付けない (sanitize の前から付けない約束)。
    def test_integer_fields_carry_no_bounds(self):
        for name, schema in _every_schema():
            for path, node in _walk(schema):
                if node.get("type") != "integer":
                    continue
                for key in ("minimum", "maximum", "exclusiveMinimum",
                            "exclusiveMaximum"):
                    self.assertNotIn(key, node, name + " " + path)

    # 項目名がキーワードと同じでも消さない (properties の鍵は項目名)。
    def test_sanitize_keeps_properties_named_like_keywords(self):
        cleaned = ps.sanitize_for_api({
            "type": "object", "additionalProperties": False,
            "required": ["maxLength"],
            "properties": {"maxLength": {"type": "integer", "minimum": 0}},
        })

        self.assertIn("maxLength", cleaned["properties"])
        self.assertNotIn("minimum", cleaned["properties"]["maxLength"])
        self.assertEqual(["maxLength"], cleaned["required"])


# 送り出すスキーマの一覧 ((名前, スキーマ) を返す)
def _every_schema():
    schemas = [("scene_eval", ps.scene_eval_schema(candidate_count=8)),
               ("review", ps.review_schema()),
               ("profile", ai_profile._schema(120))]
    for target in ps.TARGETS:
        schemas.append(("video_plan/" + target, ps.video_plan_schema(target)))
        schemas.append(("edit_plan/" + target, ps.edit_plan_schema(target)))

    return schemas


# スキーマの入れ子をすべて辿る ((場所, dict) を返す)
def _walk(node, path="$"):
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, path + "." + str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, path + "[" + str(index) + "]")


if __name__ == "__main__":
    unittest.main()
