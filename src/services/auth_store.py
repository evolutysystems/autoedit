# Stretheus API の JWT をローカルへ保存する (StretheusAPI resolve2 §6.4)
# 保存先は %LOCALAPPDATA%\Stretheus\auth.dat。平文では書かず、Windows DPAPI
# (CryptProtectData / CurrentUser スコープ) で暗号化する。復号できるのは同じ
# Windows ユーザーだけであり、ファイルをコピーしても他端末では読めない。
# トークンはログへ出力しない (resolve17 §7 と同じ方針)。
import ctypes
import ctypes.wintypes
import json
import os

from ..utils.logger import get_logger

_logger = get_logger(__name__)

# ユーザー操作を伴うプロンプトを出さない (サービス/バックグラウンドでも失敗で返す)
_CRYPTPROTECT_UI_FORBIDDEN = 0x01

# 復号時に別アプリの暗号文を取り違えないための説明文字列 (DPAPI が一緒に保護する)
_DESCRIPTION = "Stretheus API credentials"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


# %LOCALAPPDATA%\Stretheus\auth.dat の既定パスを返す。
def default_auth_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Stretheus", "auth.dat")


def _blob_bytes(blob):
    return ctypes.string_at(blob.pbData, blob.cbData)


def _free(blob):
    if blob.pbData:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


# 入力バイト列を指す DATA_BLOB を作る。buffer は呼び出しが終わるまで保持すること。
def _make_blob(data):
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


# 平文バイト列を DPAPI で暗号化する。失敗時は None。
def _protect(plain):
    source, _buffer = _make_blob(plain)
    result = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), _DESCRIPTION, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result))
    if not ok:
        return None
    try:
        return _blob_bytes(result)
    finally:
        _free(result)


# DPAPI の暗号文を復号する。他ユーザー/他端末の暗号文なら None。
def _unprotect(encrypted):
    source, _buffer = _make_blob(encrypted)
    result = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result))
    if not ok:
        return None
    try:
        return _blob_bytes(result)
    finally:
        _free(result)


# JWT とユーザー情報を DPAPI で保護して読み書きする。
# 保持する内容: access_token / expires_at / session_expires_at / user (§6.4)。
class AuthStore:

    def __init__(self, path=None):
        self._path = path or default_auth_path()

    @property
    def path(self):
        return self._path

    # 保存済みの資格情報を返す。未保存・復号不可・壊れている場合は None。
    def load(self):
        if not os.path.isfile(self._path):
            return None
        try:
            with open(self._path, "rb") as f:
                encrypted = f.read()
        except OSError as e:
            _logger.warning("認証情報の読み込みに失敗: %s", e)
            return None

        if not encrypted:
            return None

        plain = _unprotect(encrypted)
        if plain is None:
            # 別ユーザーのプロファイルへコピーされた場合などに起きる。再ログインで復旧する。
            _logger.warning("認証情報を復号できませんでした。再ログインが必要です。")
            return None

        try:
            data = json.loads(plain.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            _logger.warning("認証情報の形式が不正です。再ログインが必要です。")
            return None

        return data if isinstance(data, dict) else None

    # 資格情報を保存する。暗号化できない環境では書き込まない (平文保存はしない)。
    def save(self, data):
        plain = json.dumps(data, ensure_ascii=False).encode("utf-8")
        encrypted = _protect(plain)
        if encrypted is None:
            _logger.warning("認証情報を暗号化できないため保存しません (今回の起動中のみ有効)。")
            return False

        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "wb") as f:
                f.write(encrypted)
        except OSError as e:
            _logger.warning("認証情報の保存に失敗: %s", e)
            return False

        return True

    # 保存済みの資格情報を削除する (ログアウト / 再ログインが必要になったとき)。
    def clear(self):
        try:
            if os.path.isfile(self._path):
                os.remove(self._path)
        except OSError as e:
            _logger.warning("認証情報の削除に失敗: %s", e)
