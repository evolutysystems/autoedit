# ai セクションの設定の単体テスト (docs/AiEditor/resolve1.md §16 / §20)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・既定値の出どころは DEFAULT_SETTINGS["ai"] 1 か所であり、setting.json と
#     ai/config.DEFAULT_AI が食い違わない (3 か所に散らない)
#   ・_merge_with_defaults はセクション単位の浅いマージなので、入れ子の欠落キーは
#     ai/config.py が既定へ落とす
#   ・壊れた値 (型違い・負数) でも落とさず既定へ寄せる
#   ・鍵を置く欄が無い (配布物へ鍵を焼かない / §6.1)
#   ・ai/ は gui/ を import しない (Qt 非依存 / §4)
# 外部ネットワークは使わない。
import json
import os
import unittest

from src.ai import config as ai_config
from src.settings.settings_window import DEFAULT_SETTINGS

_SETTING_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "settings", "setting.json")


class AiConfigDefaultsTest(unittest.TestCase):

    def test_default_settings_has_ai_section(self):
        self.assertIn("ai", DEFAULT_SETTINGS)
        self.assertFalse(DEFAULT_SETTINGS["ai"]["enabled"], "既定はオフであること")

    # 既定値が 2 か所でずれると「直したのに変わらない」が起きる (test_api_base_url と同じ趣旨)。
    def test_module_defaults_match_default_settings(self):
        self.assertEqual(ai_config.DEFAULT_AI, DEFAULT_SETTINGS["ai"])

    def test_setting_json_matches_default_settings(self):
        with open(_SETTING_JSON, encoding="utf-8") as handle:
            saved = json.load(handle)

        self.assertIn("ai", saved, "setting.json に ai セクションが無いと保存で消える")
        self.assertEqual(saved["ai"], DEFAULT_SETTINGS["ai"])

    # 鍵は setting.json にも DEFAULT_SETTINGS にも置かない (§6.1 / §6.5)。
    def test_no_key_field(self):
        keys = _all_keys(DEFAULT_SETTINGS["ai"])
        for key in keys:
            lowered = key.lower()
            for forbidden in ("key", "secret", "password", "credential"):
                self.assertNotIn(forbidden, lowered, "鍵を入れる欄を作らない: " + key)

        # 値の側にも鍵らしい文字列を置かない。
        flat = json.dumps(DEFAULT_SETTINGS["ai"], ensure_ascii=False)
        self.assertNotIn("sk-", flat)


# 入れ子を含むすべてのキー名を集める。
def _all_keys(value, found=None):
    found = found if found is not None else []
    if isinstance(value, dict):
        for key, child in value.items():
            found.append(str(key))
            _all_keys(child, found)

    return found


class AiConfigFillTest(unittest.TestCase):

    def test_missing_section_falls_back_to_defaults(self):
        config = ai_config.ai_config({})

        self.assertEqual(config, ai_config.DEFAULT_AI)
        self.assertFalse(ai_config.is_enabled({}))
        self.assertFalse(ai_config.is_enabled(None))

    # 浅いマージを通った後の状態 (入れ子が欠けている) でも既定へ落ちる。
    def test_nested_keys_are_filled(self):
        config = ai_config.ai_config({"ai": {"enabled": True, "endpoint": {}}})

        self.assertTrue(config["enabled"])
        self.assertEqual("/api/ai/complete", ai_config.endpoint_path(config, "complete"))
        self.assertEqual("/api/ai/jobs", ai_config.endpoint_path(config, "jobs"))
        self.assertEqual(8192, ai_config.max_output_tokens(config))
        self.assertEqual(0.30, config["scene_eval"]["weights"]["long"]["funny"])

    def test_broken_values_are_coerced(self):
        config = ai_config.ai_config({"ai": {
            "enabled": "yes",                      # bool でない
            "max_output_tokens": "many",           # int でない
            "timeout_sec": -5,                     # 負数
            "retry": {"max_attempts": 0, "backoff_sec": "slow"},
            "jobs": {"wait_sec": None, "poll_min_ms": -1},
            "endpoint": {"complete": "   "},       # 空白のみ
            "cost": {"max_calls_per_project": -3},
        }})

        self.assertFalse(config["enabled"], "bool でない値は既定へ寄せる")
        self.assertEqual(8192, ai_config.max_output_tokens(config))
        self.assertEqual(180.0, ai_config.timeout_sec(config))
        self.assertEqual((3, 2.0), ai_config.retry_policy(config))
        self.assertEqual((900.0, 1.0), ai_config.job_policy(config))
        self.assertEqual("/api/ai/complete", ai_config.endpoint_path(config, "complete"))
        # 負数は既定へ寄る (既定は 60 / docs/error/20261125c/resolve.md §2.6)
        self.assertEqual((60, 300000), ai_config.cost_limits(config))

    # 将来キーを足したときに、古いクライアントが保存で消さない。
    def test_unknown_keys_are_kept(self):
        config = ai_config.ai_config({"ai": {"future_key": {"a": 1}}})

        self.assertEqual({"a": 1}, config["future_key"])


class AiConfigHintTest(unittest.TestCase):

    def test_model_and_effort_hints_per_role(self):
        config = ai_config.ai_config({"ai": {"enabled": True}})

        self.assertEqual("claude-sonnet-5-5", ai_config.model_hint(config, "scene_eval"))
        self.assertEqual("medium", ai_config.effort_hint(config, "scene_eval"))
        self.assertEqual("claude-opus-5-5", ai_config.model_hint(config, "plan"))
        self.assertEqual("high", ai_config.effort_hint(config, "plan"))

    # review_critic は役割 review のまま、より強いモデルを希望する (§6.3)。
    def test_derived_model_key_falls_back_to_the_base_role(self):
        config = ai_config.ai_config({"ai": {"enabled": True}})

        self.assertEqual("claude-opus-5-5", ai_config.model_hint(config, "review_critic"))
        self.assertEqual("review", ai_config.role_of("review_critic"))

        # effort は派生キーを持たないため、素の役割の値へ落ちる。
        self.assertEqual("medium", ai_config.effort_hint(config, "review_critic"))

    # 許可していない effort はサーバーの役割既定へ委ねる (空を送る)。
    def test_unknown_effort_is_dropped(self):
        config = ai_config.ai_config({"ai": {"effort": {"plan": "turbo"}}})

        self.assertEqual("", ai_config.effort_hint(config, "plan"))

    def test_empty_model_hint_is_left_to_the_server(self):
        config = ai_config.ai_config({"ai": {"models": {"plan": "", "review": ""}}})

        self.assertEqual("", ai_config.model_hint(config, "plan"))


class AiLayerDependencyTest(unittest.TestCase):

    # ai/ は Qt に依存しない (CLI とテストから同じ判断を再現できる / §4)。
    def test_ai_modules_do_not_import_gui(self):
        root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "ai")

        for name in os.listdir(root):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                source = handle.read()

            self.assertNotIn("from ..gui", source, name + " が gui を import している")
            self.assertNotIn("import PySide6", source, name + " が Qt を import している")


if __name__ == "__main__":
    unittest.main()
