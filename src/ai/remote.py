# Stretheus API 経由の AI 呼び出し (docs/AiEditor/resolve1.md §6.2 / R3)
#
# ここが**唯一**、サーバーとの線の上の形を知っている場所である。
#   ・クライアント内は snake_case (scene_eval / end_turn)、線の上は camelCase
#     (sceneEval / endTurn)。読み替えはこのモジュールの対応表 1 か所で済ませる。
#   ・冪等キー (requestId) を必ず付ける。タイムアウト再送で二重に実費を払わない
#     (追補 A1 / StretheusAPI resolve1.md §3.2)。
#   ・同期で終わらない処理は 202 で受理され、GET /api/ai/jobs/{id} で受け取る
#     (追補 A2 / 同 §2.1)。Azure App Service の 230 秒制限のためである。
#   ・出力が JSON として壊れていた場合 (ai_invalid_output) は 1 回だけ作り直す
#     (追補 A3)。2 回目は諦めて呼び出し側へ返し、既存の採点結果へ縮退させる。
#
# 鍵・プロンプト全文・応答全文はログへ出さない (§17.2)。
import json
import time

from ..exceptions import (AiOfflineError, AiPlanError, AiProviderError, ApiError,
                          ApiOfflineError, AutoEditError, ReauthRequiredError)
from ..i18n import tr
from ..utils.logger import get_logger
from . import config as ai_config
from . import plan_schema
from .provider import (STOP_END_TURN, STOP_MAX_TOKENS, STOP_REFUSAL, AiCompletion,
                       AiEstimate, AiServerConfig, AiUsage, new_request_id)

_logger = get_logger(__name__)

# 役割 (クライアント内 → 線の上)。サーバーの列挙は camelCase で、整数も許さない。
_ROLE_TO_WIRE = {
    "scene_eval": "sceneEval",
    "scene_scan": "sceneScan",
    "plan": "plan",
    "review": "review",
    "profile": "profile",
    "classify": "classify",
}


# 線の上の役名を返す (サーバーの roles と突き合わせるのに使う)。
# 知らない役は None。
def wire_role(role):
    return _ROLE_TO_WIRE.get(ai_config.role_of(role))


# 応答が終わった理由 (線の上 → クライアント内)。
_STOP_FROM_WIRE = {
    "endTurn": STOP_END_TURN,
    "maxTokens": STOP_MAX_TOKENS,
    "refusal": STOP_REFUSAL,
}

# ジョブの状態 (線の上の値)。
_JOB_PENDING = ("pending", "running")
_JOB_SUCCEEDED = "succeeded"
_JOB_FAILED = ("failed", "refused")

# 混雑・中断。待てば通る見込みがあるため、**新しい冪等キー**で作り直す
# (サーバーは同じキーの再送を「前回の結果」として返すため、作り直さないと同じ失敗を受け続ける)。
_RETRY_WITH_NEW_KEY = frozenset({"ai_busy", "ai_result_expired", "rate_limited"})

# 応答の形が壊れていた。1 回だけ作り直す (追補 A3)。
_CODE_INVALID_OUTPUT = "ai_invalid_output"

# 希望したモデルが許可されていない。希望を外して 1 回だけ送り直す。
_CODE_MODEL_NOT_ALLOWED = "ai_model_not_allowed"

# 機能が停止している。縮退の判断材料にする。
_CODE_UNAVAILABLE = "ai_unavailable"

# 受理された処理が待ち時間に収まらなかった。
# サーバー側では処理が続いている可能性があるため、**ここでは送り直さない**
# (送り直すと同じ判断に二重で払う)。呼び出し側は縮退する。
_CODE_JOB_TIMEOUT = "ai_job_timeout"

# 安全側の拒否。再試行で押し通さない。
_CODE_REFUSED = "ai_refused"

# 到達できなかった場合でも送り直さないコード。
#   ai_unavailable : 鍵が未設定・機能停止中。待っても変わらない
#   ai_job_timeout : 受理された処理が続いている。投げ直すと二重に払う
_NO_RETRY_OFFLINE_CODES = frozenset({_CODE_UNAVAILABLE, _CODE_JOB_TIMEOUT})

# 待ち時間の上限 (サーバーの Retry-After が長すぎる場合の保険)。
_MAX_BACKOFF_SEC = 30.0


class RemoteAiClient:

    # client      : services.api_client.ApiClient (JWT の付与と 401 の回復を持つ)
    # config      : ai/config.ai_config() が返す既定補完済みの設定
    # sleep       : テストから差し替えるための待ち関数
    # monotonic   : 同上 (経過時間の判定)
    # request_ids : 冪等キーの採番 (テストから固定する)
    def __init__(self, client, config, sleep=None, monotonic=None, request_ids=None):
        self._client = client
        self._config = config or ai_config.DEFAULT_AI
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._new_request_id = request_ids or new_request_id

        self._complete_path = ai_config.endpoint_path(self._config, "complete")
        self._count_path = ai_config.endpoint_path(self._config, "count")
        self._config_path = ai_config.endpoint_path(self._config, "config")
        self._jobs_path = ai_config.endpoint_path(self._config, "jobs")

        self._max_attempts, self._backoff_sec = ai_config.retry_policy(self._config)
        self._job_wait_sec, self._poll_min_sec = ai_config.job_policy(self._config)

    # ---- 公開 -------------------------------------------------------------

    # 1 回の判断を求める。同期で終われば結果、終わらなければポーリングで受け取る。
    def complete(self, request):
        request_id = self._new_request_id()
        model_hint = ai_config.model_hint(self._config, request.model_key)
        invalid_output_retried = False
        attempt = 0

        while True:
            attempt += 1
            body = self._build_body(request, request_id, model_hint)
            started = self._monotonic()

            try:
                response = self._post(self._complete_path, body)
                completion = self._completion_of(response, request)

            # AiOfflineError は AiProviderError の派生であるため、**先に**捕まえる。
            except AiOfflineError as e:
                if e.code in _NO_RETRY_OFFLINE_CODES:
                    # 機能停止中、または受理された処理が続いている。送り直さない。
                    raise

                # 到達できない。**同じ冪等キーで**送り直す
                # (サーバーが受理していた場合は保存済みの結果が返り、二重に払わない)。
                if attempt < self._max_attempts:
                    self._wait(attempt, e.retry_after)
                    continue

                raise
            except (AiProviderError, AiPlanError) as e:
                code = getattr(e, "code", None)

                # 応答の形が壊れていた。入力は同じでも作り直せば通ることがある (追補 A3)。
                if code == _CODE_INVALID_OUTPUT and not invalid_output_retried:
                    invalid_output_retried = True
                    request_id = self._new_request_id()
                    _logger.warning("AI の応答が壊れていたため作り直します (role=%s)。", request.role)
                    continue

                # 希望モデルが許可されていない。希望を外せば役割の既定で通る。
                if code == _CODE_MODEL_NOT_ALLOWED and model_hint:
                    _logger.warning("モデル %s は許可されていないため希望を外します。", model_hint)
                    model_hint = ""
                    continue

                # 混雑・中断・期限切れは、待って**新しい冪等キー**で作り直す
                # (同じキーでは前回の失敗がそのまま返るため)。
                if code in _RETRY_WITH_NEW_KEY and attempt < self._max_attempts:
                    self._wait(attempt, getattr(e, "retry_after", None))
                    request_id = self._new_request_id()
                    continue

                raise

            self._log_call(request, completion, self._monotonic() - started, attempt)

            return completion

    # 送る前の見積り。ポイントは消費しない。
    def count(self, request):
        body = self._build_body(request, self._new_request_id(),
                                ai_config.model_hint(self._config, request.model_key))
        response = self._post(self._count_path, body)
        if not isinstance(response, dict):
            raise AiPlanError(tr("AI の見積りの応答を解釈できませんでした。"))

        return AiEstimate(
            input_tokens=response.get("inputTokens", 0),
            estimated_output_tokens=response.get("estimatedOutputTokens", 0),
            estimated_points=response.get("estimatedPoints", 0),
            estimated_usd=response.get("estimatedUsd", 0.0),
            model=response.get("model", ""))

    # サーバーの構成。価格とモデルをクライアントへ焼かないための口。
    def server_config(self):
        response = self._get(self._config_path)
        if not isinstance(response, dict):
            raise AiPlanError(tr("AI の構成の応答を解釈できませんでした。"))

        return AiServerConfig(
            enabled=response.get("enabled", False),
            charge_mode=response.get("chargeMode", "measure"),
            require_login=response.get("requireLogin", True),
            roles=response.get("roles") if isinstance(response.get("roles"), dict) else {},
            allowed_models=response.get("allowedModels")
            if isinstance(response.get("allowedModels"), list) else [],
            limits=response.get("limits") if isinstance(response.get("limits"), dict) else {},
            pricing=response.get("pricing") if isinstance(response.get("pricing"), dict) else {})

    # ---- 要求の組み立て ---------------------------------------------------

    def _build_body(self, request, request_id, model_hint):
        role = _ROLE_TO_WIRE.get(ai_config.role_of(request.role))
        if role is None:
            raise AiPlanError(tr("未知の役割です: {role}", role=request.role),
                              code="validation_failed")

        body = {
            # 冪等キー。再送で二重に実費を払わないための鍵 (追補 A1)。
            "requestId": request_id,
            "role": role,
            "system": request.system,
            "payload": request.payload,
            "maxOutputTokens": int(request.max_output_tokens
                                   or ai_config.max_output_tokens(self._config)),
        }

        if model_hint:
            body["modelHint"] = model_hint

        effort = ai_config.effort_hint(self._config, request.model_key)
        if effort:
            body["effortHint"] = effort

        if request.project_id:
            body["projectId"] = str(request.project_id)

        if request.schema is not None:
            # 構造化出力が受け付けないキーワードを落としてから送る。
            # 残すとサーバー経由で Anthropic が 400 を返し、役割ごとの呼び出しが
            # すべて ai_unavailable へ落ちる (plan_schema.sanitize_for_api 参照)。
            body["schema"] = plan_schema.sanitize_for_api(request.schema)

        if request.cache_prefix_id:
            body["cachePrefixId"] = str(request.cache_prefix_id)

        if request.images:
            body["images"] = [{"mediaType": image.media_type, "data": image.data_base64}
                              for image in request.images]

        return body

    # ---- 応答の読み取り ---------------------------------------------------

    # 200 (結果) と 202 (受理) を見分ける。
    #
    # 既存の ApiClient は本文だけを返し、ステータスコードを渡さない。
    # 本文の形で判別できる (結果には result、受理には jobId がある) ため、
    # **共有の ApiClient には手を入れない**。追補 A2 の実装をこのモジュールに閉じる。
    def _completion_of(self, response, request):
        if not isinstance(response, dict):
            raise AiPlanError(tr("AI の応答を解釈できませんでした。"))

        if "result" in response:
            return self._parse_completion(response)

        job_id = response.get("jobId")
        if job_id:
            return self._wait_for_job(str(job_id), response, request)

        raise AiPlanError(tr("AI の応答に結果がありません。"))

    def _parse_completion(self, response, job_id=None):
        result = response.get("result")
        if result is None:
            raise AiPlanError(tr("AI の応答に結果がありません。"))

        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        points = response.get("points") if isinstance(response.get("points"), dict) else {}
        stop_reason = _STOP_FROM_WIRE.get(str(response.get("stopReason") or ""), STOP_END_TURN)

        # 安全側の拒否は結果が無いはずだが、念のためここでも落とす (再試行しない)。
        if stop_reason == STOP_REFUSAL:
            raise AiPlanError(tr("AI がこの内容への応答を控えました。"), code=_CODE_REFUSED)

        return AiCompletion(
            result=result,
            model=response.get("model", ""),
            usage=AiUsage(
                input_tokens=usage.get("inputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
                cache_creation_input_tokens=usage.get("cacheCreationInputTokens", 0),
                cache_read_input_tokens=usage.get("cacheReadInputTokens", 0)),
            points_charged=points.get("charged", 0),
            points_estimated=points.get("estimated", 0),
            stop_reason=stop_reason,
            duration_ms=response.get("durationMs", 0),
            job_id=job_id)

    # 202 で受理された処理を待つ (追補 A2)。
    def _wait_for_job(self, job_id, accepted, request):
        poll_sec = self._poll_sec(accepted.get("pollAfterMs"))
        deadline = self._monotonic() + self._job_wait_sec

        _logger.info("AI の処理を受理しました (role=%s job=%s)。", request.role, job_id)

        while True:
            self._sleep(poll_sec)

            response = self._get(self._jobs_path.rstrip("/") + "/" + job_id)
            if not isinstance(response, dict):
                raise AiPlanError(tr("AI の処理状況を解釈できませんでした。"))

            status = str(response.get("status") or "")

            if status == _JOB_SUCCEEDED and response.get("result") is not None:
                return self._parse_completion(response, job_id=job_id)

            if status in _JOB_FAILED or status == _JOB_SUCCEEDED:
                # succeeded で result が無いのは保持期限切れ (結果の本文は捨てられている)。
                raise self._error_of_job(response)

            if status not in _JOB_PENDING:
                raise AiPlanError(tr("AI の処理状況を解釈できませんでした。"))

            if self._monotonic() >= deadline:
                raise AiOfflineError(
                    tr("AI の処理が時間内に終わりませんでした。"), code=_CODE_JOB_TIMEOUT)

            poll_sec = self._poll_sec(response.get("pollAfterMs"))

    def _poll_sec(self, poll_after_ms):
        try:
            seconds = float(poll_after_ms or 0) / 1000.0
        except (TypeError, ValueError):
            seconds = 0.0

        return max(seconds, self._poll_min_sec)

    # ジョブの失敗を、ProblemDetails と同じ分岐の例外へ写す。
    def _error_of_job(self, response):
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        code = str(error.get("code") or _CODE_UNAVAILABLE)
        message = str(error.get("message") or tr("AI の処理に失敗しました。"))

        return self._to_ai_error(message, status=None, code=code, retry_after=None)

    # ---- 通信 -------------------------------------------------------------

    def _post(self, path, body):
        return self._call("POST", path, body)

    def _get(self, path):
        return self._call("GET", path, None)

    def _call(self, method, path, body):
        if self._client is None:
            raise AiOfflineError(tr("Stretheus API へ接続できません。"), code=_CODE_UNAVAILABLE)

        try:
            if method == "GET":
                return self._client.get(path)

            return self._client.post(path, body)
        except ReauthRequiredError:
            # 再ログインが必要。AI の都合で握り潰さず、呼び出し側の既存経路へ渡す。
            raise
        except ApiOfflineError as e:
            # code はサーバーの ProblemDetails のものをそのまま持つ (接続失敗では None)。
            # ここで ai_unavailable へ寄せてしまうと、一時的な障害も
            # 「機能停止中」と区別できなくなり、送り直せなくなる。
            raise AiOfflineError(str(e), status=e.status, code=e.code,
                                 retry_after=e.retry_after) from e
        except ApiError as e:
            raise self._to_ai_error(str(e), e.status, e.code, e.retry_after) from e
        except AutoEditError:
            raise
        except (TypeError, ValueError) as e:
            # 本文を JSON へ落とせない (payload に JSON 化できない値が混ざっている)。
            raise AiPlanError(tr("AI へ送る内容を JSON へ変換できませんでした。")) from e

    # サーバーのエラーコードを、クライアントの判断に使える例外へ写す。
    @staticmethod
    def _to_ai_error(message, status, code, retry_after):
        if code == _CODE_UNAVAILABLE:
            return AiOfflineError(message, status=status, code=code, retry_after=retry_after)

        if code in (_CODE_REFUSED, _CODE_INVALID_OUTPUT):
            return AiPlanError(message, code=code)

        return AiProviderError(message, status=status, code=code, retry_after=retry_after)

    # 待ち時間。サーバーの Retry-After を優先し、無ければ指数バックオフ。
    def _wait(self, attempt, retry_after):
        if retry_after:
            delay = float(retry_after)
        else:
            delay = self._backoff_sec * (2 ** max(attempt - 1, 0))

        self._sleep(max(0.0, min(delay, _MAX_BACKOFF_SEC)))

    # ---- ログ -------------------------------------------------------------

    # 本文は出さない。残すのは役割・モデル・所要・トークン・ポイント・再試行回数。
    def _log_call(self, request, completion, elapsed_sec, attempts):
        usage = completion.usage
        _logger.info(
            "AI 呼び出し: role=%s model=%s input=%d output=%d cache_read=%d "
            "cache_create=%d points=%d/%d %dms attempts=%d%s",
            request.role, completion.model, usage.input_tokens, usage.output_tokens,
            usage.cache_read_input_tokens, usage.cache_creation_input_tokens,
            completion.points_charged, completion.points_estimated,
            completion.duration_ms or int(elapsed_sec * 1000), attempts,
            " job=" + str(completion.job_id) if completion.job_id else "")


# 送信本文を作業フォルダへ落とす (ai.debug.dump_payloads = true のときだけ / §17.2)。
# 既定ではオフであり、プロンプト全文はログへ出さない。
def dump_payload(config, work_dir, name, body):
    if not ai_config.dumps_payloads(config) or not work_dir:
        return None

    import os

    try:
        os.makedirs(work_dir, exist_ok=True)
        path = os.path.join(work_dir, "ai_" + str(name) + ".json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(body, handle, ensure_ascii=False, indent=2)

        return path
    except OSError as e:
        _logger.warning("AI の送信本文を保存できませんでした: %s", e)

        return None
