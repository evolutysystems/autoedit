# 学習データとプロファイルの置き場 (docs/AiEditor/resolve1.md §13.4 / R11)
#
# 置き場は `%LOCALAPPDATA%\Stretheus\ai\` (Windows の利用者単位)。
#   profiles/<slug>.json   編集プロファイル (§13.3)
#   diffs/<stamp>.json     AI 版と人間版の差分 (§14)
#
# 決まり:
#   ・**プロジェクトの隣には置かない。** プロジェクトを配っても学習内容が付いて回らない。
#   ・利用者ごとに分ける。JWT の sub では分けない (ログイン前も使えるため / §13.4)。
#   ・**サーバーへ送らない** (`ai.learning.upload = false` 固定)。
#   ・古い差分は `ai.learning.keep_days` (既定 180 日) で掃除する。
#     **プロファイル本体と更新履歴は捨てない** (捨てるのは生の差分だけ / §13.4)。
import json
import os
import re
import time

from ..utils import trash
from ..utils.logger import get_logger
from . import config as ai_config

_logger = get_logger(__name__)

_APP_DIR = "Stretheus"
_AI_DIR = "ai"
_PROFILES_DIR = "profiles"
_DIFFS_DIR = "diffs"

# ファイル名に使える文字だけを残す
_SLUG_SAFE = re.compile(r"[^0-9A-Za-z぀-ヿ一-鿿_-]+")


# %LOCALAPPDATA%\Stretheus\ai
def root_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")

    return os.path.join(base, _APP_DIR, _AI_DIR)


def profiles_dir(root=""):
    return os.path.join(root or root_dir(), _PROFILES_DIR)


def diffs_dir(root=""):
    return os.path.join(root or root_dir(), _DIFFS_DIR)


# プロファイルのパス (<slug>.json)。
def profile_path(slug, root=""):
    name = slugify(slug)

    return os.path.join(profiles_dir(root), name + ".json") if name else ""


# 差分の置き場 (<日時>_<プロジェクト名>.json)。追記ではなく 1 回 1 ファイル。
def diff_path(project_path, root="", stamp=None):
    moment = time.localtime(stamp if stamp is not None else time.time())
    name = slugify(os.path.splitext(os.path.basename(str(project_path or "")))[0])

    return os.path.join(diffs_dir(root), "{0}_{1}.json".format(
        time.strftime("%Y%m%d-%H%M%S", moment), name or "project"))


def slugify(value):
    text = _SLUG_SAFE.sub("_", str(value or "").strip())

    return text.strip("_")[:64]


# JSON を読む。無い・壊れているときは default。
def load_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as e:
        _logger.warning("学習データを読めませんでした (%s): %s", path, e)

        return default


# JSON を書く (一時ファイル → rename)。失敗しても例外にしない。
def save_json(data, path):
    if not path:
        return ""
    folder = os.path.dirname(os.path.abspath(path))
    try:
        if folder:
            os.makedirs(folder, exist_ok=True)
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp, path)
    except OSError as e:
        _logger.warning("学習データを保存できませんでした (%s): %s", path, e)

        return ""

    return path


# プロファイルの一覧 (slug, パス)。
def list_profiles(root=""):
    folder = profiles_dir(root)
    if not os.path.isdir(folder):
        return []
    rows = []
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith(".json"):
            rows.append((os.path.splitext(name)[0], os.path.join(folder, name)))

    return rows


# 古い差分を掃除する (§13.4)。返り値は (消した件数, 残した件数).
#
# **プロファイルは対象にしない。** 捨てるのは生の差分だけである。
# 既定ではゴミ箱へ送る (既存の残骸整理と同じ作法)。送れない環境では消す。
def cleanup(settings, root="", now=None):
    cfg = ai_config.ai_config(settings)["learning"]
    keep_days = int(cfg.get("keep_days") or 0)
    folder = diffs_dir(root)
    if keep_days <= 0 or not os.path.isdir(folder):
        return (0, 0)

    limit = (now if now is not None else time.time()) - keep_days * 86400.0
    old = []
    kept = 0
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        try:
            if os.path.getmtime(path) < limit:
                old.append(path)
            else:
                kept += 1
        except OSError:
            continue
    if not old:
        return (0, kept)

    removed = _discard(old)
    _logger.info("古い差分を %d 件片付けました (保持 %d 日 / 残り %d 件)",
                 removed, keep_days, kept)

    return (removed, kept)


def _discard(paths):
    if trash.is_available():
        # failed は [{"path","reason"}, …] (utils/trash の約束)。
        result = trash.send_to_trash(paths) or {}
        failed = {str(row.get("path")) for row in result.get("failed") or []
                  if isinstance(row, dict)}
    else:
        failed = set(paths)
    removed = len(paths) - len(failed)
    for path in failed:
        try:
            os.remove(path)
            removed += 1
        except OSError as e:
            _logger.warning("差分を消せませんでした (%s): %s", path, e)

    return removed


# 最近の差分を読み出す (学習の材料 / §14)。
#
# 1 ファイル = 1 回の保存ぶんの差分である。新しいものから limit 件ぶんを読み、
# 中の差分を 1 本のリストへ平す。壊れたファイルは飛ばす。
def recent_diffs(root="", limit=20):
    folder = diffs_dir(root)
    if not os.path.isdir(folder):
        return []
    names = sorted((name for name in os.listdir(folder)
                    if name.lower().endswith(".json")), reverse=True)
    rows = []
    for name in names[:max(int(limit), 0)]:
        data = load_json(os.path.join(folder, name))
        if isinstance(data, dict):
            rows.extend(row for row in data.get("diffs") or []
                        if isinstance(row, dict))

    return rows
