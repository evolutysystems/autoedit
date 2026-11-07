# 自動更新の取得物検証の単体テスト (sec2 S1)
# 実行: python -m unittest discover -s tests
#
# 要件:
#   ・取得先 URL は https + 既知ホストだけを許す (リダイレクト先も確かめる)
#   ・SHA-256 が一致しないインストーラは実行経路へ残さない
#   ・置き場所は毎回作る専用の一時フォルダ (固定パスの差し替えを防ぐ)
#   ・期待ハッシュを得られないリリースは更新を見送る
# 外部ネットワークは使わない。localhost に配布元の代役を立てる。
import hashlib
import http.server
import os
import threading
import unittest
from unittest import mock

from src.utils import updater

_HOST = "127.0.0.1"
_PAYLOAD = b"dummy installer payload" * 1000
_PAYLOAD_SHA256 = hashlib.sha256(_PAYLOAD).hexdigest()


# インストーラの配布元の代役。/installer で本体、/hash で .sha256 の中身を返す。
class _Handler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path.startswith("/installer"):
            body = _PAYLOAD
        elif self.path.startswith("/hash"):
            body = f"{_PAYLOAD_SHA256}  StretheusSetup.exe\n".encode("ascii")
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # テスト出力を汚さない


class _Server:

    def __enter__(self):
        self._httpd = http.server.HTTPServer((_HOST, 0), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self.port = self._httpd.server_port
        # 許可ホストの検証を通すため、テストの間だけ localhost を許す。
        # https 必須の判定も合わせて緩める (平文の代役サーバのため)。
        self._patch_hosts = mock.patch.object(
            updater, "_ALLOWED_HOSTS", frozenset({_HOST}))
        self._patch_scheme = mock.patch.object(
            updater, "_is_allowed_url",
            lambda url: str(url or "").startswith(f"http://{_HOST}:{self.port}/"))
        self._patch_hosts.start()
        self._patch_scheme.start()
        return self

    def url(self, path):
        return f"http://{_HOST}:{self.port}{path}"

    def __exit__(self, *exc):
        self._patch_scheme.stop()
        self._patch_hosts.stop()
        self._httpd.shutdown()
        self._httpd.server_close()
        return False


class AllowedUrlTest(unittest.TestCase):

    # GitHub Releases の実体とリダイレクト先だけを許す
    def test_github_hosts_are_allowed(self):
        for url in ("https://github.com/x/y/releases/download/v1/StretheusSetup.exe",
                    "https://objects.githubusercontent.com/abc",
                    "https://release-assets.githubusercontent.com/abc"):
            self.assertTrue(updater._is_allowed_url(url), url)

    # http・別ホスト・file は弾く (sec2 S1-c)
    def test_other_urls_are_rejected(self):
        for url in ("http://github.com/x/y/StretheusSetup.exe",
                    "https://evil.example.invalid/StretheusSetup.exe",
                    "https://github.com.evil.invalid/x",
                    "file:///C:/Windows/System32/calc.exe",
                    "", None):
            self.assertFalse(updater._is_allowed_url(url), url)


class CleanSha256Test(unittest.TestCase):

    # GitHub の digest 形式・sha256sum 形式・素の十六進のいずれも受ける
    def test_accepted_forms(self):
        for value in (_PAYLOAD_SHA256,
                      _PAYLOAD_SHA256.upper(),
                      "sha256:" + _PAYLOAD_SHA256,
                      f"{_PAYLOAD_SHA256}  StretheusSetup.exe\n",
                      f"  {_PAYLOAD_SHA256}  "):
            self.assertEqual(updater._clean_sha256(value), _PAYLOAD_SHA256, value)

    # 長さ違い・十六進でない・空は受け付けない
    def test_rejected_forms(self):
        for value in ("", None, "deadbeef", "z" * 64, _PAYLOAD_SHA256[:-1]):
            self.assertIsNone(updater._clean_sha256(value), value)


class DownloadInstallerTest(unittest.TestCase):

    # 期待ハッシュと一致すれば、専用の一時フォルダへ置いて返す
    def test_download_succeeds_and_isolates_directory(self):
        with _Server() as server:
            path = updater.download_installer(server.url("/installer"), _PAYLOAD_SHA256)
            try:
                self.assertTrue(os.path.isfile(path))
                with open(path, "rb") as f:
                    self.assertEqual(f.read(), _PAYLOAD)
                # 固定の %TEMP% 直下ではなく、毎回作る専用フォルダに置く (S1-b)
                parent = os.path.basename(os.path.dirname(path))
                self.assertTrue(parent.startswith("stretheus_update_"), parent)
            finally:
                import shutil
                shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    # ハッシュが違えば例外にし、**取得物を残さない**
    def test_hash_mismatch_removes_file(self):
        other = hashlib.sha256(b"something else").hexdigest()
        with _Server() as server:
            with self.assertRaises(ValueError) as cm:
                updater.download_installer(server.url("/installer"), other)
            self.assertIn("ハッシュ", str(cm.exception))
        # 一時フォルダごと消えている (実行経路に偽物を残さない)
        leftovers = [n for n in os.listdir(__import__("tempfile").gettempdir())
                     if n.startswith("stretheus_update_")]
        self.assertEqual(leftovers, [])

    # 期待ハッシュが無ければ、通信する前に中止する
    def test_missing_hash_aborts(self):
        for value in (None, "", "   ", "not-a-hash"):
            with self.assertRaises(ValueError):
                updater.download_installer("https://github.com/x/StretheusSetup.exe", value)

    # 想定外のホストは取得しない
    def test_disallowed_url_aborts(self):
        with self.assertRaises(ValueError) as cm:
            updater.download_installer("https://evil.example.invalid/x.exe", _PAYLOAD_SHA256)
        self.assertIn("配布元", str(cm.exception))

    # 進捗は (取得済み, 総数) で通知される
    def test_progress_reports_bytes(self):
        seen = []
        with _Server() as server:
            path = updater.download_installer(
                server.url("/installer"), _PAYLOAD_SHA256,
                on_progress=lambda done, total: seen.append((done, total)))
        import shutil
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], len(_PAYLOAD))
        self.assertEqual(seen[-1][1], len(_PAYLOAD))


class ExpectedSha256Test(unittest.TestCase):

    # GitHub API が digest を返すならそれを使う (追加の通信をしない)
    def test_digest_is_preferred(self):
        assets = [{"name": "StretheusSetup.exe", "digest": "sha256:" + _PAYLOAD_SHA256}]
        self.assertEqual(updater._resolve_expected_sha256(assets), _PAYLOAD_SHA256)

    # digest が無ければ .sha256 アセットを取りに行く
    def test_sha256_asset_is_used(self):
        with _Server() as server:
            assets = [{"name": "StretheusSetup.exe", "browser_download_url": server.url("/installer")},
                      {"name": "StretheusSetup.exe.sha256", "browser_download_url": server.url("/hash")}]
            self.assertEqual(updater._resolve_expected_sha256(assets), _PAYLOAD_SHA256)

    # どちらも無ければ None (呼び出し側が更新を見送る)
    def test_missing_everything_returns_none(self):
        assets = [{"name": "StretheusSetup.exe", "browser_download_url": "https://github.com/x"}]
        self.assertIsNone(updater._resolve_expected_sha256(assets))


class FindUpdateTest(unittest.TestCase):

    def setUp(self):
        self._frozen = mock.patch.object(updater.sys, "frozen", True, create=True)
        self._frozen.start()
        self.addCleanup(self._frozen.stop)

    # 期待ハッシュが取れたときだけ更新を知らせる
    def test_update_with_hash_is_reported(self):
        latest = {"tag": "v9.9.9", "installer_url": "https://github.com/x/StretheusSetup.exe",
                  "sha256": _PAYLOAD_SHA256}
        with mock.patch.object(updater, "check_latest", return_value=latest):
            self.assertEqual(updater.find_update("1.8.2"), latest)

    # 期待ハッシュが無いリリースは見送る (検証できないものを実行しない / sec2 S1)
    def test_update_without_hash_is_skipped(self):
        latest = {"tag": "v9.9.9", "installer_url": "https://github.com/x/StretheusSetup.exe",
                  "sha256": None}
        with mock.patch.object(updater, "check_latest", return_value=latest):
            self.assertIsNone(updater.find_update("1.8.2"))

    # 新しくないタグは更新しない (従来どおり)
    def test_same_version_is_skipped(self):
        latest = {"tag": "v1.8.2", "installer_url": "https://github.com/x/StretheusSetup.exe",
                  "sha256": _PAYLOAD_SHA256}
        with mock.patch.object(updater, "check_latest", return_value=latest):
            self.assertIsNone(updater.find_update("1.8.2"))

    # 非凍結 (ソース実行) では更新しない
    def test_source_run_is_skipped(self):
        with mock.patch.object(updater.sys, "frozen", False, create=True):
            self.assertIsNone(updater.find_update("1.0.0"))


if __name__ == "__main__":
    unittest.main()
