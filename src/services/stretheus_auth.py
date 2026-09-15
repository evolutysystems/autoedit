# Stretheus API へのログイン (OAuth 認可コードフロー) と JWT の管理
# (StretheusAPI docs/request/resolve2.md §2 / §6.3〜§6.5)
#
# アーカイブ取得用の implicit flow (archive/twitch_auth.py) とは役割が異なる。
#   implicit flow  … Twitch Helix を直接叩くためのアクセストークン。API を経由しない。
#   code flow (本) … Stretheus API の JWT。ポイント制・サブスク判定の権威はサーバー側にある。
# 両者の間でトークンを受け渡すことはしない。Twitch の RefreshToken はサーバーだけが持つ。
#
# Client ID / RedirectUri / scope はクライアントに持たせず、API の
# GET /api/auth/twitch/authorize-params から取得する (§2.4)。3 箇所で値を一致させる
# 運用をやめ、ずれの再発を防ぐため。
import datetime
import secrets
import threading
import urllib.parse
import webbrowser

from ..exceptions import ApiError, AutoEditError, ReauthRequiredError
from ..utils.logger import get_logger
from .api_client import ApiClient, CODE_INVALID_TOKEN
from .auth_store import AuthStore

_logger = get_logger(__name__)

_AUTHORIZE_PARAMS_PATH = "/api/auth/twitch/authorize-params"
_LOGIN_PATH = "/api/auth/twitch/login"
_REFRESH_PATH = "/api/auth/refresh"
_LOGOUT_PATH = "/api/auth/logout"

# 有効期限のこれだけ手前になったら送信前にリフレッシュする (§6.4)
_REFRESH_MARGIN = datetime.timedelta(minutes=5)

# implicit flow と同じポートを使うため、2 つのログインを同時に走らせない (§6.3)。
# 呼び出し側 (UI) は login_in_progress() を見てボタンを無効化する。
_LOGIN_LOCK = threading.Lock()

_DONE_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Stretheus</title></head>"
    "<body style='font-family:sans-serif;padding:2em'>"
    "<h2>Stretheus へのログインが完了しました</h2>"
    "<p>このタブを閉じてアプリに戻ってください。</p></body></html>"
)
_ERROR_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Stretheus</title></head>"
    "<body style='font-family:sans-serif;padding:2em'>"
    "<h2>Stretheus へのログインに失敗しました</h2>"
    "<p>アプリに戻って再度お試しください。</p></body></html>"
)


# 別のログイン (本モジュール) が進行中なら True。
def login_in_progress():
    return _LOGIN_LOCK.locked()


# API が返す UTC の ISO 8601 文字列を datetime へ変換する。
# .NET は小数秒を 7 桁で出すことがあり fromisoformat が受け付けないため、6 桁へ丸める。
def parse_timestamp(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    head, sep, tail = text.partition(".")
    if sep:
        digits = ""
        for char in tail:
            if not char.isdigit():
                break
            digits += char
        text = head + "." + digits[:6] + tail[len(digits):]

    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        _logger.warning("API の日時を解釈できませんでした。")
        return None

    # タイムゾーンの無い値は UTC とみなす (API は常に UTC を返す)。
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


# 認可コードを受け取るローカル HTTP ハンドラ。
# redirect_uri のパス (既定 /callback) だけを処理し、それ以外は 404 とする。
def _make_handler():
    from http.server import BaseHTTPRequestHandler

    class _CodeHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != self.server.callback_path:
                self.send_response(404)
                self.end_headers()
                return

            params = urllib.parse.parse_qs(parsed.query)
            if params.get("error"):
                # 利用者が認可を拒否した場合もここへ来る。
                self.server.auth_error = (params.get("error_description") or params.get("error"))[0]
            else:
                state = params.get("state", [""])[0]
                if not secrets.compare_digest(state, self.server.expected_state):
                    self.server.auth_error = "state 不一致 (CSRF 検出)。再度お試しください。"
                else:
                    code = params.get("code", [""])[0]
                    if code:
                        self.server.auth_code = code
                    else:
                        self.server.auth_error = "認可コードを取得できませんでした。"

            self._respond(_DONE_HTML if self.server.auth_code else _ERROR_HTML)

        def _respond(self, html):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

        # アクセスログを標準エラーへ出さない (GUI アプリのため)
        def log_message(self, *args):
            return

    return _CodeHandler


# Stretheus API のログイン・リフレッシュ・ログアウトと、JWT の保持を担当する。
# ApiClient から JWT の供給元として参照される (access_token / refresh / on_reauth_required)。
class StretheusAuth:

    def __init__(self, base_url, timeout_sec=15, store=None, client=None):
        self._client = client or ApiClient(base_url, timeout_sec)
        self._client.set_token_provider(self)
        self._store = store or AuthStore()
        self._refresh_lock = threading.Lock()
        self._data = self._store.load()

    @property
    def client(self):
        return self._client

    def is_logged_in(self):
        return bool(self._data and self._data.get("access_token"))

    # ログイン中のユーザー情報 (id / twitchUserId / displayName)。未ログインなら None。
    def user(self):
        return (self._data or {}).get("user")

    # リフレッシュ可能な最終期限。これを過ぎると再ログインが必要 (§2.7)。
    def session_expires_at(self):
        return parse_timestamp((self._data or {}).get("session_expires_at"))

    # ---- ApiClient から呼ばれる JWT の供給 --------------------------------
    # 送信前に期限が近ければリフレッシュしてから返す (§6.4 の契機 (a))。
    def access_token(self, refresh_if_needed=True):
        if not self.is_logged_in():
            return None
        if refresh_if_needed and self._needs_refresh(self._data):
            self.refresh()
        return (self._data or {}).get("access_token")

    # 保存済みトークンでリフレッシュする。成功で True。
    # 並行リフレッシュは §2.7 の再利用検知でセッションごと失効するため直列化する。
    def refresh(self):
        with self._refresh_lock:
            current = self._data
            if not current or not current.get("access_token"):
                return False

            # ロック待ちの間に他スレッドが更新済みなら、何もしない。
            latest = self._store.load()
            if latest and latest.get("access_token") and latest.get("access_token") != current.get("access_token"):
                self._data = latest
                if not self._needs_refresh(latest):
                    return True
                current = latest

            try:
                response = self._client.request(
                    "POST", _REFRESH_PATH,
                    body={"accessToken": current.get("access_token")},
                    authenticated=False, retry_auth=False)
            except ReauthRequiredError:
                self._discard()
                return False
            except ApiError as e:
                # 署名が合わない (接続先を変えた・鍵が変わった) トークンは、
                # 何度リフレッシュしても回復しない。保持し続けず捨てる。
                if e.code == CODE_INVALID_TOKEN:
                    _logger.warning("保持している JWT を検証できませんでした。再ログインが必要です。")
                    self._discard()
                    return False

                _logger.warning("JWT のリフレッシュに失敗しました: %s", e)
                return False

            self._apply(response)
            return True

    # 401 reauth_required を受けた。保存済みトークンを捨て、再ログインを促す状態にする。
    def on_reauth_required(self):
        self._discard()

    # ---- ログイン / ログアウト -------------------------------------------
    # 認可コードフローでログインする。成功でユーザー情報を返す。
    # timeout 秒以内にブラウザで許可されなければ AutoEditError。
    def login(self, timeout=180, open_browser=True):
        if not _LOGIN_LOCK.acquire(blocking=False):
            raise AutoEditError("別のログインが進行中です。完了してから実行してください。")
        try:
            params = self._client.request("GET", _AUTHORIZE_PARAMS_PATH, authenticated=False)
            redirect_uri = str((params or {}).get("redirectUri") or "")
            client_id = str((params or {}).get("clientId") or "")
            if not redirect_uri or not client_id:
                raise AutoEditError("API から認可パラメーターを取得できませんでした。")

            code = self._receive_code(params, redirect_uri, timeout, open_browser)
            response = self._client.request(
                "POST", _LOGIN_PATH,
                body={"code": code, "redirectUri": redirect_uri},
                authenticated=False)

            self._apply(response)
            _logger.info("Stretheus へログインしました。")
            return self.user()
        finally:
            _LOGIN_LOCK.release()

    # サーバー側の Twitch 連携とログインセッションを失効させ、ローカルの JWT も削除する。
    # 通信できなくてもローカルは必ず削除する。
    def logout(self):
        try:
            if self.is_logged_in():
                self._client.request("POST", _LOGOUT_PATH)
        except (ApiError, AutoEditError) as e:
            _logger.warning("ログアウトの通知に失敗しました: %s", e)
        finally:
            self._discard()

    # ---- 内部 ------------------------------------------------------------
    # ブラウザで認可させ、localhost で認可コードを受け取る。
    def _receive_code(self, params, redirect_uri, timeout, open_browser):
        from http.server import HTTPServer

        parsed = urllib.parse.urlparse(redirect_uri)
        port = parsed.port or 80
        callback_path = parsed.path or "/"

        try:
            httpd = HTTPServer(("localhost", port), _make_handler())
        except OSError as e:
            # implicit flow のログインが動いている場合もここに来る (§6.3)。
            raise AutoEditError(f"ローカル受信ポート {port} を開けません: {e}")

        httpd.auth_code = None
        httpd.auth_error = None
        httpd.expected_state = secrets.token_urlsafe(32)
        httpd.callback_path = callback_path
        httpd.timeout = timeout

        def _serve():
            while httpd.auth_code is None and httpd.auth_error is None:
                httpd.handle_request()

        # リダイレクトが返ってくる前に受け口を動かしておく (先にブラウザを開くと、
        # 接続は受け付けても応答できず、ブラウザ側がタイムアウトしうる)。
        worker = threading.Thread(target=_serve, daemon=True)
        worker.start()

        authorize_url = self._build_authorize_url(params, redirect_uri, httpd.expected_state)
        _logger.info("Stretheus のログインページを開きます (ポート%d)", port)
        if open_browser:
            webbrowser.open(authorize_url)

        worker.join(timeout)
        try:
            httpd.server_close()
        except OSError:
            pass

        if httpd.auth_error:
            raise AutoEditError(f"Stretheus の認可に失敗しました: {httpd.auth_error}")
        if not httpd.auth_code:
            raise AutoEditError("Stretheus の認可がタイムアウトしました (ブラウザで許可されませんでした)。")

        return httpd.auth_code

    @staticmethod
    def _build_authorize_url(params, redirect_uri, state):
        scopes = [str(scope) for scope in (params.get("scopes") or []) if str(scope)]
        query = {
            "client_id": str(params.get("clientId") or ""),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            # 別アカウントへ切り替えられるよう、毎回同意画面を出す。
            "force_verify": "true",
        }
        authorize_url = str(params.get("authorizeUrl") or "https://id.twitch.tv/oauth2/authorize")
        return authorize_url + "?" + urllib.parse.urlencode(query)

    # ログイン / リフレッシュの応答を保存する。
    def _apply(self, response):
        response = response or {}
        if not response.get("accessToken"):
            raise ApiError("API から JWT を取得できませんでした。")

        self._data = {
            "access_token": response.get("accessToken"),
            "expires_at": response.get("expiresAt"),
            "session_expires_at": response.get("sessionExpiresAt"),
            "user": response.get("user"),
        }
        self._store.save(self._data)

    def _discard(self):
        self._data = None
        self._store.clear()

    @staticmethod
    def _needs_refresh(data):
        expires_at = parse_timestamp((data or {}).get("expires_at"))
        if expires_at is None:
            # 期限が分からないトークンは、使う前に更新しておく。
            return True
        return expires_at - _REFRESH_MARGIN <= datetime.datetime.now(datetime.timezone.utc)
