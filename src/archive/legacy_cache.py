# 旧版が残した取得データの回収 (docs/error/fromUser/resolve.md §6-6-6 / §8-2 7-7)
#
# 2026-10-22 の利用者報告で、取得まわりが**見えない場所に溜まり続ける**ことが分かった。
# 開発端末での実測:
#   %LOCALAPPDATA%\twitch-dl              48.38 GB  (未完成の取得 1 本ぶんの断片)
#   <インストール先>…\src\settings\archive_work   45.59 GB  (結合後の VOD)
#
# どちらも現行版では使わなくなる場所である:
#   ・断片の置き場は work_dir/cache へ移した (§6-3)
#   ・取得物の置き場は %LOCALAPPDATA%\Stretheus へ移した (§6-6-6)
# しかし**旧版が残した分は自動では消えない**ため、利用者へ知らせて回収する。
#
# 決め事:
#   ・実在するときだけ対象にする (無ければ何も言わない)
#   ・小さいものでは尋ねない (毎回の起動で煩わせない)
#   ・無断では消さない。必ず尋ね、ゴミ箱へ送る
#   ・一度答えてもらったら覚えておき、二度と尋ねない
import os

from ..utils import trash
from ..utils.logger import get_logger
from .config import legacy_work_dir

_logger = get_logger(__name__)

# twitch-dl が既定で使う断片キャッシュ (twitchdl/cache.py の _cache_dir_path と同じ決まり)。
# 現行版は --cache-dir で work_dir 配下を指すため、ここは旧版の置き土産にあたる。
_TWITCHDL_CACHE_NAME = "twitch-dl"

# これ未満なら尋ねない (バイト)。1 GB 未満は知らせる価値がない。
MIN_NOTICE_BYTES = 1024 ** 3


# twitch-dl の断片キャッシュの場所を返す (無ければ空文字)。
# **実在するときだけ**返す。他ツールと共用しうる場所のため、
# 無いものを作ったり、存在を仮定したりしない。
def twitchdl_cache_dir():
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return ""
    path = os.path.join(base, _TWITCHDL_CACHE_NAME)
    return path if os.path.isdir(path) else ""


# フォルダの合計サイズ (バイト)。測れないものは数えない。
def folder_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


# 回収できる旧データを探す。
# 戻り値: [{"path", "bytes", "label"}…] (サイズの大きい順)
# min_bytes 未満のものは含めない (知らせる価値がないため)。
def find_reclaimable(settings, min_bytes=MIN_NOTICE_BYTES):
    found = []

    # ① twitch-dl の断片キャッシュ (実在するときのみ)
    cache = twitchdl_cache_dir()
    if cache:
        found.append({"path": cache, "bytes": folder_size(cache),
                      "label": "取得時の一時データ"})

    # ② 旧い作業フォルダ (インストールフォルダ配下)。
    #    現行の置き場と同じなら対象外 (移行前の利用者だけが該当する)。
    legacy = legacy_work_dir(settings)
    if legacy and os.path.isdir(legacy):
        found.append({"path": legacy, "bytes": folder_size(legacy),
                      "label": "以前の保存先に残った配信データ"})

    return sorted((item for item in found if item["bytes"] >= min_bytes),
                  key=lambda item: item["bytes"], reverse=True)


# 見つかった旧データをゴミ箱へ送る。
# 呼び出し側が利用者へ尋ねたうえで呼ぶこと (ここでは尋ねない)。
# ゴミ箱を使えない環境では何もしない (黙って完全削除はしない)。
# 戻り値: 回収したバイト数。
def reclaim(items):
    paths = [item["path"] for item in items if item.get("path")]
    if not paths:
        return 0
    if not trash.is_available():
        _logger.info("ゴミ箱を使えないため旧データの回収は行いません")
        return 0

    sizes = {item["path"]: item["bytes"] for item in items}
    result = trash.send_to_trash(paths)
    freed = sum(sizes.get(path, 0) for path in result["trashed"])
    for path in result["trashed"]:
        _logger.info("旧データをゴミ箱へ送りました: %s", path)
    for item in result["failed"]:
        _logger.warning("旧データをゴミ箱へ送れませんでした: %s (%s)",
                        item["path"], item["reason"])
    if freed:
        _logger.info("旧データの回収で %.1f GB を取り戻しました", freed / (1024 ** 3))
    return freed
