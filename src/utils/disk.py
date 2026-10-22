# ディスク空き容量の確認と「容量不足」の判定 (docs/error/fromUser/resolve.md §6-2)
#
# 背景: 2026-10-22 の利用者報告で、VOD 取得中に C: が枯渇して
# [Errno 28] No space left on device のままクラッシュした。
# 本モジュール導入前は disk_usage も ENOSPC 判定もコード内に 1 箇所も無く、
#   ・始める前に「足りない」と言えない
#   ・枯渇したのか別の理由で落ちたのか区別できない
# という状態だった。その 2 点を埋めるのがここの役目である。
#
# 外部ライブラリは追加しない (docs/CLAUDE.md)。標準の shutil.disk_usage のみ使う。
import errno
import os
import shutil

from ..exceptions import DiskSpaceError
from ..i18n import tr
from .logger import get_logger

_logger = get_logger(__name__)

# Windows の「ディスクが一杯」系システムエラー (winerror)
# 112 = ERROR_DISK_FULL / 39 = ERROR_HANDLE_DISK_FULL
# shutil.copy2 などを経由すると errno が ENOSPC にならないことがあるため両方見る。
_WIN_DISK_FULL_ERRORS = (112, 39)

# FFmpeg / 外部ツールの標準エラーに出る「容量不足」の言い回し (小文字で比較)
_NO_SPACE_PHRASES = (
    "no space left on device",
    "not enough space on the disk",
    "there is not enough space",
    "disk full",
    "enospc",
)

# 1 GB のバイト数 (設定値 min_free_space_gb の換算に使う)
GIB = 1024 ** 3


# バイト数を人が読める文字列にする ("12.3 GB")。
# ログと画面文言で同じ表記を使うため、整形はここへ寄せる。
def human(size):
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} TB"


# path が置かれるドライブの空き容量 (バイト) を返す。取得できなければ None。
# path 自体がまだ無くてもよい: 実在する直近の親まで遡って測る
# (これから作るフォルダの空きを開始前に確かめるため)。
def free_bytes(path):
    candidate = os.path.abspath(str(path or "."))
    while True:
        try:
            return shutil.disk_usage(candidate).free
        except OSError:
            parent = os.path.dirname(candidate)
            if not parent or parent == candidate:
                _logger.warning("空き容量を取得できませんでした: %s", path)
                return None
            candidate = parent


# 例外が「ディスクが一杯」を表すかを判定する。
# OSError 以外 (ラップされた例外など) は __cause__ まで 1 段だけ辿る。
def is_no_space(error):
    if isinstance(error, DiskSpaceError):
        return True
    if isinstance(error, OSError):
        if error.errno == errno.ENOSPC:
            return True
        if getattr(error, "winerror", None) in _WIN_DISK_FULL_ERRORS:
            return True
    cause = getattr(error, "__cause__", None)
    if cause is not None and isinstance(cause, OSError):
        return is_no_space(cause)
    return False


# 外部ツールの標準エラー出力が「容量不足」を示しているかを判定する。
# FFmpeg は returncode だけでは理由が分からないため、文言で見分ける (§6-9)。
def looks_like_no_space(text):
    if not text:
        return False
    lowered = str(text).lower()
    return any(phrase in lowered for phrase in _NO_SPACE_PHRASES)


# 「空き容量が足りません」の共通文言を組み立てる。
# 画面・ログ・例外で同じ文面を使い、利用者が次に何をすればよいか分かるようにする。
def shortage_message(path, free, required):
    return tr(
        "空き容量が足りません。\n"
        "  場所 : {path}\n"
        "  空き : {free}\n"
        "  必要 : {required}\n"
        "不要なファイルを削除するか、設定画面で作業フォルダを"
        "別のドライブへ変更してください。",
        path=path, free=human(free), required=human(required))


# path のドライブに required_bytes の空きがあることを確かめる。
# 足りなければ DiskSpaceError を送出する (1 バイトも書く前に止めるために使う)。
# required_bytes <= 0 のときは確認しない (設定で無効にできるようにするため)。
# 空き容量を測れない環境では**止めない** (測れないことを理由に機能を奪わない)。
def ensure_free(path, required_bytes, label=""):
    required = int(required_bytes or 0)
    if required <= 0:
        return
    free = free_bytes(path)
    if free is None:
        return
    if free >= required:
        _logger.info("空き容量 OK%s: %s (空き %s / 必要 %s)",
                     f" [{label}]" if label else "", path, human(free), human(required))
        return
    _logger.error("空き容量 不足%s: %s (空き %s / 必要 %s)",
                  f" [{label}]" if label else "", path, human(free), human(required))
    raise DiskSpaceError(shortage_message(path, free, required),
                         path=str(path), free=free, required=required)


# 設定値 (GB) をバイトへ直す。不正値・負値は 0 (= 確認しない) とする。
def required_from_gb(gigabytes):
    try:
        value = float(gigabytes)
    except (TypeError, ValueError):
        return 0
    return int(value * GIB) if value > 0 else 0
