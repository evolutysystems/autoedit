# 自動アップデートモジュール (request_autoupdate.md §5)
# GitHub Releases から最新版を確認し、新版があればインストーラを取得・起動する。
# 通信は標準ライブラリ urllib のみ (新規依存を増やさない)。失敗時は静かにスキップする。
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

from .logger import get_logger

_logger = get_logger(__name__)

# 配布元リポジトリ (installer/AutoEdit.iss の RepoOwner/RepoName と一致させる)
_REPO_OWNER = "evolutysystems"
_REPO_NAME = "autoedit"
# インストーラのアセット名 (installer/AutoEdit.iss の OutputBaseFilename に対応)
_INSTALLER_ASSET = "AutoEditSetup.exe"
_API_LATEST = f"https://api.github.com/repos/{_REPO_OWNER}/{_REPO_NAME}/releases/latest"
_HTTP_TIMEOUT_SEC = 5


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


# 最新リリースの {"tag", "installer_url"} を返す。失敗時は None (呼び出し側でスキップ)。
def check_latest():
    req = urllib.request.Request(
        _API_LATEST,
        headers={
            "User-Agent": "AutoEdit-Updater",  # GitHub API はUA必須
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
    # アセットから AutoEditSetup.exe を探す。無ければタグからURLを組み立てる。
    url = None
    for asset in data.get("assets", []):
        if asset.get("name") == _INSTALLER_ASSET:
            url = asset.get("browser_download_url")
            break
    if not url and tag:
        url = (f"https://github.com/{_REPO_OWNER}/{_REPO_NAME}"
               f"/releases/download/{tag}/{_INSTALLER_ASSET}")
    return {"tag": tag, "installer_url": url}


# 更新が必要なら latest 情報を返す。凍結ビルドでない/最新/失敗時は None。
def find_update(current_version):
    # 開発(ソース)実行では自身を更新しない (PyInstaller 凍結時のみ有効)
    if not getattr(sys, "frozen", False):
        _logger.info("非凍結実行のため更新チェックをスキップ")
        return None
    latest = check_latest()
    if not latest or not latest.get("installer_url"):
        return None
    if is_newer(latest["tag"], current_version):
        _logger.info("更新あり: %s -> %s", current_version, latest["tag"])
        return latest
    return None


# インストーラを一時フォルダへDLし、そのパスを返す。
# on_progress(block_num, block_size, total_size) を urlretrieve に橋渡しする。
def download_installer(url, on_progress=None):
    dest = os.path.join(tempfile.gettempdir(), _INSTALLER_ASSET)
    urllib.request.urlretrieve(url, dest, reporthook=on_progress)
    return dest


# インストーラを新プロセスで起動する。呼び出し側 (GUI) が続けてアプリを終了し、
# インストーラが本体ファイルを上書き更新できるようにする。
def launch_installer(installer_path):
    subprocess.Popen([installer_path], close_fds=True)
