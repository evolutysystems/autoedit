# 作業フォルダの残骸を片づける (docs/error/fromUser/resolve.md §6-10)
#
# 背景: 中間ファイルは tempfile.TemporaryDirectory の配下に置いているが、
# プロセスが異常終了すると後始末は走らない。2026-10-22 の報告のように
# 容量不足で落ちると、落ちるまでに書いた中間ファイルがそのまま残り、
# 「容量不足で落ちる → 残骸が増える → 次回はさらに空きが少ない」という
# 悪循環になる。起動のたびに古い残骸だけを引き取ってこれを断ち切る。
#
# 安全のための決め事:
#   ・指定時間より新しいものには触らない (多重起動で動いている実行を壊さない)
#   ・自分が作る名前 (prefix) で始まるフォルダだけを対象にする
#   ・例外は内部で握る。掃除の失敗で起動を妨げない
import os
import shutil
import tempfile
import time

from .logger import get_logger

_logger = get_logger(__name__)

# 掃除の対象にする一時フォルダの名前 (PipelineContext / media_recovery が作るもの)
DEFAULT_PREFIXES = ("autoedit_", "autoedit_recover_")

# 既定の猶予時間 (時間)。これより新しいものは走っている可能性があるため残す。
DEFAULT_MAX_AGE_HOURS = 24


# フォルダの合計サイズ (バイト)。測れないものは数えない。
def _folder_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


# 古い作業フォルダの残骸を削除する。
# base_dir      : 探す場所 (既定: %TEMP%)
# prefixes      : 対象にするフォルダ名の先頭
# max_age_hours : これより古いものだけ消す (0 以下なら何もしない)
# 戻り値: {"removed": 削除数, "freed": 回収バイト数}
def sweep_stale(base_dir=None, prefixes=DEFAULT_PREFIXES,
                max_age_hours=DEFAULT_MAX_AGE_HOURS, now=None):
    result = {"removed": 0, "freed": 0}
    try:
        hours = float(max_age_hours)
    except (TypeError, ValueError):
        return result
    if hours <= 0:
        return result

    folder = base_dir or tempfile.gettempdir()
    deadline = (now if now is not None else time.time()) - hours * 3600
    try:
        names = os.listdir(folder)
    except OSError as e:
        _logger.warning("作業フォルダの掃除を始められませんでした: %s (%s)", folder, e)
        return result

    for name in names:
        if not name.startswith(tuple(prefixes)):
            continue
        path = os.path.join(folder, name)
        try:
            if not os.path.isdir(path) or os.path.getmtime(path) >= deadline:
                continue
            size = _folder_size(path)
            shutil.rmtree(path, ignore_errors=True)
            if os.path.exists(path):
                # 掴まれている等。次回の起動で改めて試す。
                _logger.info("使用中のため残しました: %s", path)
                continue
        except OSError as e:
            _logger.warning("残骸を片づけられませんでした: %s (%s)", path, e)
            continue
        result["removed"] += 1
        result["freed"] += size

    if result["removed"]:
        _logger.info("作業フォルダの残骸を %d 件削除しました (%.1f GB 回収)",
                     result["removed"], result["freed"] / (1024 ** 3))
    return result
