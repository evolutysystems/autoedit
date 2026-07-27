# Twitch ログイン (OAuth 認可コードフロー) と所有判定 (flow17 R3 / resolve17 §4.3.0)
# twitch-dl 自体に対話ログインが無いため、本アプリ側で OAuth を実装しユーザーアクセストークンを得る。
# 取得したトークンで Helix を叩き「入力 VOD が自分の所有か」を機械判定する (owner_only)。
#
# 依存は標準ライブラリのみ (http.server / urllib / webbrowser)。requests/torch は使わない。
# 機微情報 (トークン) はローカルのファイルにのみ保存し、ログには出力しない (resolve17 §7)。
import json
import os
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


# ローカル redirect を受けて認可コード(code)を1回だけ捕捉する簡易 HTTP ハンドラ
def _make_handler():
    from http.server import BaseHTTPRequestHandler

    class _CodeHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            # server 属性へ結果を渡す (code もしくは error)
            self.server.auth_code = params.get("code", [None])[0]
            err = params.get("error_description", params.get("error", [None]))[0]
            self.server.auth_error = err
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_DONE_HTML.encode("utf-8"))

        # アクセスログを標準エラーへ出さない (GUI アプリのため)
        def log_message(self, *args):
            return

    return _CodeHandler


# Twitch OAuth ログインとトークン管理・Helix 呼び出しをまとめるクラス
class TwitchAuth:

    def __init__(self, client_id, client_secret, redirect_port=3737, token_path=None,
                 scopes=None):
        self._client_id = client_id or ""
        self._client_secret = client_secret or ""
        self._redirect_port = int(redirect_port or 3737)
        self._token_path = token_path
        # 所有判定・自VOD一覧に必要な最小スコープ (公開 VOD 情報は追加スコープ不要)。
        self._scopes = scopes if scopes is not None else []
        self._token = self._read_token_file()

    # ---- トークンの永続化 (ローカル限定) ---------------------------------
    @property
    def redirect_uri(self):
        # Twitch 開発者コンソールに登録する redirect と完全一致させること。
        return f"http://localhost:{self._redirect_port}"

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

    # ---- OAuth 認可コードフロー -----------------------------------------
    # ブラウザで認可 → localhost で code 受信 → token 交換。成功で True。
    # timeout 秒以内に認可されなければ TwitchError。
    def login(self, timeout=180, open_browser=True):
        if not self._client_id or not self._client_secret:
            raise TwitchError(
                "Twitch の Client-ID / Client-Secret が未設定です。"
                "設定(setting.json)の archive.auth に登録してください。")

        from http.server import HTTPServer
        import webbrowser

        # 1) ローカル受信サーバを起動
        try:
            httpd = HTTPServer(("localhost", self._redirect_port), _make_handler())
        except OSError as e:
            raise TwitchError(f"ローカル受信ポート {self._redirect_port} を開けません: {e}")
        httpd.auth_code = None
        httpd.auth_error = None
        httpd.timeout = timeout

        # 2) 認可 URL をブラウザで開く
        params = {
            "client_id": self._client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self._scopes),
        }
        auth_url = f"{_AUTH_BASE}/authorize?" + urllib.parse.urlencode(params)
        _logger.info("Twitch 認可ページを開きます (ポート%d)", self._redirect_port)
        if open_browser:
            webbrowser.open(auth_url)

        # 3) 1リクエスト分だけ処理 (別スレッドで timeout 監視)
        result = {"done": False}

        def _serve():
            # code か error を受け取るまで handle_request を回す
            while not result["done"] and httpd.auth_code is None and httpd.auth_error is None:
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
            raise TwitchError(f"Twitch 認可が拒否されました: {httpd.auth_error}")
        if not httpd.auth_code:
            raise TwitchError("Twitch 認可がタイムアウトしました (ブラウザで許可されませんでした)。")

        # 4) code → token 交換
        self._token = self._exchange_code(httpd.auth_code)
        self._write_token_file()
        _logger.info("Twitch ログイン成功")
        return True

    def _exchange_code(self, code):
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        }
        return self._post_token(data)

    def _refresh_token(self):
        refresh = (self._token or {}).get("refresh_token")
        if not refresh:
            return False
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        }
        try:
            self._token = self._post_token(data)
            self._write_token_file()
            return True
        except TwitchError:
            return False

    def _post_token(self, data):
        body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(f"{_AUTH_BASE}/token", data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise TwitchError(f"トークン取得に失敗しました (HTTP {e.code})。")
        except (urllib.error.URLError, ValueError) as e:
            raise TwitchError(f"トークン取得に失敗しました: {e}")
        if not payload.get("access_token"):
            raise TwitchError("トークン応答に access_token がありません。")
        return payload

    # ---- Helix API -------------------------------------------------------
    # Helix GET (Bearer + Client-Id)。401 のときトークンを1回リフレッシュして再試行する。
    def _helix_get(self, path, query=None, _retry=True):
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
            if e.code == 401 and _retry and self._refresh_token():
                return self._helix_get(path, query, _retry=False)
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
