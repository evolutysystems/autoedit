# ファイル・フォルダをゴミ箱へ送る (docs/request/ver3/resolve9.md §3-7 / §5.10-b)
#
# 外部ライブラリ (send2trash) は docs/CLAUDE.md の方針により追加しない。
# 標準ライブラリの ctypes から Windows のシェル API SHFileOperationW を呼ぶ。
# 非 Windows・API 失敗時は failed へ入れて返し、完全に削除するかどうかは
# 呼び出し側 (GUI) が利用者へ確認する。黙って完全削除はしない。
import ctypes
import os

from .logger import get_logger

_logger = get_logger(__name__)

# SHFileOperationW の操作種別とフラグ (shellapi.h)
FO_DELETE = 0x0003
FOF_SILENT = 0x0004             # 進捗ダイアログを出さない
FOF_NOCONFIRMATION = 0x0010     # 「ゴミ箱へ移しますか」を出さない (アプリ側で確認済み)
FOF_ALLOWUNDO = 0x0040          # ← これがゴミ箱行きの指定
FOF_NOERRORUI = 0x0400          # エラーダイアログを出さず戻り値で受ける

# SHFileOperationW が扱えるパス長の上限 (MAX_PATH)。超えるものは送れない。
_MAX_PATH = 260

if os.name == "nt":
    from ctypes import wintypes

    # SHFILEOPSTRUCTW (64bit のパディングは既定に任せる = _pack_ を指定しない)
    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),        # "path1\0path2\0\0" (ダブル NUL 終端)
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort),        # FILEOP_FLAGS = WORD
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]
else:
    _SHFILEOPSTRUCTW = None


# ゴミ箱が使えるか (Windows で shell32 を読めるか)
def is_available():
    if os.name != "nt":
        return False
    try:
        return ctypes.windll.shell32 is not None
    except Exception:  # noqa: BLE001 (読めない環境ではゴミ箱を使わない)
        return False


# ゴミ箱へ送れないパスを弾く理由を返す (送れるなら None)
def _reject_reason(path):
    if not path:
        return "パスが空です"
    full = os.path.abspath(path)
    if not os.path.exists(full):
        return "ファイルが見つかりません"
    if len(full) >= _MAX_PATH:
        return f"パスが長すぎます ({len(full)} 文字 / 上限 {_MAX_PATH - 1})"
    return None


# まとめてゴミ箱へ送る (シェル API の確認・進捗が複数回出ないよう 1 回で呼ぶ)
# 戻り値: {"trashed": [パス…], "failed": [{"path","reason"}…]}
def send_to_trash(paths):
    result = {"trashed": [], "failed": []}
    targets = []
    for path in (paths or []):
        reason = _reject_reason(path)
        if reason:
            result["failed"].append({"path": str(path), "reason": reason})
            continue
        targets.append(os.path.abspath(str(path)))
    if not targets:
        return result
    if not is_available():
        result["failed"].extend(
            {"path": t, "reason": "ゴミ箱を利用できない環境です"} for t in targets)
        return result

    operation = _SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    # ダブル NUL 終端の連結文字列 (末尾の "" が 2 つ目の NUL になる)
    operation.pFrom = "\0".join(targets) + "\0\0"
    operation.pTo = None
    operation.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION
                        | FOF_SILENT | FOF_NOERRORUI)
    operation.fAnyOperationsAborted = False
    operation.hNameMappings = None
    operation.lpszProgressTitle = None

    try:
        code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    except Exception as e:  # noqa: BLE001 (API 呼び出しの失敗で画面を落とさない)
        _logger.exception("ゴミ箱への移動に失敗しました")
        result["failed"].extend({"path": t, "reason": str(e)} for t in targets)
        return result

    if code != 0 or operation.fAnyOperationsAborted:
        reason = f"シェル API がエラーを返しました (コード 0x{code:X})"
        if operation.fAnyOperationsAborted:
            reason = "操作が中断されました"
        _logger.warning("ゴミ箱へ送れませんでした: %s", reason)
        result["failed"].extend({"path": t, "reason": reason} for t in targets)
        return result

    # API は成功しても消えていない場合があるため実体で確認する
    for target in targets:
        if os.path.exists(target):
            result["failed"].append({"path": target, "reason": "削除されませんでした"})
        else:
            result["trashed"].append(target)
    if result["trashed"]:
        _logger.info("ゴミ箱へ移動しました (%d 件)", len(result["trashed"]))
    return result
