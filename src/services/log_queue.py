# クライアント側ログ送信のローカルキュー (request1 / resolve1 §5)
#
# テーブルごとにフォルダを作り、waiting → sending → sent の順にファイルを
# 移動させる。1 ファイル = 1 件 = 1 リクエストで、中身は API のリクエスト本文
# そのままである (resolve1 §5.3)。
#
# ここは **HTTP を一切知らない**。積む側 (telemetry) はネットワークも認証も
# 触らずにファイルを 1 本書くだけで済み、送る側 (log_sender) はフォルダから
# 拾って POST するだけで済む。この分離は後でキューを SQLite へ差し替える
# ための境目でもある (resolve1 §14.5)。
#
# 「移す」は必ず os.replace による move 1 操作で行う (resolve1 §5.6)。
# コピーしてから削除すると、途中で落ちたときに waiting と sending の両方へ
# 残り、冪等キーを持たないテーブル (ExportLogs / Errors / AnalyticsEvents) で
# 行が二重に増える。
import json
import os
import re
import threading
from datetime import datetime, timezone

from .auth_store import default_auth_path
from ..utils.logger import get_logger

_logger = get_logger(__name__)

# ---- テーブル (フォルダ名としてそのまま使う) --------------------------------
TABLE_SESSIONS = "Sessions"
TABLE_PROJECTS = "Projects"
TABLE_EVENTS = "AnalyticsEvents"
TABLE_EXPORT_LOGS = "ExportLogs"
TABLE_PROCESSING_LOGS = "ProcessingLogs"
TABLE_ERRORS = "Errors"

TABLES = (
    TABLE_SESSIONS,
    TABLE_PROJECTS,
    TABLE_EVENTS,
    TABLE_EXPORT_LOGS,
    TABLE_PROCESSING_LOGS,
    TABLE_ERRORS,
)

# ---- 状態 (フォルダ名) ------------------------------------------------------
WAITING = "waiting"
SENDING = "sending"
SENT = "sent"
ERROR = "error"

_STATES = (WAITING, SENDING, SENT, ERROR)

# ログファイルの中身のスキーマ版 (resolve1 §5.3)
SCHEMA = 1

# 何度送っても通らないログを往復させ続けない上限 (resolve1 §9)
MAX_ATTEMPTS = 10

# waiting に溜めてよい件数 (resolve1 §5.9)。溢れたら古いものから捨てる。
DEFAULT_LIMITS = {
    TABLE_ERRORS: 500,
    TABLE_EVENTS: 5000,
    "default": 1000,
}

# 保持期間 (resolve1 §5.8)
DEFAULT_SENT_RETENTION_DAYS = 7
DEFAULT_ERROR_RETENTION_DAYS = 30

_SECONDS_PER_DAY = 86400

# yyyyMMddHHmmss_(テーブル名).log / 衝突時は yyyyMMddHHmmss-NN_(テーブル名).log
_NAME_RE = re.compile(r"^(\d{14})(?:-(\d{2}))?_([A-Za-z]+)\.log$")


# ログファイル名を組み立てる。1 件目は要求どおりの名前で、
# 同一秒の 2 件目以降だけ -02 以降が付く (resolve1 §5.2)。
def _compose(stamp, index, table):
    suffix = "" if index <= 1 else f"-{index:02d}"
    return f"{stamp}{suffix}_{table}.log"


# 並び順は「時刻 → 同一秒内の連番」で決める。
# 文字列の順では "-02" が "_" より前へ来てしまい、同一秒の 2 件目が
# 1 件目より先に送られる (ASCII で '-' < '_')。
def _sort_key(name):
    parts = _NAME_RE.match(name)
    if parts is None:
        return (name, 0)
    stamp, seq, _table = parts.groups()
    return (stamp, int(seq or 1))


# %LOCALAPPDATA%\Stretheus\logqueue を返す (auth.dat と同じ親 / resolve1 §5.1)。
def default_queue_dir():
    return os.path.join(os.path.dirname(default_auth_path()), "logqueue")


# 1 件のログ。ファイルの場所と中身を持つ。
class LogEntry:

    def __init__(self, table, state_path, data):
        self.table = table
        # いまファイルが置かれている絶対パス
        self.state_path = state_path
        self.data = data or {}

    @property
    def name(self):
        return os.path.basename(self.state_path)

    @property
    def path(self):
        return str(self.data.get("path", "") or "")

    @property
    def body(self):
        return self.data.get("body")

    @property
    def attempts(self):
        try:
            return int(self.data.get("attempts", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def depends_on(self):
        depends = self.data.get("dependsOn")
        return depends if isinstance(depends, dict) else {}


class LogQueue:

    def __init__(self, base_dir=None, limits=None):
        self._base_dir = base_dir or default_queue_dir()
        self._limits = dict(DEFAULT_LIMITS)
        if isinstance(limits, dict):
            self._limits.update({k: v for k, v in limits.items() if v})
        self._lock = threading.Lock()

    @property
    def base_dir(self):
        return self._base_dir

    # ---- 積む ------------------------------------------------------------
    # 1 件を waiting へ書く。送信はしない。成功したらファイル名、失敗なら None。
    # 例外は外へ出さない (ログ送信でアプリを壊さない)。
    def enqueue(self, table, path, body, depends_on=None):
        if table not in TABLES or not path:
            _logger.warning("未知のテーブルへのログ要求を捨てました: %s", table)
            return None

        entry = {
            "schema": SCHEMA,
            "table": table,
            "path": path,
            "method": "POST",
            "createdAt": _utc_now_iso(),
            "attempts": 0,
            "lastError": None,
            "dependsOn": depends_on or {},
            "body": body,
        }

        try:
            with self._lock:
                self._trim(table)
                target = self._reserve(table, WAITING)
                _write_json(target, entry)
            return os.path.basename(target)
        except OSError as e:
            _logger.warning("ログを保存できませんでした (%s): %s", table, e)
            return None

    # ---- 取り出す --------------------------------------------------------
    # waiting から古い順に最大 limit 件を sending へ move して返す。
    # skip(data) が True を返したものは waiting へ残す (依存待ち / resolve1 §6.6)。
    # 壊れている (JSON として読めない) ものは error へ落とす。
    def pick(self, table, limit, skip=None):
        picked = []
        if limit <= 0:
            return picked

        for name in self._names(table, WAITING):
            if len(picked) >= limit:
                break

            source = os.path.join(self._dir(table, WAITING), name)
            data = _read_json(source)
            if not isinstance(data, dict) or not data.get("path"):
                self._move(source, self._dir(table, ERROR), name)
                _logger.warning("壊れたログを error へ移しました: %s", name)
                continue

            if skip is not None and skip(data):
                continue

            moved = self._move(source, self._dir(table, SENDING), name)
            if moved is None:
                continue
            picked.append(LogEntry(table, moved, data))

        return picked

    # sending にあるものを送信済みとして sent へ移す。
    def mark_sent(self, entry):
        self._move(entry.state_path, self._dir(entry.table, SENT), entry.name)

    # 再送しても直らない失敗。error へ移す (resolve1 §9)。
    def mark_error(self, entry, reason=None):
        entry.data["lastError"] = _short(reason)
        _rewrite(entry.state_path, entry.data)
        self._move(entry.state_path, self._dir(entry.table, ERROR), entry.name)
        _logger.warning("ログを error へ移しました (%s): %s", entry.name, _short(reason))

    # 一時的な失敗。attempts を +1 して waiting へ戻す。
    # 上限を超えていたら error へ落とす (往復し続けさせない)。
    def release(self, entry, reason=None):
        entry.data["attempts"] = entry.attempts + 1
        entry.data["lastError"] = _short(reason)

        if entry.data["attempts"] > MAX_ATTEMPTS:
            self.mark_error(entry, reason="attempts を超過: " + _short(reason))
            return

        _rewrite(entry.state_path, entry.data)
        self._move(entry.state_path, self._dir(entry.table, WAITING), entry.name)

    # 中断時にまとめて waiting へ戻す (resolve1 §6.3)。
    # 中断は利用者の操作を優先しただけで失敗ではないため attempts は増やさない。
    def return_to_waiting(self, entries, reason=None):
        for entry in entries or []:
            entry.data["lastError"] = _short(reason)
            _rewrite(entry.state_path, entry.data)
            self._move(entry.state_path, self._dir(entry.table, WAITING), entry.name)

    # ---- 起動時の後始末 ---------------------------------------------------
    # 前回の送信が完了せずに落ちた分を waiting へ戻す (resolve1 §5.7)。
    # 併せて書き込み中に落ちた残骸 (.tmp / 0 バイト) を片付ける。
    def recover(self):
        recovered = 0
        for table in TABLES:
            for state in _STATES:
                self._clean_debris(table, state)

            for name in self._names(table, SENDING):
                source = os.path.join(self._dir(table, SENDING), name)
                data = _read_json(source)
                if isinstance(data, dict):
                    try:
                        data["attempts"] = int(data.get("attempts", 0) or 0) + 1
                    except (TypeError, ValueError):
                        data["attempts"] = 1
                    _rewrite(source, data)
                if self._move(source, self._dir(table, WAITING), name) is not None:
                    recovered += 1

        if recovered:
            _logger.info("前回送信しきれなかったログ %d 件を waiting へ戻しました。", recovered)
        return recovered

    # sent と error の古いものを消す (resolve1 §5.8)。判定は更新時刻で行う。
    def purge(self, sent_days=DEFAULT_SENT_RETENTION_DAYS,
              error_days=DEFAULT_ERROR_RETENTION_DAYS):
        removed = 0
        for table in TABLES:
            removed += self._purge_state(table, SENT, sent_days)
            removed += self._purge_state(table, ERROR, error_days)
        if removed:
            _logger.info("保持期間を過ぎたログ %d 件を削除しました。", removed)
        return removed

    # ---- 参照 ------------------------------------------------------------
    # waiting + sending に残っている body[field] の集合。依存の判定に使う。
    def pending_values(self, table, field):
        values = set()
        for state in (WAITING, SENDING):
            for name in self._names(table, state):
                data = _read_json(os.path.join(self._dir(table, state), name))
                body = (data or {}).get("body")
                if isinstance(body, dict) and body.get(field):
                    values.add(str(body[field]))
        return values

    def count(self, table, state):
        return len(self._names(table, state))

    # 診断ログ用の件数。{テーブル: {状態: 件数}}
    def counts(self):
        return {table: {state: self.count(table, state) for state in _STATES}
                for table in TABLES}

    # ---- 内部 ------------------------------------------------------------
    def _dir(self, table, state):
        path = os.path.join(self._base_dir, table, state)
        os.makedirs(path, exist_ok=True)
        return path

    # フォルダ内のログを古い順 (ファイル名順 = 時刻順) に返す。
    def _names(self, table, state):
        directory = os.path.join(self._base_dir, table, state)
        try:
            names = os.listdir(directory)
        except OSError:
            return []
        return sorted((name for name in names if _NAME_RE.match(name)), key=_sort_key)

    # yyyyMMddHHmmss_(テーブル名).log を確保する。
    # 同一秒に 2 件目が来たら -02 以降を足す (resolve1 §5.2)。
    # O_EXCL で空ファイルを作って名前を押さえるため、同時に書いても奪い合わない。
    #
    # 名前は **4 つの状態すべてで一意**にする。waiting から出た名前をすぐ使い回すと、
    # sent (7 日保持) に同名が居て移動時に退避名へ逃げることになる。
    def _reserve(self, table, state):
        directory = self._dir(table, state)
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")

        for index in range(1, 100):
            name = _compose(stamp, index, table)
            if self._exists_anywhere(table, name):
                continue

            candidate = os.path.join(directory, name)
            try:
                handle = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            os.close(handle)
            return candidate

        raise OSError(f"同一秒のログ名が埋まりました: {stamp}_{table}.log")

    # 同じ名前が waiting / sending / sent / error のどこかに在るか。
    def _exists_anywhere(self, table, name):
        return any(os.path.exists(os.path.join(self._base_dir, table, state, name))
                   for state in _STATES)

    # 上限を超えていたら古いものから捨てる (resolve1 §5.9)。
    def _trim(self, table):
        limit = int(self._limits.get(table, self._limits.get("default", 1000)) or 0)
        if limit <= 0:
            return

        names = self._names(table, WAITING)
        excess = len(names) - limit + 1
        if excess <= 0:
            return

        directory = self._dir(table, WAITING)
        for name in names[:excess]:
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass
        _logger.warning("%s の送信待ちが上限 %d 件を超えたため古い %d 件を捨てました。",
                        table, limit, excess)

    # move 1 操作。移した先の絶対パスを返す (失敗なら None)。
    def _move(self, source, dest_dir, name):
        target = os.path.join(dest_dir, name)
        try:
            if os.path.exists(target):
                target = _dedupe(target)
            os.replace(source, target)
            return target
        except OSError as e:
            _logger.warning("ログを移動できませんでした (%s): %s", name, e)
            return None

    def _purge_state(self, table, state, days):
        if days is None or days < 0:
            return 0

        directory = os.path.join(self._base_dir, table, state)
        try:
            names = os.listdir(directory)
        except OSError:
            return 0

        limit_sec = float(days) * _SECONDS_PER_DAY
        now = _now_epoch()
        removed = 0
        for name in names:
            path = os.path.join(directory, name)
            try:
                if now - os.path.getmtime(path) <= limit_sec:
                    continue
                os.remove(path)
                removed += 1
            except OSError:
                continue
        return removed

    # 書き込み中に落ちた残骸を消す。.tmp と、名前だけ押さえた 0 バイトのログ。
    def _clean_debris(self, table, state):
        directory = os.path.join(self._base_dir, table, state)
        try:
            names = os.listdir(directory)
        except OSError:
            return

        for name in names:
            path = os.path.join(directory, name)
            try:
                if name.endswith(".tmp"):
                    os.remove(path)
                elif _NAME_RE.match(name) and os.path.getsize(path) == 0:
                    os.remove(path)
            except OSError:
                continue


# ---- ファイル入出力 ---------------------------------------------------------
# 同じフォルダへ .tmp で書いてから os.replace で置き換える (resolve1 §5.4)。
# 読み手が半端な JSON を拾わず、電源断で 0 バイトのファイルも残らない。
def _write_json(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# attempts / lastError を書き戻す。失敗しても移動は続ける (回数が 1 回ずれるだけ)。
def _rewrite(path, data):
    try:
        _write_json(path, data)
    except OSError as e:
        _logger.warning("ログの更新に失敗しました (%s): %s", os.path.basename(path), e)


# 移動先に同名がある場合の退避名。通常は起きない (名前は 4 状態で一意) が、
# 手で置かれたファイルと衝突しても上書きで失わないようにする。
#
# 退避名も yyyyMMddHHmmss-NN_(テーブル名).log の形に収める。別の形にすると
# 一覧 (_names) と掃除 (purge) の対象から外れ、消えないファイルが残る。
def _dedupe(target):
    directory, name = os.path.split(target)
    parts = _NAME_RE.match(name)
    if parts is None:
        return target

    stamp, _seq, table = parts.groups()
    for index in range(2, 100):
        candidate = os.path.join(directory, _compose(stamp, index, table))
        if not os.path.exists(candidate):
            return candidate
    return target


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _now_epoch():
    return datetime.now().timestamp()


def _short(value, limit=200):
    if not value:
        return None
    text = str(value).replace("\n", " ")
    return text[:limit]
