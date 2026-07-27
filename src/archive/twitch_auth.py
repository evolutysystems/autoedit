# Twitch ログイン (OAuth インプリシットフロー) と所有判定 (flow17 R3 / resolve17 §4.3.0)
# twitch-dl 自体に対話ログインが無いため、本アプリ側で OAuth を実装しユーザーアクセストークンを得る。
# 取得したトークンで Helix を叩き「入力 VOD が自分の所有か」を機械判定する (owner_only)。
#
# インプリシットフロー(response_type=token)採用の理由: **Client-ID だけでログインできる**
# (Client-Secret 不要)。ログインボタン押下 → 既定ブラウザで Twitch 認証ページが開く →
# 許可するとトークンが URL フラグメントで返る → localhost の簡易サーバが JS 経由で受け取る。
# 依存は標準ライブラリのみ (http.server / urllib / webbrowser / secrets)。requests/torch は使わない。
# 機微情報 (トークン) はローカルのファイルにのみ保存し、ログには出力しない (resolve17 §7)。
import json
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request

from ..exceptions import TwitchError
from ..utils.logger import get_logger

_logger = get_logger(__name__)

_AUTH_BASE = "https://id.twitch.tv/oauth2"
_HELIX_BASE = "https://api.twitch.tv/helix"
_HTTP_TIMEOUT = 20

# 認可完了後にブラウザへ返す簡易ページ (日本語)。
_DONE_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Stretheus</title></head>"
    "<body style='font-family:sans-serif;padding:2em'>"
    "<h2>Twitch ログインが完了しました</h2>"
    "<p>このタブを閉じてアプリに戻ってください。</p></body></html>"
)
_ERROR_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Stretheus</title></head>"
    "<body style='font-family:sans-serif;padding:2em'>"
    "<h2>Twitch ログインに失敗しました</h2>"
    "<p>アプリに戻って再度お試しください。</p></body></html>"
)
# トークンは URL フラグメント(#access_token=...)で返るためサーバへ送られない。
# この relay ページの JS がフラグメントを読み取り /capture?... へ渡し直してサーバに届ける。
_RELAY_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Stretheus</title></head>"
    "<body style='font-family:sans-serif;padding:2em'><p>ログイン処理中…</p>"
    "<script>"
    "var h = window.location.hash ? window.location.hash.substring(1) : '';"
    "if (h) { window.location.replace('/capture?' + h); }"
    "else { document.body.innerHTML = '<p>トークンが取得できませんでした。</p>'; }"
    "</script></body></html>"
)


# ローカル redirect を受けてアクセストークンを捕捉する簡易 HTTP ハンドラ。
#   GET /        : フラグメント中継用 relay ページを返す (エラーはクエリで来るためここで捕捉も)
#   GET /capture : relay が付け直したクエリから access_token / state を捕捉する
def _make_handler():
    from http.server import BaseHTTPRequestHandler

    class _TokenHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/capture":
                self._handle_capture(params)
                return
            # 認可拒否等のエラーはフラグメントでなくクエリで来る場合がある
            if params.get("error"):
                self.server.auth_error = (params.get("error_description")
                                          or params.get("error"))[0]
                self._respond(_ERROR_HTML)
                return
            # 通常はフラグメントにトークンがあるため relay ページを返す
            self._respond(_RELAY_HTML)

        def _handle_capture(self, params):
            state = params.get("state", [None])[0]
            expected = getattr(self.server, "expected_state", None)
            if params.get("error"):
                self.server.auth_error = (params.get("error_description")
                                          or params.get("error"))[0]
            elif expected and state != expected:
                self.server.auth_error = "state 不一致 (CSRF 検出)。再度お試しください。"
            else:
                token = params.get("access_token", [None])[0]
                if token:
                    self.server.auth_token = token
                    self.server.auth_scope = params.get("scope", [""])[0]
                else:
                    self.server.auth_error = "access_token を取得できませんでした。"
            self._respond(_DONE_HTML if self.server.auth_token else _ERROR_HTML)

        def _respond(self, html):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

        # アクセスログを標準エラーへ出さない (GUI アプリのため)
        def log_message(self, *args):
            return

    return _TokenHandler


# Twitch OAuth ログインとトークン管理・Helix 呼び出しをまとめるクラス
class TwitchAuth:

    def __init__(self, client_id, client_secret="", redirect_port=3737, token_path=None,
                 scopes=None):
        self._client_id = client_id or ""
        # client_secret はインプリシットフローでは不要 (後方互換で受け取るが未使用)。
        self._client_secret = client_secret or ""
        self._redirect_port = int(redirect_port or 3737)
        self._token_path = token_path
        # 所有判定・自VOD一覧は公開情報のため追加スコープ不要 (空スコープでよい)。
        self._scopes = scopes if scopes is not None else []
        self._token = self._read_token_file()

    # Twitch 開発者コンソールに登録する redirect と完全一致させること。
    @property
    def redirect_uri(self):
        return f"http://localhost:{self._redirect_port}"

    # ---- トークンの永続化 (ローカル限定) ---------------------------------
    def _read_token_file(self):
        if not self._token_path or not os.path.isfile(self._token_path):
            return None
        try:
            with open(self._token_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _write_token_file(self):
        if not self._token_path or not self._token:
            return
        try:
            os.makedirs(os.path.dirname(self._token_path), exist_ok=True)
            with open(self._token_path, "w", encoding="utf-8") as f:
                json.dump(self._token, f)
        except OSError as e:  # 保存失敗は致命としない (今回セッションは継続可能)
            _logger.warning("Twitch トークン保存に失敗: %s", e)

    def is_logged_in(self):
        return bool(self._token and self._token.get("access_token"))

    def logout(self):
        self._token = None
        try:
            if self._token_path and os.path.isfile(self._token_path):
                os.remove(self._token_path)
        except OSError:
            pass

    # 認可 URL を組み立てる (state はCSRF対策の照合用)
    def build_authorize_url(self, state):
        params = {
            "client_id": self._client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "token",          # インプリシットフロー (Client-Secret 不要)
            "scope": " ".join(self._scopes),
            "state": state,
            "force_verify": "true",
        }
        return f"{_AUTH_BASE}/authorize?" + urllib.parse.urlencode(params)

    # ---- OAuth インプリシットフロー -------------------------------------
    # ブラウザで認可 → localhost でトークン受信。成功で True。
    # Client-ID のみ必須。timeout 秒以内に許可されなければ TwitchError。
    def login(self, timeout=180, open_browser=True):
        if not self._client_id:
            raise TwitchError(
                "Twitch の Client-ID が未設定です。"
                "設定(setting.json)の archive.auth.client_id に登録してください。")

        from http.server import HTTPServer
        import webbrowser

        # 1) ローカル受信サーバを起動
        try:
            httpd = HTTPServer(("localhost", self._redirect_port), _make_handler())
        except OSError as e:
            raise TwitchError(f"ローカル受信ポート {self._redirect_port} を開けません: {e}")
        httpd.auth_token = None
        httpd.auth_scope = ""
        httpd.auth_error = None
        httpd.expected_state = secrets.token_urlsafe(16)
        httpd.timeout = timeout

        # 2) 既定ブラウザで Twitch 認証ページを開く
        auth_url = self.build_authorize_url(httpd.expected_state)
        _logger.info("Twitch 認証ページを開きます (ポート%d)", self._redirect_port)
        if open_browser:
            webbrowser.open(auth_url)

        # 3) relay(/) → /capture の2リクエストを処理し、トークン受信まで回す
        result = {"done": False}

        def _serve():
            while (not result["done"]
                   and httpd.auth_token is None and httpd.auth_error is None):
                httpd.handle_request()
            result["done"] = True

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        t.join(timeout)
        result["done"] = True
        try:
            httpd.server_close()
        except OSError:
            pass

        if httpd.auth_error:
            raise TwitchError(f"Twitch 認可に失敗しました: {httpd.auth_error}")
        if not httpd.auth_token:
            raise TwitchError("Twitch 認可がタイムアウトしました (ブラウザで許可されませんでした)。")

        # 4) トークンを保存 (インプリシットフローは refresh_token 無し)
        self._token = {
            "access_token": httpd.auth_token,
            "scope": httpd.auth_scope,
            "token_type": "bearer",
        }
        self._write_token_file()
        _logger.info("Twitch ログイン成功")
        return True

    # ---- Helix API -------------------------------------------------------
    # Helix GET (Bearer + Client-Id)。401 はトークン失効として logout し再ログインを促す
    # (インプリシットフローは refresh_token を持たないため自動更新はできない)。
    def _helix_get(self, path, query=None):
        if not self.is_logged_in():
            raise TwitchError("Twitch にログインしていません。")
        url = f"{_HELIX_BASE}/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {self._token['access_token']}")
        req.add_header("Client-Id", self._client_id)
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self.logout()
                raise TwitchError("Twitch トークンが失効しました。再度ログインしてください。")
            raise TwitchError(f"Twitch API 呼び出しに失敗しました (HTTP {e.code})。")
        except (urllib.error.URLError, ValueError) as e:
            raise TwitchError(f"Twitch API 呼び出しに失敗しました: {e}")

    # ログイン中ユーザー情報 {id, login, display_name} を返す
    def get_self(self):
        data = self._helix_get("users").get("data", [])
        if not data:
            raise TwitchError("ログインユーザー情報を取得できませんでした。")
        u = data[0]
        return {"id": u.get("id"), "login": u.get("login"),
                "display_name": u.get("display_name")}

    # VOD の所有者 user_id を返す (存在しなければ None)
    def get_video_owner_id(self, video_id):
        data = self._helix_get("videos", {"id": video_id}).get("data", [])
        if not data:
            return None
        return data[0].get("user_id")

    # 入力 VOD がログインユーザーの所有かを判定する
    def is_own_video(self, video_id):
        me = self.get_self()
        owner = self.get_video_owner_id(video_id)
        return owner is not None and str(owner) == str(me["id"])

    # 自分の VOD 一覧 [{id,title,url,duration,created_at}] を返す (最大 first 件)
    def list_own_videos(self, first=20):
        me = self.get_self()
        data = self._helix_get(
            "videos", {"user_id": me["id"], "first": int(first), "type": "archive"}
        ).get("data", [])
        out = []
        for v in data:
            out.append({
                "id": v.get("id"),
                "title": v.get("title", ""),
                "url": v.get("url", ""),
                "duration": v.get("duration", ""),
                "created_at": v.get("created_at", ""),
            })
        return out
