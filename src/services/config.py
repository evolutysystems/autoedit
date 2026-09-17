# Stretheus API 接続設定の読み出しと、認証オブジェクトの生成
# (StretheusAPI docs/request/resolve2.md §6.2)
# ハードコードを避け、setting.json の api セクションを唯一の出どころとする。
from .points import PointsService
from .stretheus_auth import StretheusAuth

# 設定が空だった場合の接続先 (§5.5 で確定した本番ホスト名)。
_DEFAULT_BASE_URL = "https://stretheusapi.azurewebsites.net"
_DEFAULT_TIMEOUT_SEC = 15


# api セクションを平坦化して返す。
def api_config(settings):
    api = settings.get("api", {}) if isinstance(settings, dict) else {}
    return {
        "base_url": str(api.get("base_url", "") or _DEFAULT_BASE_URL).rstrip("/"),
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
