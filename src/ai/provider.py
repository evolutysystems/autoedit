# AI 呼び出しの口 (docs/AiEditor/resolve1.md §6 / R3)
#
# scene_finder / planner / reviewer が知っているのは AiProvider だけである。
# 実体は次の 3 つで、どれに差し替えても呼び出し側は変わらない。
#
#   StretheusProvider : Stretheus API 経由 (既定)。鍵はサーバーだけが持つ
#   DirectProvider    : 開発用の直接呼び出し (ai.direct_mode = true のときだけ)
#   MockProvider      : テスト用。固定の応答を返し、ネットワークへ出ない
#
# 利用量 (トークン) の正は応答の usage であり、クライアントの概算は
# 分割の判断にだけ使う (§6.1)。
import uuid

from ..exceptions import AiOfflineError, AiPlanError, AiProviderError
from ..i18n import tr
from ..utils.logger import get_logger
from . import config as ai_config

_logger = get_logger(__name__)

# 応答が終わった理由 (サーバーは camelCase。クライアント内は snake_case で持つ)。
STOP_END_TURN = "end_turn"
STOP_MAX_TOKENS = "max_tokens"
STOP_REFUSAL = "refusal"

# 「この VOD では AI を諦めて既存の採点結果で続行する」と判断するコード (§17 / §17.3)。
# 残高不足・上限・未ログイン・機能停止は、再試行しても同じ結果になる。
DEGRADABLE_CODES = frozenset({
    "ai_unavailable",
    "ai_quota_exceeded",
    "ai_busy",
    "ai_budget_exceeded",
    "ai_job_timeout",
    "insufficient_points",
    "login_required",
})


# 1 回の判断の依頼。
#
# role        : scene_eval / plan / review / profile / classify
# model_key   : 設定上のモデルキー (既定は role。review_critic のような派生を許す)
# system      : モデルへの指示 (役割の前置きはサーバーが持つ)
# payload     : 入力の JSON (キーの順序は呼び出し側が固定する / §6.4)
# schema      : 期待する JSON の形 (構造化出力)
# images      : 参照させる静止画 (§12.5)
# cache_prefix_id : 同じ前置きを使い回すという意思表示 (§6.4)
class AiRequest:

    def __init__(self, role, system, payload, schema=None, images=None,
                 cache_prefix_id=None, project_id=None, max_output_tokens=None,
                 model_key=None):
        self.role = str(role)
        self.model_key = str(model_key or role)
        self.system = str(system or "")
        self.payload = payload
        self.schema = schema
        self.images = list(images or [])
        self.cache_prefix_id = cache_prefix_id
        self.project_id = project_id
        self.max_output_tokens = max_output_tokens


# モデルへ参照させる静止画 1 枚。送信後に元のファイルを消すのは呼び出し側の責務 (§12.5)。
class AiImage:

    def __init__(self, media_type, data_base64):
        self.media_type = str(media_type or "")
        self.data_base64 = str(data_base64 or "")


# 使ったトークン。4 項目は加算関係にあり、合計を出すときに引き算をしてはならない。
class AiUsage:

    def __init__(self, input_tokens=0, output_tokens=0,
                 cache_creation_input_tokens=0, cache_read_input_tokens=0):
        self.input_tokens = int(input_tokens or 0)
        self.output_tokens = int(output_tokens or 0)
        self.cache_creation_input_tokens = int(cache_creation_input_tokens or 0)
        self.cache_read_input_tokens = int(cache_read_input_tokens or 0)

    # 入力側の合計 (キャッシュの読み・書きを含む)。
    @property
    def prompt_tokens(self):
        return (self.input_tokens
                + self.cache_creation_input_tokens
                + self.cache_read_input_tokens)

    @property
    def total_tokens(self):
        return self.prompt_tokens + self.output_tokens


# 1 回の判断の結果。
class AiCompletion:

    def __init__(self, result, model="", usage=None, points_charged=0,
                 points_estimated=0, stop_reason=STOP_END_TURN, duration_ms=0,
                 job_id=None):
        self.result = result
        self.model = str(model or "")
        self.usage = usage or AiUsage()
        self.points_charged = int(points_charged or 0)
        self.points_estimated = int(points_estimated or 0)
        self.stop_reason = str(stop_reason or STOP_END_TURN)
        self.duration_ms = int(duration_ms or 0)
        # 202 で受理され、ポーリングで受け取った場合のジョブ ID (ログ用)。
        self.job_id = job_id


# 送る前の見積り (/api/ai/count)。ポイントは消費しない。
class AiEstimate:

    def __init__(self, input_tokens=0, estimated_output_tokens=0,
                 estimated_points=0, estimated_usd=0.0, model=""):
        self.input_tokens = int(input_tokens or 0)
        self.estimated_output_tokens = int(estimated_output_tokens or 0)
        self.estimated_points = int(estimated_points or 0)
        self.estimated_usd = float(estimated_usd or 0.0)
        self.model = str(model or "")


# サーバーが返す構成 (/api/ai/config)。
# 価格とモデルをクライアントへ焼かないための口であり、enabled が false なら
# クライアントは AI 工程を丸ごと飛ばす (§17)。
class AiServerConfig:

    def __init__(self, enabled=False, charge_mode="measure", require_login=True,
                 roles=None, allowed_models=None, limits=None, pricing=None):
        self.enabled = bool(enabled)
        self.charge_mode = str(charge_mode or "measure")
        self.require_login = bool(require_login)
        self.roles = dict(roles or {})
        self.allowed_models = list(allowed_models or [])
        self.limits = dict(limits or {})
        self.pricing = dict(pricing or {})

    # 課金が有効か (計測のみなら false)。表示の文言を分けるために使う。
    @property
    def charging(self):
        return self.charge_mode == "charge"


# 呼び出しの抽象。
class AiProvider:

    name = "base"

    # 1 回の判断を求める。失敗は AiError 系で投げる。
    def complete_json(self, request):
        raise NotImplementedError

    # 送る前の見積り。できない実装は None を返してよい。
    def count_tokens(self, request):
        return None

    # サーバーの構成。取得できない場合は None。
    def server_config(self):
        return None


# テスト用。固定の応答を返し、ネットワークへ出ない。
class MockProvider(AiProvider):

    name = "mock"

    def __init__(self, results=None, usage=None, stop_reason=STOP_END_TURN):
        # results: 役割ごとの固定 JSON。role をキーに引き、無ければ空の辞書を返す。
        self._results = dict(results or {})
        self._usage = usage or AiUsage(1000, 200, 0, 0)
        self._stop_reason = stop_reason
        self.requests = []

    def complete_json(self, request):
        self.requests.append(request)
        result = self._results.get(request.role, {})

        return AiCompletion(result=result, model="mock", usage=self._usage,
                            stop_reason=self._stop_reason)

    def count_tokens(self, request):
        self.requests.append(request)

        return AiEstimate(input_tokens=self._usage.input_tokens,
                          estimated_output_tokens=self._usage.output_tokens,
                          model="mock")

    def server_config(self):
        return AiServerConfig(enabled=True, charge_mode="measure", require_login=False)


# Stretheus API 経由の呼び出し。鍵はサーバーだけが持つ (§6.1)。
#
# 1 プロジェクト分の上限 (ai.cost.*) と利用量の記録はここで行い、
# 通信と要求・応答の変換は ai/remote.py に閉じる。
class StretheusProvider(AiProvider):

    name = "stretheus"

    def __init__(self, auth, settings=None, tracker=None, remote=None):
        self._auth = auth
        self._config = ai_config.ai_config(settings)
        self._tracker = tracker

        if remote is None:
            # 循環 import を避けるため、ここで読み込む (remote は provider の型を使う)。
            from .remote import RemoteAiClient

            remote = RemoteAiClient(self._client_of(auth), self._config)
        self._remote = remote

    @staticmethod
    def _client_of(auth):
        return getattr(auth, "client", None)

    def complete_json(self, request):
        self._ensure_session()
        if self._tracker is not None:
            self._tracker.guard(request.role)

        completion = self._remote.complete(request)

        if self._tracker is not None:
            self._tracker.record(request, completion)

        return completion

    def count_tokens(self, request):
        self._ensure_session()

        return self._remote.count(request)

    def server_config(self):
        try:
            self._ensure_session()

            return self._remote.server_config()
        except (AiProviderError, AiPlanError) as e:
            # 構成が取れないことは縮退の判断材料でしかない。例外にしない。
            _logger.warning("AI の構成を取得できませんでした: %s", e)

            return None

    # AI は実費が出るため、サーバーは既定で身元のある利用者だけに提供する
    # (RequireLogin)。匿名セッションでは login_required になるため、先に確かめる。
    def _ensure_session(self):
        if self._auth is None:
            raise AiOfflineError(tr("Stretheus へログインしていないため AI を利用できません。"),
                                 code="login_required")

        if not self._auth.ensure_session():
            raise AiOfflineError(tr("Stretheus API へ接続できないため AI を利用できません。"),
                                 code="ai_unavailable")

        if getattr(self._auth, "is_anonymous", lambda: False)():
            raise AiProviderError(
                tr("AI の機能のご利用には Twitch または Google でのログインが必要です。"),
                status=400, code="login_required")


# 開発用の直接呼び出し (§6.5)。**今は使えない経路である。**
#
# この経路は「サーバーの口ができるまで判断の質を測る」ための足場であり
# (§21 の 8 番 → 11 番)、サーバー側の /api/ai/* が実装された時点で役目を終えた
# (§22 T3 完了)。配布物へ公式 SDK (anthropic) を入れない決まりもあるため、
# ここでは実装を持たず、設定が true のときに**分かる形で**失敗させる。
#
# 鍵の保管だけは ai/key_store.py に残してある (開発端末で測り直すときのため)。
class DirectProvider(AiProvider):

    name = "direct"

    def __init__(self, settings=None, api_key=None):
        self._config = ai_config.ai_config(settings)
        self._api_key = api_key

    def complete_json(self, request):
        raise AiOfflineError(
            tr("ai.direct_mode は利用できません。ai.direct_mode = false にして "
               "Stretheus API 経由で呼び出してください。"),
            code="ai_unavailable")


# 設定に従ってプロバイダーを作る。AI が無効なら None を返す。
#
# 呼び出し側は None を「AI 工程を飛ばす」と読む (§17 の縮退)。
def create_provider(settings, auth=None, tracker=None):
    config = ai_config.ai_config(settings)
    if not config["enabled"]:
        return None

    if config["direct_mode"]:
        _logger.info("AI は開発用の直接呼び出しで動作します (ai.direct_mode = true)。")

        return DirectProvider(settings)

    return StretheusProvider(auth, settings=settings, tracker=tracker)


# この失敗で AI 工程を諦めてよいか (既存の採点結果へ縮退する判断 / §17)。
def should_degrade(error):
    if isinstance(error, AiOfflineError):
        return True
    if isinstance(error, AiPlanError):
        # 応答が JSON でない・安全側の拒否。再試行では通らない。
        return True
    if isinstance(error, AiProviderError):
        return error.code in DEGRADABLE_CODES

    return False


# 冪等キーを作る。再送で二重に実費を払わないための鍵 (StretheusAPI resolve1.md §3.2)。
def new_request_id():
    return uuid.uuid4().hex
