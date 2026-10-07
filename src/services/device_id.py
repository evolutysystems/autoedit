# 未ログイン (匿名) 利用者を識別する端末 ID
#
# 未ログインのままでもポイント制の対象にするため、端末ごとに 1 つの ID を決め、
# API (POST /api/auth/device) へ渡して匿名ユーザーの JWT を受け取る。
# サーバーはこの ID のハッシュだけを保存する。
#
# ID は**端末の MachineGuid から導出する**。ファイルを消したり再インストールしても
# 同じ ID になり、残高が戻らないようにするためである。MachineGuid そのものは送らず、
# アプリ固有の文字列を混ぜた SHA-256 の要約を送る (端末の識別子を外へ出さない)。
#
# MachineGuid が読めない環境では、乱数で作った ID を %LOCALAPPDATA% へ保存して使う。
# 保存できなければ毎回変わってしまうため、暗号化できない場合は平文で保存する。
# この ID で辿れるのは匿名のポイント台帳だけであり、個人情報は含まれない
# (JWT は従来どおり暗号化できなければ保存しない / auth_store.py)。
import base64
import hashlib
import os
import secrets

from ..utils.logger import get_logger
from .auth_store import protect, unprotect

_logger = get_logger(__name__)

# 導出に混ぜるアプリ固有の文字列。MachineGuid をそのまま送らないために使う。
_DERIVATION_SALT = "stretheus-device-v1"

# 乱数で作る場合の長さ (base64url で 32 文字)
_RANDOM_BYTES = 24


# %LOCALAPPDATA%\Stretheus\device.dat の既定パスを返す。
def default_device_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Stretheus", "device.dat")


# Windows の MachineGuid を返す。読めなければ None。
def machine_guid():
    try:
        import winreg
    except ImportError:
        return None

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, _type = winreg.QueryValueEx(key, "MachineGuid")
    except OSError:
        return None

    text = str(value or "").strip()
    return text or None


# 端末の識別子から送信用の ID を作る。API の検証 (22〜128 文字の [A-Za-z0-9_-]) に収まる。
def derive(seed):
    digest = hashlib.sha256(f"{_DERIVATION_SALT}:{seed}".encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# 端末 ID を返す。MachineGuid から導出できなければ、保存した乱数を使う。
# 取得も保存もできない場合だけ None を返す (呼び出し側は匿名ログインを諦める)。
def device_id(path=None):
    guid = machine_guid()
    if guid:
        return derive(guid)

    _logger.warning("MachineGuid を取得できませんでした。保存した端末 ID を使います。")
    return _stored_device_id(path or default_device_path())


# 保存済みの端末 ID を読む。無ければ作って保存する。
def _stored_device_id(path):
    stored = _read(path)
    if stored:
        return stored

    generated = secrets.token_urlsafe(_RANDOM_BYTES)
    _write(path, generated)
    return generated


def _read(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None

    if not raw:
        return None

    # 暗号化して保存できた場合と、平文で保存した場合の両方を受け付ける。
    plain = unprotect(raw)
    text = (plain if plain is not None else raw).decode("utf-8", "ignore").strip()
    return text or None


def _write(path, value):
    plain = value.encode("utf-8")
    data = protect(plain)
    if data is None:
        # 暗号化できない環境。保存しないと毎回 ID が変わり、残高が戻ってしまう。
        _logger.warning("端末 ID を暗号化できないため平文で保存します。")
        data = plain

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    except OSError as e:
        # 保存できなくても今回の起動では使える (次回は別の ID になる)。
        _logger.warning("端末 ID の保存に失敗: %s", e)
