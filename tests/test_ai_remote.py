# Stretheus API 経由の AI 呼び出しの単体テスト (docs/AiEditor/resolve1.md §6.2 / §20)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・要求本文の形 (role / schema / cache_prefix_id / 冪等キー / 希望モデル)
#   ・列挙はサーバー側が camelCase (scene_eval → sceneEval / endTurn → end_turn)
#   ・ApiOfflineError → AiOfflineError (縮退の判断材料)
#   ・stop_reason = refusal は再試行しない
#   ・同期で終わらない処理は 202 + jobId を受け、ポーリングで受け取る (追補 A2)
#   ・ai_invalid_output は 1 回だけ作り直す (追補 A3)
#   ・再送で二重に実費を払わない (到達不能は同じ冪等キー / 混雑は新しい冪等キー)
# ネットワークは使わない。API クライアントと待ち時間を差し替える。
import unittest

from src.ai import config as ai_config
from src.ai.provider import AiImage, AiRequest
from src.ai.remote import RemoteAiClient
from src.exceptions import (AiOfflineError, AiPlanError, AiProviderError, ApiError,
                            ApiOfflineError, ReauthRequiredError)

_COMPLETION = {
    "result": {"scores": [1, 2, 3]},
    "model": "claude-sonnet-5-5",
    "usage": {"inputTokens": 41203, "outputTokens": 2187,
              "cacheCreationInputTokens": 0, "cacheReadInputTokens": 38950},
    "points": {"charged": 0, "estimated": 3},
    "stopReason": "endTurn",
    "durationMs": 18432,
}


# 応答をテストから指定できる API クライアント。
# 1 要素が 1 回の呼び出しに対応し、例外を入れるとその回で投げる。
class _FakeClient:

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def post(self, path, body=None, authenticated=True):
        return self._next("POST", path, body)

    def get(self, path, authenticated=True):
        return self._next("GET", path, None)

    def _next(self, method, path, body):
        self.calls.append((method, path, body))
        if not self.responses:
            raise AssertionError("想定より多く呼ばれた: " + path)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response

        return response

    # 直近の POST 本文
    def last_body(self):
        for method, _path, body in reversed(self.calls):
            if method == "POST":
                return body

        return None

    def bodies(self):
        return [body for method, _path, body in self.calls if method == "POST"]


class _Recorder:

    def __init__(self):
        self.slept = []
        self.now = 0.0

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += float(seconds)

    def monotonic(self):
        return self.now


def _config(**overrides):
    settings = {"ai": {"enabled": True}}
    settings["ai"].update(overrides)

    return ai_config.ai_config(settings)


def _client(responses, config=None, request_ids=None):
    fake = _FakeClient(responses)
    recorder = _Recorder()
    ids = iter(request_ids or ["id-1", "id-2", "id-3", "id-4"])
    remote = RemoteAiClient(fake, config or _config(),
                            sleep=recorder.sleep, monotonic=recorder.monotonic,
                            request_ids=lambda: next(ids))

    return remote, fake, recorder


def _request(**overrides):
    values = {
        "role": "scene_eval",
        "system": "judge the scenes",
        "payload": {"candidates": [1, 2, 3]},
    }
    values.update(overrides)

    return AiRequest(**values)


class AiRemoteBodyTest(unittest.TestCase):

    def test_body_follows_the_contract(self):
        remote, fake, _ = _client([_COMPLETION])

        remote.complete(_request(schema={"type": "object"},
                                 cache_prefix_id="vod-1-bundle-v1",
                                 project_id="11111111-1111-1111-1111-111111111111"))

        body = fake.last_body()
        self.assertEqual("id-1", body["requestId"])
        self.assertEqual("sceneEval", body["role"], "列挙は camelCase で送る")
        self.assertEqual("judge the scenes", body["system"])
        self.assertEqual({"candidates": [1, 2, 3]}, body["payload"])
        self.assertEqual({"type": "object"}, body["schema"])
        self.assertEqual("vod-1-bundle-v1", body["cachePrefixId"])
        self.assertEqual("11111111-1111-1111-1111-111111111111", body["projectId"])
        self.assertEqual("claude-sonnet-5-5", body["modelHint"])
        self.assertEqual("medium", body["effortHint"])
        self.assertEqual(8192, body["maxOutputTokens"])

        # 鍵・端末名などは送らない。
        self.assertNotIn("apiKey", body)

    def test_images_are_sent_as_base64(self):
        remote, fake, _ = _client([_COMPLETION])

        remote.complete(_request(images=[AiImage("image/jpeg", "AAAA")]))

        self.assertEqual([{"mediaType": "image/jpeg", "data": "AAAA"}],
                         fake.last_body()["images"])

    def test_derived_model_key_sends_the_base_role(self):
        remote, fake, _ = _client([_COMPLETION])

        remote.complete(_request(role="review", model_key="review_critic"))

        body = fake.last_body()
        self.assertEqual("review", body["role"])
        self.assertEqual("claude-opus-5-5", body["modelHint"], "critic は強いモデルを希望する")

    def test_unknown_role_is_rejected_before_sending(self):
        remote, fake, _ = _client([])

        with self.assertRaises(AiPlanError):
            remote.complete(_request(role="unknown"))

        self.assertEqual([], fake.calls, "送る前に落とす")

    def test_empty_model_hint_is_not_sent(self):
        config = _config(models={"scene_eval": ""})
        remote, fake, _ = _client([_COMPLETION], config=config)

        remote.complete(_request())

        self.assertNotIn("modelHint", fake.last_body(), "希望が無ければサーバーの既定に任せる")


class AiRemoteResponseTest(unittest.TestCase):

    def test_completion_is_parsed(self):
        remote, _, _ = _client([_COMPLETION])

        completion = remote.complete(_request())

        self.assertEqual({"scores": [1, 2, 3]}, completion.result)
        self.assertEqual("claude-sonnet-5-5", completion.model)
        self.assertEqual(41203, completion.usage.input_tokens)
        self.assertEqual(2187, completion.usage.output_tokens)
        self.assertEqual(38950, completion.usage.cache_read_input_tokens)
        self.assertEqual("end_turn", completion.stop_reason, "線の上は camelCase")
        self.assertEqual(0, completion.points_charged)
        self.assertEqual(3, completion.points_estimated)
        self.assertEqual(18432, completion.duration_ms)

        # usage の 4 項目は加算関係にあり、引き算をしない。
        self.assertEqual(41203 + 38950, completion.usage.prompt_tokens)

    def test_refusal_is_not_retried(self):
        response = dict(_COMPLETION)
        response["stopReason"] = "refusal"
        remote, fake, _ = _client([response])

        with self.assertRaises(AiPlanError):
            remote.complete(_request())

        self.assertEqual(1, len(fake.calls), "安全側の拒否を再試行で押し通さない")

    def test_missing_result_is_a_plan_error(self):
        remote, _, _ = _client([{"model": "claude-sonnet-5-5"}])

        with self.assertRaises(AiPlanError):
            remote.complete(_request())

    def test_count_and_config_are_parsed(self):
        remote, _, _ = _client([
            {"inputTokens": 50000, "estimatedOutputTokens": 2500,
             "estimatedPoints": 103, "estimatedUsd": 1.025, "model": "claude-sonnet-5-5"},
            {"enabled": True, "chargeMode": "measure", "requireLogin": True,
             "roles": {"sceneEval": {"model": "claude-sonnet-5-5"}},
             "allowedModels": ["claude-sonnet-5-5"],
             "limits": {"maxCallsPerProject": 40},
             "pricing": {"usdPerPoint": 0.01, "minChargePoints": 1}},
        ])

        estimate = remote.count(_request())
        self.assertEqual(50000, estimate.input_tokens)
        self.assertEqual(103, estimate.estimated_points)

        config = remote.server_config()
        self.assertTrue(config.enabled)
        self.assertFalse(config.charging, "measure では課金しない")
        self.assertEqual(["claude-sonnet-5-5"], config.allowed_models)
        self.assertEqual(40, config.limits["maxCallsPerProject"])


class AiRemoteJobTest(unittest.TestCase):

    # 追補 A2: 同期で終わらない処理は 202 で受理され、ポーリングで受け取る。
    def test_accepted_job_is_polled_until_it_succeeds(self):
        accepted = {"jobId": "job-1", "status": "running",
                    "pollAfterMs": 3000, "expiresAt": "2026-11-20T12:10:00Z"}
        running = {"jobId": "job-1", "status": "running", "pollAfterMs": 3000}
        done = dict(_COMPLETION)
        done.update({"jobId": "job-1", "status": "succeeded", "pollAfterMs": 0})

        remote, fake, recorder = _client([accepted, running, done])

        completion = remote.complete(_request())

        self.assertEqual({"scores": [1, 2, 3]}, completion.result)
        self.assertEqual("job-1", completion.job_id)
        self.assertEqual([3.0, 3.0], recorder.slept, "pollAfterMs を守る")
        self.assertEqual(["POST", "GET", "GET"], [method for method, _p, _b in fake.calls])
        self.assertEqual("/api/ai/jobs/job-1", fake.calls[1][1])

    def test_failed_job_raises_the_mapped_error(self):
        accepted = {"jobId": "job-2", "status": "running", "pollAfterMs": 1000}
        failed = {"jobId": "job-2", "status": "failed", "pollAfterMs": 0,
                  "error": {"code": "ai_busy", "message": "中断されました。"}}

        # 再送を 1 回に抑え、エラーコードの写し取りだけを見る。
        remote, _, _ = _client([accepted, failed],
                               config=_config(retry={"max_attempts": 1, "backoff_sec": 1.0}))

        with self.assertRaises(AiProviderError) as captured:
            remote.complete(_request())

        self.assertEqual("ai_busy", captured.exception.code)

    # 中断 (ai_busy) は待って作り直す。処理そのものは失われている。
    def test_interrupted_job_is_resent_with_a_new_request_id(self):
        accepted = {"jobId": "job-7", "status": "running", "pollAfterMs": 1000}
        failed = {"jobId": "job-7", "status": "failed", "pollAfterMs": 0,
                  "error": {"code": "ai_busy", "message": "中断されました。"}}
        remote, fake, _ = _client([accepted, failed, _COMPLETION])

        completion = remote.complete(_request())

        self.assertEqual({"scores": [1, 2, 3]}, completion.result)
        self.assertEqual(["id-1", "id-2"], [body["requestId"] for body in fake.bodies()])

    def test_refused_job_is_a_plan_error(self):
        accepted = {"jobId": "job-3", "status": "pending", "pollAfterMs": 1000}
        refused = {"jobId": "job-3", "status": "refused", "pollAfterMs": 0,
                   "error": {"code": "ai_refused", "message": "応答を控えました。"}}
        remote, _, _ = _client([accepted, refused])

        with self.assertRaises(AiPlanError):
            remote.complete(_request())

    # 結果の保持期限が過ぎたジョブ (succeeded だが本文が無い)。
    def test_expired_result_raises(self):
        accepted = {"jobId": "job-4", "status": "running", "pollAfterMs": 1000}
        expired = {"jobId": "job-4", "status": "succeeded", "pollAfterMs": 0,
                   "error": {"code": "ai_result_expired", "message": "期限切れです。"}}
        remote, _, _ = _client([accepted, expired],
                               config=_config(retry={"max_attempts": 1, "backoff_sec": 1.0}))

        with self.assertRaises(AiProviderError) as captured:
            remote.complete(_request())

        self.assertEqual("ai_result_expired", captured.exception.code)

    def test_polling_stops_at_the_wait_limit_without_resending(self):
        accepted = {"jobId": "job-5", "status": "running", "pollAfterMs": 1000}
        running = {"jobId": "job-5", "status": "running", "pollAfterMs": 1000}
        config = _config(jobs={"wait_sec": 2, "poll_min_ms": 1000})
        remote, fake, _ = _client([accepted, running, running], config=config)

        with self.assertRaises(AiOfflineError) as captured:
            remote.complete(_request())

        # サーバー側では処理が続いている可能性があるため、投げ直して二重に払わない。
        self.assertEqual("ai_job_timeout", captured.exception.code)
        self.assertEqual(1, len(fake.bodies()))

    def test_poll_interval_has_a_floor(self):
        accepted = {"jobId": "job-6", "status": "running", "pollAfterMs": 1}
        done = dict(_COMPLETION)
        done.update({"status": "succeeded", "pollAfterMs": 0})
        config = _config(jobs={"wait_sec": 900, "poll_min_ms": 1500})
        remote, _, recorder = _client([accepted, done], config=config)

        remote.complete(_request())

        self.assertEqual([1.5], recorder.slept, "サーバーの指示が短すぎても叩き過ぎない")


class AiRemoteFailureTest(unittest.TestCase):

    # ApiOfflineError → AiOfflineError (既存の採点結果へ縮退させる判断材料)
    def test_offline_is_translated_after_retries(self):
        config = _config(retry={"max_attempts": 2, "backoff_sec": 1.0})
        remote, fake, recorder = _client(
            [ApiOfflineError("接続できません"), ApiOfflineError("接続できません")], config=config)

        with self.assertRaises(AiOfflineError):
            remote.complete(_request())

        self.assertEqual(2, len(fake.bodies()))

        # 到達できなかった再送は**同じ冪等キー**で送る (受理済みなら二重に払わない)。
        self.assertEqual("id-1", fake.bodies()[0]["requestId"])
        self.assertEqual("id-1", fake.bodies()[1]["requestId"])
        self.assertEqual([1.0], recorder.slept)

    def test_unavailable_is_offline(self):
        remote, _, _ = _client([ApiError("停止中です", status=503, code="ai_unavailable")])

        with self.assertRaises(AiOfflineError) as captured:
            remote.complete(_request())

        self.assertEqual("ai_unavailable", captured.exception.code)

    # 混雑は待って作り直す。サーバーは同じ冪等キーの再送を「前回の失敗」として返すため、
    # 鍵を変えなければ同じ失敗を受け続ける。
    def test_busy_is_retried_with_a_new_request_id(self):
        config = _config(retry={"max_attempts": 3, "backoff_sec": 2.0})
        remote, fake, recorder = _client(
            [ApiError("混雑しています", status=429, code="ai_busy", retry_after=5), _COMPLETION],
            config=config)

        completion = remote.complete(_request())

        self.assertEqual({"scores": [1, 2, 3]}, completion.result)
        self.assertEqual(["id-1", "id-2"],
                         [body["requestId"] for body in fake.bodies()])
        self.assertEqual([5.0], recorder.slept, "Retry-After を優先する")

    def test_quota_is_not_retried(self):
        config = _config(retry={"max_attempts": 3, "backoff_sec": 1.0})
        remote, fake, _ = _client(
            [ApiError("上限に達しました", status=429, code="ai_quota_exceeded")], config=config)

        with self.assertRaises(AiProviderError) as captured:
            remote.complete(_request())

        self.assertEqual("ai_quota_exceeded", captured.exception.code)
        self.assertEqual(1, len(fake.bodies()), "上限は待っても通らない")

    # 追補 A3: 出力が JSON として壊れていたら 1 回だけ作り直す。
    def test_invalid_output_is_retried_once(self):
        broken = ApiError("応答が壊れています", status=422, code="ai_invalid_output")
        remote, fake, _ = _client([broken, _COMPLETION])

        completion = remote.complete(_request())

        self.assertEqual({"scores": [1, 2, 3]}, completion.result)
        self.assertEqual(["id-1", "id-2"], [body["requestId"] for body in fake.bodies()])

    def test_invalid_output_twice_gives_up(self):
        broken = ApiError("応答が壊れています", status=422, code="ai_invalid_output")
        remote, fake, _ = _client([broken, broken])

        with self.assertRaises(AiPlanError) as captured:
            remote.complete(_request())

        self.assertEqual("ai_invalid_output", captured.exception.code)
        self.assertEqual(2, len(fake.bodies()), "作り直しは 1 回だけ")

    # 希望モデルが許可されていない場合は希望を外して送り直す。
    def test_model_not_allowed_falls_back_to_the_role_default(self):
        rejected = ApiError("許可されていません", status=400, code="ai_model_not_allowed")
        remote, fake, _ = _client([rejected, _COMPLETION])

        remote.complete(_request())

        bodies = fake.bodies()
        self.assertEqual("claude-sonnet-5-5", bodies[0]["modelHint"])
        self.assertNotIn("modelHint", bodies[1], "希望を外して役割の既定へ委ねる")

    def test_payload_too_large_is_reported_to_the_caller(self):
        too_large = ApiError("大きすぎます", status=400, code="ai_payload_too_large")
        remote, fake, _ = _client([too_large])

        with self.assertRaises(AiProviderError) as captured:
            remote.complete(_request())

        self.assertEqual("ai_payload_too_large", captured.exception.code)
        self.assertEqual(1, len(fake.bodies()), "入力を変えずに再送しない")

    # 再ログインは AI の都合で握り潰さず、既存の経路へ渡す。
    def test_reauth_required_passes_through(self):
        remote, _, _ = _client([ReauthRequiredError("ログインが必要です", status=401,
                                                    code="reauth_required")])

        with self.assertRaises(ReauthRequiredError):
            remote.complete(_request())

    def test_login_required_is_reported_with_its_code(self):
        remote, _, _ = _client([ApiError("ログインが必要です", status=400,
                                         code="login_required")])

        with self.assertRaises(AiProviderError) as captured:
            remote.complete(_request())

        self.assertEqual("login_required", captured.exception.code)


if __name__ == "__main__":
    unittest.main()
