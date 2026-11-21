# 操作ログ (docs/AiEditor/resolve1.md §15 / R13。任意・既定オフ)
#
# 差分 (§14) は「結果」しか分からない。「どういう手順で直したか」まで見たい場合の後付け。
#
# 決まり:
#   ・`CommandStack(observer=...)` の観測口へ繋ぐだけ。**既定は None = 現行と同じ挙動**
#   ・書き出しは `<project>.editlog.jsonl` (追記のみ)。1 行 = {"at","label","kind","params"}
#   ・**スナップショットは書かない** (Timeline 全体の deepcopy をファイルへ落とさない)
#   ・各 Command へ `to_log()` を足さない (36 クラスへ手を入れない)。
#     ここでクラス名と属性から拾い、拾えないものは label だけ残す
#   ・記録の失敗で編集を止めない (CommandStack 側が例外を飲む)
import json
import os
import time

from ..utils.logger import get_logger
from . import config as ai_config

_logger = get_logger(__name__)

SUFFIX = ".editlog.jsonl"

# 記録する値の種類 (JSON にできるものだけ)
_SCALARS = (bool, int, float, str)
# 長い文字列は切る (本文を丸ごと溜めない)
_MAX_CHARS = 80
# 1 行に残す属性の上限 (コマンドによって属性数が大きく違うため)
_MAX_PARAMS = 12


# 操作ログの置き場 (<project>.editlog.jsonl)。
def log_path_for(project_path):
    base = os.path.splitext(str(project_path or ""))[0]

    return base + SUFFIX if base else ""


# 設定で有効か (`ai.learning.operation_log`。既定 false)。
def is_enabled(settings):
    learning = ai_config.ai_config(settings)["learning"]

    return bool(learning.get("enabled")) and bool(learning.get("operation_log"))


# CommandStack へ渡す観測口を作る。無効なら None (= 現行と同じ挙動)。
#
#   stack = CommandStack(observer=edit_log.observer(settings, project_path))
def observer(settings, project_path):
    if not is_enabled(settings):
        return None
    path = log_path_for(project_path)
    if not path:
        return None
    writer = EditLog(path)

    return writer.record


# 1 行 1 操作の追記ログ。
class EditLog:

    def __init__(self, path):
        self.path = str(path)
        self.count = 0

    def record(self, label, command):
        row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "label": str(label or ""),
               "kind": type(command).__name__,
               "params": params_of(command)}
        try:
            folder = os.path.dirname(os.path.abspath(self.path))
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as e:
            _logger.warning("操作ログを書けませんでした: %s", e)

            return False
        self.count += 1

        return True

    # 読み戻す (学習で使う / 壊れた行は飛ばす)。
    def rows(self):
        if not os.path.exists(self.path):
            return []
        rows = []
        try:
            with open(self.path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError as e:
            _logger.warning("操作ログを読めませんでした: %s", e)

        return rows


# コマンドから記録できる値を拾う。
#
# Command の値は `_clip_id` のような非公開属性で持たれている。§15 の「公開属性から拾う」
# だけでは何も残らないコマンドが多いため、**先頭の下線を外した名前**で拾う
# (36 クラスへ to_log() を足さない方針を守りつつ、中身が分かるようにする)。
def params_of(command):
    values = {}
    for name, value in vars(command).items():
        if len(values) >= _MAX_PARAMS:
            break
        key = name.lstrip("_")
        if not key or key.startswith("tracks_cfg") or key == "cfg":
            continue
        picked = _scalar(value)
        if picked is not None:
            values[key] = picked

    return values


def _scalar(value):
    if isinstance(value, str):
        return value[:_MAX_CHARS]
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple)):
        items = [item for item in (_scalar(entry) for entry in value)
                 if item is not None]

        return items[:_MAX_PARAMS] if items else None

    return None
