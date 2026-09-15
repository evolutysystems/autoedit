# Stretheus API 接続 (認可コードフロー / JWT の保存とリフレッシュ) の単体テスト
# 実行: python -m unittest discover -s tests
# 要望 (StretheusAPI docs/request/resolve2.md §6):
#   ・Client ID / RedirectUri は API から取得する
#   ・JWT は DPAPI で保存し、期限の 5 分前にリフレッシュする
#   ・401 reauth_required では保存済みトークンを捨てて再ログインを促す
# 外部ネットワークは使わない。localhost に API の代役を立てて通信部分だけを見る。
import datetime
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from src.exceptions import ReauthRequiredError
from src.services import stretheus_auth
from src.services.auth_store import AuthStore
from src.services.stretheus_auth import StretheusAuth, parse_timestamp


# localhost は ::1 → 127.0.0.1 の順に試されて 1 回あたり数秒待つことがあるため、
# テストでは接続先を IPv4 で直接指定する (製品コードの挙動には影響しない)。
_HOST = "127.0.0.1"


# 空きポートを 1 つ確保して返す (redirect_uri と API の双方で使う)
def _free_port():
    with socket.socket() as sock:
        sock.bind((_HOST, 0))
        return sock.getsockname()[1]


def _iso(delta_minutes):
    moment = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=delta_minutes)
    # .NET と同じく小数秒 7 桁で返し、クライアント側の丸めも合わせて検証する
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond:06d}0" + "Z"


# API の代役。呼び出し回数と最後に受け取った本文をサーバーへ記録する。
class _FakeApiHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.server.calls.append((self.path, None, self.headers.get("Authorization")))
        if self.path == "/api/points":
            # 再ログインが必要な状態 (セッション失効 / 猶予切れ)
            self._json(401, {"title": "Authentication failed", "detail": "再ログインが必要です。",
                             "code": "reauth_required"})
            return
        if self.path == "/api/auth/twitch/authorize-params":
            self._json(200, {
                "clientId": "fake-client-id",
                "redirectUri": f"http://{_HOST}:{self.server.callback_port}/callback",
                "scopes": ["user:read:subscriptions"],
                "authorizeUrl": "http://localhost/oauth2/authorize",
            })
            return
        self._json(404, {"code": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        self.server.calls.append((self.path, body, self.headers.get("Authorization")))

        if self.path == "/api/auth/twitch/login":
            self._json(200, self._token_response("token-1"))
        elif self.path == "/api/auth/refresh":
            self._json(200, self._token_response("token-2"))
        elif self.path == "/api/auth/logout":
            self.send_response(204)
            self.end_headers()
        else:
            self._json(404, {"code": "not_found"})

    def _token_response(self, access_token):
        return {
            "accessToken": access_token,
            "tokenType": "Bearer",
            "expiresAt": _iso(120),
            "sessionExpiresAt": _iso(60 * 24 * 90),
            "user": {"id": "user-1", "twitchUserId": "12345", "displayName": "Streamer"},
        }

    def _json(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        return


class StretheusAuthTest(unittest.TestCase):

    def setUp(self):
        self._callback_port = _free_port()
        self._server = HTTPServer((_HOST, _free_port()), _FakeApiHandler)
        self._server.callback_port = self._callback_port
        self._server.calls = []
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        self._temp = tempfile.mkdtemp(prefix="stretheus-auth-test-")
        self._store = AuthStore(os.path.join(self._temp, "auth.dat"))
        self._auth = StretheusAuth(
            f"http://{_HOST}:{self._server.server_port}", timeout_sec=5, store=self._store)

        self._original_open = stretheus_auth.webbrowser.open
        stretheus_auth.webbrowser.open = self._browser_open

    def tearDown(self):
        stretheus_auth.webbrowser.open = self._original_open
        self._server.shutdown()
        self._server.server_close()
        self._store.clear()
        try:
            os.rmdir(self._temp)
        except OSError:
            pass

    # ブラウザの代役。認可 URL から state を読み、redirect_uri へ code を返す。
    def _browser_open(self, url):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual("code", query["response_type"][0])
        self.assertEqual("fake-client-id", query["client_id"][0])

        callback = query["redirect_uri"][0] + "?" + urllib.parse.urlencode(
            {"code": "auth-code", "state": query["state"][0]})
        with urllib.request.urlopen(callback, timeout=5):
            pass
        return True

    def test_login_stores_jwt_and_user(self):
        user = self._auth.login(timeout=10)

        self.assertEqual("Streamer", user["displayName"])
        self.assertTrue(self._auth.is_logged_in())
        # 保存内容は DPAPI で暗号化され、読み直しても同じ JWT が得られる
        self.assertEqual("token-1", AuthStore(self._store.path).load()["access_token"])

        path, body, _ = self._call("/api/auth/twitch/login")
        self.assertEqual("/api/auth/twitch/login", path)
        self.assertEqual("auth-code", body["code"])
        self.assertEqual(f"http://{_HOST}:{self._callback_port}/callback", body["redirectUri"])

    def test_access_token_refreshes_before_expiry(self):
        self._auth.login(timeout=10)
        self._expire_stored_token()

        # 期限の 5 分前を過ぎているため、送信前にリフレッシュが走る (§6.4 の契機 (a))
        self.assertEqual("token-2", self._auth.access_token())
        self.assertIn("/api/auth/refresh", [call[0] for call in self._server.calls])

    def test_valid_token_is_not_refreshed(self):
        self._auth.login(timeout=10)
        self._server.calls.clear()

        self.assertEqual("token-1", self._auth.access_token())
        self.assertEqual([], self._server.calls)

    def test_reauth_required_discards_stored_token(self):
        self._auth.login(timeout=10)

        with self.assertRaises(ReauthRequiredError):
            self._auth.client.get("/api/points")

        self.assertFalse(self._auth.is_logged_in())
        self.assertIsNone(AuthStore(self._store.path).load())

    def test_logout_sends_bearer_and_clears_storage(self):
        self._auth.login(timeout=10)
        self._server.calls.clear()

        self._auth.logout()

        path, _, authorization = self._call("/api/auth/logout")
        self.assertEqual("/api/auth/logout", path)
        self.assertEqual("Bearer token-1", authorization)
        self.assertFalse(self._auth.is_logged_in())

    def test_login_is_serialized_with_the_other_login(self):
        # implicit flow と同じポートを使うため、同時ログインを許さない (§6.3)
        self.assertFalse(stretheus_auth.login_in_progress())
        with stretheus_auth._LOGIN_LOCK:
            self.assertTrue(stretheus_auth.login_in_progress())

    # 代役サーバーが受け取った呼び出しのうち、指定パスの最初の 1 件を返す
    def _call(self, path):
        for call in self._server.calls:
            if call[0] == path:
                return call
        self.fail(f"{path} が呼ばれていません: {[call[0] for call in self._server.calls]}")

    # 保存済みトークンの有効期限を過去にする (リフレッシュ契機の再現)
    def _expire_stored_token(self):
        data = self._store.load()
        data["expires_at"] = _iso(-1)
        self._store.save(data)
        self._auth._data = data


class ParseTimestampTest(unittest.TestCase):

    def test_parses_dotnet_seven_digit_fraction(self):
        # .NET の DateTime は小数秒 7 桁で出力する。fromisoformat は受け付けないため丸める。
        parsed = parse_timestamp("2026-09-15T12:00:00.1234567Z")

        self.assertEqual(datetime.timezone.utc, parsed.tzinfo)
        self.assertEqual((2026, 9, 15, 12, 0, 0), parsed.timetuple()[:6])

    def test_treats_naive_value_as_utc(self):
        self.assertEqual(datetime.timezone.utc, parse_timestamp("2026-09-15T12:00:00").tzinfo)

    def test_returns_none_for_invalid_value(self):
        self.assertIsNone(parse_timestamp(""))
        self.assertIsNone(parse_timestamp("not-a-timestamp"))


if __name__ == "__main__":
    unittest.main()
