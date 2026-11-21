# AI のポイント計測と縮退の単体テスト (docs/AiEditor/resolve1.md §17.3 / §20)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・「計測のみ」(charged = 0) と「課金する」(charged > 0) の**両方で完走する**
#   ・残高不足・上限・未ログインは AI 工程だけを縮退させる (既存の採点結果で続行)
#   ・残高から推測せず、サーバーの応答だけを見る (透かしと同じ作法)
#   ・AI が無効なら provider を作らない (ai/ の他のモジュールへ触らない)
#   ・匿名セッションでは AI を使わない (実費が出る経路を使い捨ての ID で叩かせない)
# ネットワークは使わない。
import unittest

from src.ai.provider import (DEGRADABLE_CODES, AiCompletion, AiRequest, AiUsage,
                             MockProvider, StretheusProvider, create_provider,
                             should_degrade)
from src.ai.usage_log import UsageTracker
from src.exceptions import (AiOfflineError, AiPlanError, AiProviderError,
                            ReauthRequiredError)


# 応答を固定した remote の代わり。
class _FakeRemote:

    def __init__(self, completion=None, error=None):
        self._completion = completion
        self._error = error
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        if self._error is not None:
            raise self._error

        return self._completion

    def count(self, request):
        return None

    def server_config(self):
        if self._error is not None:
            raise self._error

        return None


# ログイン状態をテストから指定できる認証。
class _FakeAuth:

    def __init__(self, session=True, anonymous=False):
        self._session = session
        self._anonymous = anonymous
        self.client = object()

    def ensure_session(self):
        return self._session

    def is_anonymous(self):
        return self._anonymous


def _completion(charged=0, estimated=3):
    return AiCompletion(result={"ok": True}, model="claude-sonnet-5-5",
                        usage=AiUsage(1000, 200, 0, 0),
                        points_charged=charged, points_estimated=estimated)


def _request():
    return AiRequest(role="scene_eval", system="s", payload={"a": 1})


class AiMeteringTest(unittest.TestCase):

    # 当面の既定。実測だけを残し、消費しない。
    def test_measure_only_completes(self):
        tracker = UsageTracker({"ai": {}})
        provider = StretheusProvider(_FakeAuth(), settings={"ai": {"enabled": True}},
                                     tracker=tracker, remote=_FakeRemote(_completion(0, 3)))

        completion = provider.complete_json(_request())

        self.assertEqual({"ok": True}, completion.result)
        self.assertEqual(0, tracker.points_charged)
        self.assertEqual(3, tracker.points_estimated)

    # 単価が決まった後。消費が返ってきても同じ経路で完走する。
    def test_charging_completes_too(self):
        tracker = UsageTracker({"ai": {}})
        provider = StretheusProvider(_FakeAuth(), settings={"ai": {"enabled": True}},
                                     tracker=tracker, remote=_FakeRemote(_completion(5, 5)))

        completion = provider.complete_json(_request())

        self.assertEqual(5, completion.points_charged)
        self.assertEqual(5, tracker.points_charged)

    # 残高不足はサーバーが拒否する。クライアントは理由を出して AI 工程だけ縮退する。
    def test_insufficient_points_degrades_only_the_ai_stage(self):
        error = AiProviderError("ポイントが不足しています。", status=402,
                                code="insufficient_points")
        provider = StretheusProvider(_FakeAuth(), settings={"ai": {"enabled": True}},
                                     remote=_FakeRemote(error=error))

        with self.assertRaises(AiProviderError) as captured:
            provider.complete_json(_request())

        self.assertTrue(should_degrade(captured.exception))

    def test_tracker_guard_runs_before_the_call(self):
        tracker = UsageTracker({"ai": {"cost": {"max_calls_per_project": 1}}})
        remote = _FakeRemote(_completion())
        provider = StretheusProvider(_FakeAuth(), settings={"ai": {"enabled": True}},
                                     tracker=tracker, remote=remote)

        provider.complete_json(_request())

        with self.assertRaises(AiProviderError):
            provider.complete_json(_request())

        self.assertEqual(1, remote.calls, "上限に達したら呼ばない (無駄な往復を減らす)")


class AiSessionTest(unittest.TestCase):

    # 実費が出る経路を、使い捨ての端末 ID で無限に叩けてはならない。
    def test_anonymous_session_is_rejected_before_calling(self):
        remote = _FakeRemote(_completion())
        provider = StretheusProvider(_FakeAuth(anonymous=True),
                                     settings={"ai": {"enabled": True}}, remote=remote)

        with self.assertRaises(AiProviderError) as captured:
            provider.complete_json(_request())

        self.assertEqual("login_required", captured.exception.code)
        self.assertEqual(0, remote.calls)

    def test_without_a_session_it_is_offline(self):
        provider = StretheusProvider(_FakeAuth(session=False),
                                     settings={"ai": {"enabled": True}},
                                     remote=_FakeRemote(_completion()))

        with self.assertRaises(AiOfflineError):
            provider.complete_json(_request())

    def test_without_auth_it_is_offline(self):
        provider = StretheusProvider(None, settings={"ai": {"enabled": True}},
                                     remote=_FakeRemote(_completion()))

        with self.assertRaises(AiOfflineError):
            provider.complete_json(_request())

    # 構成の取得は失敗しても例外にしない (縮退の判断材料でしかない)。
    def test_server_config_failure_returns_none(self):
        provider = StretheusProvider(
            _FakeAuth(), settings={"ai": {"enabled": True}},
            remote=_FakeRemote(error=AiOfflineError("接続できません")))

        self.assertIsNone(provider.server_config())


class AiProviderFactoryTest(unittest.TestCase):

    # ai.enabled = false のときは作らない。呼び出し側は None を「飛ばす」と読む。
    def test_disabled_returns_none(self):
        self.assertIsNone(create_provider({"ai": {"enabled": False}}, auth=_FakeAuth()))
        self.assertIsNone(create_provider({}, auth=_FakeAuth()))

    def test_enabled_returns_the_stretheus_provider(self):
        provider = create_provider({"ai": {"enabled": True}}, auth=_FakeAuth())

        self.assertEqual("stretheus", provider.name)

    # 開発用の直接呼び出しは今は使えない (サーバー経由へ寄せた / §6.5)。
    def test_direct_mode_fails_clearly(self):
        provider = create_provider({"ai": {"enabled": True, "direct_mode": True}},
                                   auth=_FakeAuth())

        self.assertEqual("direct", provider.name)
        with self.assertRaises(AiOfflineError):
            provider.complete_json(_request())


class AiDegradeDecisionTest(unittest.TestCase):

    def test_offline_and_plan_errors_degrade(self):
        self.assertTrue(should_degrade(AiOfflineError("接続できません")))
        self.assertTrue(should_degrade(AiPlanError("JSON ではありません")))

    def test_degradable_codes(self):
        for code in sorted(DEGRADABLE_CODES):
            self.assertTrue(should_degrade(AiProviderError("x", code=code)), code)

    # 入力が大きすぎる・モデルが許可されていないのは呼び出し側が組み直せる。
    def test_recoverable_codes_do_not_degrade(self):
        for code in ("ai_payload_too_large", "validation_failed"):
            self.assertFalse(should_degrade(AiProviderError("x", code=code)), code)

    # 再ログインは AI の判断で縮退させない (既存の経路が扱う)。
    def test_reauth_is_not_an_ai_decision(self):
        self.assertFalse(should_degrade(ReauthRequiredError("ログインが必要です")))


class AiMockProviderTest(unittest.TestCase):

    # テストは固定 JSON で検証し、プロバイダーへ一切繋がない (§20)。
    def test_mock_returns_fixed_results(self):
        provider = MockProvider(results={"scene_eval": {"scores": [1]}})

        completion = provider.complete_json(_request())

        self.assertEqual({"scores": [1]}, completion.result)
        self.assertEqual("mock", completion.model)
        self.assertEqual(1, len(provider.requests))


if __name__ == "__main__":
    unittest.main()
