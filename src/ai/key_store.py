# 開発用の直接呼び出しの鍵の保管 (docs/AiEditor/resolve1.md §6.5)
#
# **配布経路では使わない。** 既定 (ai.direct_mode = false) ではこのモジュールへ触れない。
#
# 決まり:
#   ・鍵は setting.json へ書かない (配布 setting.json に鍵欄を持たせない / sec2 N1)。
#   ・置き場は %LOCALAPPDATA%\Stretheus\ai_keys.dat で、DPAPI (CurrentUser) で保護する
#     (services/auth_store.protect / unprotect をそのまま使う)。
#   ・鍵そのものはログへ出さない (§17.2)。長さだけを残す。
import json
import os

from ..services.auth_store import default_auth_path, protect, unprotect
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# 保存するファイル名 (auth.dat と同じフォルダ)。
_FILE_NAME = "ai_keys.dat"

# 既定の鍵の名前 (将来プロバイダーが増えたときに分けられるようにしておく)。
PROVIDER_ANTHROPIC = "anthropic"


# 鍵ファイルの既定のパス。
def default_key_path():
    return os.path.join(os.path.dirname(default_auth_path()), _FILE_NAME)


# 鍵を保存する。DPAPI が使えない環境では保存せず False を返す
# (平文では書かない。鍵を持たないまま直接呼び出しを諦めるほうが安全である)。
def save_key(api_key, provider=PROVIDER_ANTHROPIC, path=None):
    key = str(api_key or "").strip()
    if not key:
        return False

    target = path or default_key_path()
    keys = _read_all(target)
    keys[str(provider)] = key

    blob = protect(json.dumps(keys, ensure_ascii=False).encode("utf-8"))
    if blob is None:
        _logger.warning("AI の鍵を保護できなかったため保存しません (DPAPI 不可)。")

        return False

    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(blob)
    except OSError as e:
        _logger.warning("AI の鍵を保存できませんでした: %s", e)

        return False

    _logger.info("AI の鍵を保存しました (%d 文字)。", len(key))

    return True


# 鍵を読み出す。無い・読めない場合は None。
def load_key(provider=PROVIDER_ANTHROPIC, path=None):
    keys = _read_all(path or default_key_path())
    key = keys.get(str(provider))

    return key if isinstance(key, str) and key.strip() else None


# 鍵を消す。
def clear_key(path=None):
    target = path or default_key_path()
    try:
        if os.path.exists(target):
            os.remove(target)

            return True
    except OSError as e:
        _logger.warning("AI の鍵を削除できませんでした: %s", e)

    return False


def _read_all(path):
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "rb") as handle:
            blob = handle.read()
    except OSError as e:
        _logger.warning("AI の鍵を読み出せませんでした: %s", e)

        return {}

    if not blob:
        return {}

    plain = unprotect(blob)
    if plain is None:
        # 他ユーザー・他端末の暗号文。読めないものは無いものとして扱う。
        _logger.warning("AI の鍵を復号できませんでした (別の利用者・端末の暗号文)。")

        return {}

    try:
        parsed = json.loads(plain.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}

    return parsed if isinstance(parsed, dict) else {}
