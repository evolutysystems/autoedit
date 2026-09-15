# Stretheus API への HTTP クライアント (StretheusAPI resolve2 §6.1 / §6.5)
# ベース URL の結合・JSON の送受信・Bearer の付与・401 時のリフレッシュ 1 回・
# ProblemDetails の code を見た例外化をここに閉じ込める。
# 依存は標準ライブラリのみ。トークンはログへ出力しない。
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from ..exceptions import ApiError, ApiOfflineError, ReauthRequiredError
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 再ログインが必要なことを示す API のエラーコード (resolve2 §4.2)
CODE_REAUTH_REQUIRED = "reauth_required"
# JWT の署名・形式が不正。別のサーバーが発行したトークンを提示した場合もこれになる
CODE_INVALID_TOKEN = "invalid_token"

# リフレッシュでは回復できず、ログインし直すしかないエラーコード
_UNRECOVERABLE_CODES = frozenset({CODE_REAUTH_REQUIRED, CODE_INVALID_TOKEN})
# 楽観的排他の競合。時間をおいて再送してよい (resolve2 §3.6)
CODE_CONCURRENCY_CONFLICT = "concurrency_conflict"

# 429 / 409 concurrency_conflict の待ち時間。Retry-After が無い場合の既定と上限。
_DEFAULT_RETRY_AFTER_SEC = 3.0
_MAX_RETRY_AFTER_SEC = 10.0


# JWT を供給する側 (stretheus_auth.StretheusAuth) に求める操作。
# 循環 import を避けるため型では縛らず、以下のメソッドを持つオブジェクトを受け取る。
#   access_token(refresh_if_needed=True) -> str | None
#   refresh() -> bool           リフレッシュできたら True
#   on_reauth_required()        保存済みトークンを破棄する
class ApiClient:

    def __init__(self, base_url, timeout_sec=15, token_provider=None):
        self._base_url = str(base_url or "").rstrip("/")
        self._timeout = float(timeout_sec or 15)
        self._token_provider = token_provider

    @property
    def base_url(self):
        return self._base_url

    # JWT の供給元を後から差し込む (StretheusAuth が自身を登録する)。
    def set_token_provider(self, provider):
        self._token_provider = provider

    def get(self, path, authenticated=True):
        return self.request("GET", path, authenticated=authenticated)

    def post(self, path, body=None, authenticated=True):
        return self.request("POST", path, body=body, authenticated=authenticated)

    # API を呼び出して応答 JSON (dict) を返す。204 など本文が無ければ None。
    # authenticated=True なら Bearer を付け、401 を受けたら 1 回だけリフレッシュして再送する。
    def request(self, method, path, body=None, authenticated=True, retry_auth=True, retry_busy=True):
        url = self._build_url(path)
        token = None
        if authenticated and self._token_provider is not None:
            # 送信前の先回りリフレッシュ (expires_at − 5 分を過ぎていれば更新される)。
            token = self._token_provider.access_token()

            # リフレッシュの結果トークンを失った場合を含め、資格情報が無いまま
            # 認証必須の API を呼ばない。匿名要求の 401 は code を持たず、
            # 呼び出し側が再ログインの必要性を判別できないため。
            if not token:
                raise ReauthRequiredError(
                    "ログインが必要です。", status=401, code=CODE_REAUTH_REQUIRED)

        try:
            return self._send(method, url, body, token)
        except ApiError as e:
            if e.status == 401 and e.code in _UNRECOVERABLE_CODES:
                if self._token_provider is not None:
                    self._token_provider.on_reauth_required()
                raise ReauthRequiredError(str(e), status=e.status, code=e.code) from e

            # 期限切れ以外の理由でも 401 になりうる (サーバー再起動直後など)。1 回だけ回復を試す。
            if (e.status == 401 and retry_auth and authenticated
                    and self._token_provider is not None and self._token_provider.refresh()):
                return self.request(method, path, body=body, authenticated=authenticated,
                                    retry_auth=False, retry_busy=retry_busy)

            if retry_busy and self._is_busy(e):
                time.sleep(self._retry_delay(e))
                return self.request(method, path, body=body, authenticated=authenticated,
                                    retry_auth=retry_auth, retry_busy=False)

            raise

    # 混雑 (429) と楽観的排他の競合 (409) は時間をおけば通る (§6.5)。
    @staticmethod
    def _is_busy(error):
        return error.status == 429 or (error.status == 409 and error.code == CODE_CONCURRENCY_CONFLICT)

    @staticmethod
    def _retry_delay(error):
        delay = error.retry_after if error.retry_after else _DEFAULT_RETRY_AFTER_SEC
        return max(0.0, min(float(delay), _MAX_RETRY_AFTER_SEC))

    def _build_url(self, path):
        if not self._base_url:
            raise ApiError("API のベース URL が未設定です。設定 (setting.json) の api.base_url を確認してください。")
        return self._base_url + "/" + str(path).lstrip("/")

    def _send(self, method, url, body, token):
        data = None
        request = urllib.request.Request(url, method=str(method).upper())
        request.add_header("Accept", "application/json")
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request.add_header("Content-Type", "application/json; charset=utf-8")
        if token:
            request.add_header("Authorization", "Bearer " + token)

        try:
            with urllib.request.urlopen(request, data=data, timeout=self._timeout) as response:
                return self._read_json(response)
        except urllib.error.HTTPError as e:
            raise self._to_error(e) from e
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            # 接続できない・応答が無い。オフライン扱いとする (§6.5)。
            raise ApiOfflineError(f"Stretheus API へ接続できません: {e}") from e

    @staticmethod
    def _read_json(response):
        raw = response.read()
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ApiError("API の応答を解釈できませんでした。") from e

    # HTTPError を ProblemDetails の code 付き例外へ変換する。
    def _to_error(self, http_error):
        status = int(http_error.code)
        problem = self._read_problem(http_error)
        code = problem.get("code")
        message = problem.get("detail") or problem.get("title") or f"API がエラーを返しました (HTTP {status})。"
        retry_after = self._parse_retry_after(http_error.headers.get("Retry-After"))

        if status >= 500:
            # サーバー側の障害。オフラインと同じく縮退させる。
            _logger.warning("Stretheus API が %d を返しました。", status)
            return ApiOfflineError(message, status=status, code=code, retry_after=retry_after)

        return ApiError(message, status=status, code=code, retry_after=retry_after)

    @staticmethod
    def _read_problem(http_error):
        try:
            raw = http_error.read()
        except OSError:
            return {}
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _parse_retry_after(value):
        try:
            return float(value) if value else None
        except (TypeError, ValueError):
            # HTTP-date 形式は使わないため、解釈できない場合は既定の待ち時間に委ねる。
            return None
