# 自動アップデートモジュール (request_autoupdate.md §5)
# GitHub Releases から最新版を確認し、新版があればインストーラを取得・起動する。
# 通信は標準ライブラリ urllib のみ (新規依存を増やさない)。失敗時は静かにスキップする。
#
# 取得物の検証 (sec2 S1):
#   ・取得先 URL は https + 既知のホストだけを許す (リダイレクト先も確かめる)
#   ・SHA-256 が一致しないインストーラは実行しない
#   ・置き場所は毎回作る専用の一時フォルダ (固定パスの差し替えを防ぐ)
#   ・ダウンロードにタイムアウトを設ける
# 期待ハッシュを得られないリリースは**更新を見送る** (検証できないものを実行しない)。
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

from .logger import get_logger

_logger = get_logger(__name__)

# 配布元リポジトリ (installer/AutoEdit.iss の RepoOwner/RepoName と一致させる)
_REPO_OWNER = "evolutysystems"
_REPO_NAME = "autoedit"
# インストーラのアセット名 (installer/AutoEdit.iss の OutputBaseFilename に対応)
_INSTALLER_ASSET = "StretheusSetup.exe"
# 期待ハッシュを載せるアセット名 (GitHub API が digest を返さない場合に使う)
_INSTALLER_SHA256_ASSET = _INSTALLER_ASSET + ".sha256"
_API_LATEST = f"https://api.github.com/repos/{_REPO_OWNER}/{_REPO_NAME}/releases/latest"
_HTTP_TIMEOUT_SEC = 5

# インストーラの取得を許可するホスト (GitHub Releases の実体と、そのリダイレクト先)。
# https 以外・ここに無いホストは弾く。配布元を変えるときはここも直すこと。
_ALLOWED_HOSTS = frozenset({
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})
# 本体 (数百 MB) の取得に許す時間。応答が止まったまま固まるのを防ぐ。
_DOWNLOAD_TIMEOUT_SEC = 300
# 取得しながらハッシュを計算する単位
_CHUNK_BYTES = 256 * 1024
# SHA-256 の十六進表記の長さ
_SHA256_HEX_LEN = 64

_USER_AGENT = "AutoEdit-Updater"


# "x.y.z" を整数タプル化する (v 接頭辞・前後空白を除去。数値化不能な部分は 0)
def _parse_version(text):
    text = (text or "").strip().lstrip("vV")
    parts = []
    for p in text.split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts) if parts else (0,)


# remote が local より新しいバージョンなら True
def is_newer(remote, local):
    return _parse_version(remote) > _parse_version(local)


# 取得先 URL が想定どおりか確かめる (sec2 S1-c)。
# https 以外・想定外ホストは弾く。リダイレクト後の URL も同じ関数で確かめる。
def _is_allowed_url(url):
    parsed = urllib.parse.urlsplit(url or "")
    return parsed.scheme == "https" and parsed.hostname in _ALLOWED_HOSTS


# 十六進 64 文字の SHA-256 なら小文字で返す。そうでなければ None。
def _clean_sha256(value):
    text = str(value or "").strip().lower()
    # GitHub API の digest は "sha256:<hex>" 形式で返る
    if text.startswith("sha256:"):
        text = text[len("sha256:"):].strip()
    # sha256sum 形式 ("<hex>  ファイル名") の 1 列目を拾う
    text = text.split()[0] if text.split() else ""
    if len(text) != _SHA256_HEX_LEN:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return text


# 期待ハッシュを載せたアセット (.sha256) を取得して中身を返す。失敗時は None。
def _fetch_sha256_asset(url):
    if not _is_allowed_url(url):
        _logger.warning("ハッシュの取得先が想定外のため無視します: %s", url)
        return None
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as res:
            if not _is_allowed_url(res.geturl()):
                _logger.warning("ハッシュの取得が想定外へリダイレクトされました。")
                return None
            # ハッシュのテキストは数十バイト。桁違いに大きければ読まない。
            raw = res.read(4096)
    except Exception as e:  # noqa: BLE001 (取得失敗時は更新を見送る)
        _logger.info("ハッシュの取得に失敗: %s", e)
        return None
    return _clean_sha256(raw.decode("utf-8", "replace"))


# リリース情報から期待ハッシュを決める (sec2 S1-a)。
#   1) アセットの digest (GitHub が付ける "sha256:<hex>")
#   2) 併置した <インストーラ名>.sha256 アセットの中身
# どちらも無ければ None を返し、呼び出し側が更新を見送る。
def _resolve_expected_sha256(assets):
    for asset in assets:
        if asset.get("name") == _INSTALLER_ASSET:
            digest = _clean_sha256(asset.get("digest"))
            if digest:
                return digest
            break

    for asset in assets:
        if asset.get("name") == _INSTALLER_SHA256_ASSET:
            return _fetch_sha256_asset(asset.get("browser_download_url"))

    return None


# 最新リリースの {"tag", "installer_url", "sha256"} を返す。失敗時は None (呼び出し側でスキップ)。
def check_latest():
    req = urllib.request.Request(
        _API_LATEST,
        headers={
            "User-Agent": _USER_AGENT,  # GitHub API はUA必須
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as res:
            data = json.loads(res.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 (通信失敗時は更新をスキップ)
        _logger.info("更新チェックをスキップ (取得失敗): %s", e)
        return None

    tag = data.get("tag_name", "")
    assets = [a for a in data.get("assets", []) if isinstance(a, dict)]
    # アセットから StretheusSetup.exe を探す。無ければタグからURLを組み立てる。
    url = None
    for asset in assets:
        if asset.get("name") == _INSTALLER_ASSET:
            url = asset.get("browser_download_url")
            break
    if not url and tag:
        url = (f"https://github.com/{_REPO_OWNER}/{_REPO_NAME}"
               f"/releases/download/{tag}/{_INSTALLER_ASSET}")
    return {"tag": tag, "installer_url": url, "sha256": _resolve_expected_sha256(assets)}


# 更新が必要なら latest 情報を返す。凍結ビルドでない/最新/失敗時は None。
def find_update(current_version):
    # 開発(ソース)実行では自身を更新しない (PyInstaller 凍結時のみ有効)
    if not getattr(sys, "frozen", False):
        _logger.info("非凍結実行のため更新チェックをスキップ")
        return None
    latest = check_latest()
    if not latest or not latest.get("installer_url"):
        return None
    if not is_newer(latest["tag"], current_version):
        return None

    # 期待ハッシュが無いリリースは検証できないため更新しない (sec2 S1)。
    # リリース側の添え忘れなので、気づけるように警告で残す
    # (利用者には何も出さない。更新が無いときと同じ扱いにする)。
    if not latest.get("sha256"):
        _logger.warning(
            "更新 %s には %s も digest もありません。検証できないため更新を見送ります "
            "(リリース手順 HowToRelease §8 を確認してください)。",
            latest.get("tag"), _INSTALLER_SHA256_ASSET)
        return None

    _logger.info("更新あり: %s -> %s", current_version, latest["tag"])
    return latest


# インストーラを取得し、期待ハッシュと一致したパスを返す (sec2 S1-a/b/d)。
# 置き場所は毎回作る専用の一時フォルダにし、固定パスの差し替えを避ける。
# on_progress(done_bytes, total_bytes) で進捗を通知する (total は不明なら 0)。
def download_installer(url, expected_sha256, on_progress=None):
    expected = _clean_sha256(expected_sha256)
    if not expected:
        raise ValueError("更新プログラムの期待ハッシュが無いため、更新を中止しました。")
    if not _is_allowed_url(url):
        raise ValueError(f"想定外の配布元 URL のため更新を中止しました: {url}")

    work_dir = tempfile.mkdtemp(prefix="stretheus_update_")
    dest = os.path.join(work_dir, _INSTALLER_ASSET)
    digest = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_SEC) as res:
            if not _is_allowed_url(res.geturl()):   # リダイレクト先も確かめる
                raise ValueError("配布元 URL が想定外へリダイレクトされました。")
            total = int(res.headers.get("Content-Length") or 0)
            done = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = res.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)

        actual = digest.hexdigest().lower()
        if actual != expected:
            _logger.error("更新プログラムのハッシュ不一致: expected=%s actual=%s", expected, actual)
            raise ValueError("更新プログラムのハッシュが一致しません。更新を中止しました。")
    except Exception:
        # 壊れた/偽物の取得物を残さない。実行経路から確実に取り除く。
        shutil.rmtree(work_dir, ignore_errors=True)
        raise

    return dest


# インストーラを新プロセスで起動する。呼び出し側 (GUI) が続けてアプリを終了し、
# インストーラが本体ファイルを上書き更新できるようにする。
# **ここへ渡してよいのは download_installer が検証を通したパスだけ**である。
def launch_installer(installer_path):
    subprocess.Popen([installer_path], close_fds=True)
