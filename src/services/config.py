# Stretheus API 接続設定の読み出しと、認証オブジェクトの生成
# (StretheusAPI docs/request/resolve2.md §6.2)
# 接続先は _DEFAULT_BASE_URL 1 箇所で決まり、setting.json と環境変数で上書きできる (sec2 N1)。
import os
import sys
import threading

from ..utils.logger import get_logger
from .log_sender import LogSender
from .points import PointsService
from .stretheus_auth import StretheusAuth

_logger = get_logger(__name__)

# 配布クライアントの接続先。**接続先を決めるのはここ 1 箇所だけ** (sec2 N1)。
#
# 以前は setting.json・settings_window.DEFAULT_SETTINGS・_normalize_legacy_values の
# 3 か所で決まっており、1 か所だけ直しても向き先が変わらなかった
# (StretheusPlan/plan.md §7.8.4「最も見落としやすい」)。setting.json と
# DEFAULT_SETTINGS を空の既定にしてここへ集約したため、**切り替えはこの 1 行**で足りる。
#
# 現在の運用: dev 環境が実運用を担っている (prod は未デプロイ / HowToRelease §6)。
# prod へ移すときは、この 1 行を prod へ変えたうえで、
# settings_window._MANAGED_API_BASE_URLS へ dev の URL を足す
# (既に dev を保存している利用者を既定へ追従させるため。同 §7.8.4 の 3 番目)。
_DEFAULT_BASE_URL = "https://stretheusapi-dev.azurewebsites.net"
_DEFAULT_TIMEOUT_SEC = 15

# 開発端末だけ接続先を変えるための環境変数 (sec2 N1)。
#
# 既定 (_DEFAULT_BASE_URL) 以外の環境を試したいときに使う。setting.json へ書くと
#   ・リポジトリへ commit される (git 履歴にホスト名が残る)
#   ・main_window.spec が同梱時に空へ落とすので配布物では効かない
# ため、設定ファイルではなく環境変数で渡す。
#
# 使い方 (PowerShell):
#   $env:STRETHEUS_API_BASE_URL = "https://stretheusapi.azurewebsites.net"
_ENV_BASE_URL = "STRETHEUS_API_BASE_URL"

# 上書きの通知を出したか (毎回呼ばれるためログは 1 回だけにする)
_env_override_logged = False


# 環境変数で指定された接続先を返す。未設定なら空文字。
#
# **凍結配布では常に空を返す**。配布物の向き先を環境変数で外から変えられると、
# 利用者の JWT を任意のサーバーへ送らせる経路になるため (sec2 S2 の緩和)。
def _env_base_url():
    global _env_override_logged

    if getattr(sys, "frozen", False):
        return ""

    value = str(os.environ.get(_ENV_BASE_URL, "") or "").strip()
    if value and not _env_override_logged:
        _logger.info("環境変数 %s により API 接続先を上書きします: %s", _ENV_BASE_URL, value)
        _env_override_logged = True
    return value


# api セクションを平坦化して返す。
# 接続先の優先順位は 環境変数 (開発端末のみ) > setting.json > _DEFAULT_BASE_URL。
def api_config(settings):
    api = settings.get("api", {}) if isinstance(settings, dict) else {}
    base_url = (_env_base_url()
                or str(api.get("base_url", "") or "").strip()
                or _DEFAULT_BASE_URL)
    return {
        "base_url": base_url.rstrip("/"),
        "timeout_sec": float(api.get("timeout_sec", _DEFAULT_TIMEOUT_SEC) or _DEFAULT_TIMEOUT_SEC),
    }


# 設定から StretheusAuth を作る。アプリ全体で 1 つだけ持ち回ること
# (リフレッシュの直列化はインスタンス内のロックで行うため / §6.4)。
def create_auth(settings, store=None):
    config = api_config(settings)
    return StretheusAuth(config["base_url"], config["timeout_sec"], store=store)


# 設定から PointsService を作る。認証と同じく 1 つだけ持ち回る。
def create_points(settings, auth=None, store_dir=None):
    return PointsService(auth or create_auth(settings), settings, store_dir=store_dir)


# 設定から LogSender を作る (request1 / resolve1 §4.1)。
# 認証を共有するため、既にある StretheusAuth を渡すこと。
def create_log_sender(settings, auth=None, queue=None):
    return LogSender(auth or create_auth(settings), settings, queue=queue)


# ---- プロセス全体で 1 つだけ持つ実体 (GUI 用) --------------------------------
# リフレッシュの直列化はインスタンス内のロックで行うため、画面・出力処理・
# 残高表示がそれぞれ別の実体を持つと直列化が効かない (§6.4)。
# CLI / テストはここを使わず、必要なら create_auth / create_points を直接呼ぶ。
_shared = {"auth": None, "points": None, "log_sender": None}
_shared_lock = threading.Lock()


# 共有の StretheusAuth。最初に呼ばれたときの設定で作る。
def shared_auth(settings):
    with _shared_lock:
        if _shared["auth"] is None:
            _shared["auth"] = create_auth(settings)
        return _shared["auth"]


# 共有の PointsService。
def shared_points(settings):
    with _shared_lock:
        if _shared["points"] is None:
            if _shared["auth"] is None:
                _shared["auth"] = create_auth(settings)
            _shared["points"] = PointsService(_shared["auth"], settings)
        return _shared["points"]


# 共有の LogSender (request1 / resolve1 §6)。停止要求を 1 箇所で受けるため、
# 画面と出力処理が同じ実体を見る必要がある。認証も残高表示と共有する。
def shared_log_sender(settings):
    with _shared_lock:
        if _shared["log_sender"] is None:
            if _shared["auth"] is None:
                _shared["auth"] = create_auth(settings)
            _shared["log_sender"] = LogSender(_shared["auth"], settings)
        return _shared["log_sender"]


# 既に作られている共有 LogSender を返す (無ければ None)。
# 出力処理から「送信をいま止めて」と伝えるために使う。
def current_log_sender():
    with _shared_lock:
        return _shared["log_sender"]


# 既に作られている共有 PointsService を返す (無ければ None)。
# 画面から直接書き出す経路 (Resolve 出力) で使う。GUI を立ち上げていない
# 呼び出し (CLI / テスト) では None になり、ポイント処理を通らない。
def current_points():
    with _shared_lock:
        return _shared["points"]


# 共有の実体を捨てる (接続先を変えたとき / テストの後始末)。
def reset_shared():
    with _shared_lock:
        _shared["auth"] = None
        _shared["points"] = None
        _shared["log_sender"] = None
